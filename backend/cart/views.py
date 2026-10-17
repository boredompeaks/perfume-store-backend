from django.db import transaction
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.decorators import api_view, throttle_classes, throttle_scope
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from orders.views import validate_redeemable_coupon
from products.models import products

from .models import Cart, CartItem
from .serializers import CartSerializer


class CartMutationRateThrottle(ScopedRateThrottle):
    """Applies the 'cart' scope to mutating methods only.

    The cart view mixes GET (read) and POST (add) in one endpoint; reads
    must not consume the mutation budget that guards write abuse, so safe
    methods bypass the throttle."""

    def allow_request(self, request, view):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return super().allow_request(request, view)


def _object_body(data):
    """The write body as a dict, or ``{}`` when it is not a JSON object.

    A JSON array / scalar / null body parses fine but has no ``.get`` to
    call, so reading keys off it raised AttributeError — a 500 on a public
    write endpoint (F7-B1). Reading it as the empty object routes every
    such body into the endpoint's own missing-key 400, byte-identical to
    posting ``{}``; form posts are QueryDict (a dict subclass) and are
    unaffected."""
    return data if isinstance(data, dict) else {}


# SPEC-17-03 [R-17.18]: this GET is the SPA's boot call (the header's cart
# badge runs it on every page load), so it is the natural surface to issue
# the csrftoken cookie from: the browser has the double-submit cookie in
# hand before the first gated mutation, and the SPA's X-CSRFToken slot
# (api.ts) replays it on every unsafe method. CSRF enforcement itself lives
# in common.authentication.SessionCartCSRFAuthentication.
@api_view(["GET", "POST"])
@ensure_csrf_cookie
@throttle_classes([CartMutationRateThrottle])
@throttle_scope("cart")
def cart_detail(request):

    # The session key is the cart's owner key; mint one so a first-time
    # boot GET finds (or lazily creates) the cart row instead of failing.
    if not request.session.session_key:
        request.session.create()

    session_id = request.session.session_key

    cart, created = Cart.objects.get_or_create(session_id=session_id)

    if request.method == "GET":
        serializer = CartSerializer(cart)

        return Response(serializer.data)

    elif request.method == "POST":
        body = _object_body(request.data)
        product_id = body.get("product_id")
        quantity = body.get("quantity", 1)

        if not product_id:
            return Response(
                {"error": "product_id is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            product = products.objects.get(id=product_id)

        except (products.DoesNotExist, ValueError, TypeError):
            # ValueError/TypeError: non-numeric product_id must 404, not 500
            return Response(
                {"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND
            )

        # MERCHANT-DELIST: the buying surface treats a delisted product
        # exactly like an unknown one — this same 404, byte-identical body
        # — so a cart add is refused without becoming an oracle that
        # distinguishes "never existed" from "taken off sale" (the public
        # detail endpoint does the same). The customer's cart is untouched;
        # a line already holding a delisted product is checkout's problem,
        # refused there with a message that names it.
        if not product.is_active:
            return Response(
                {"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND
            )

        try:
            quantity = int(quantity)

        except (ValueError, TypeError):
            return Response(
                {"error": "Quantity must be a number"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if quantity <= 0:
            return Response(
                {"error": "Quantity must be greater than 0"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Advisory only: stock can drop between here and capture, so the
        # authority for "enough stock" is commit_order_sale's summed
        # re-check at payment time, not this unlocked read.
        if quantity > product.stock:
            return Response(
                {"error": "Not enough stock"}, status=status.HTTP_400_BAD_REQUEST
            )

        cart_item, created = CartItem.objects.get_or_create(
            cart=cart, product=product, defaults={"quantity": quantity}
        )

        if not created:
            new_quantity = cart_item.quantity + quantity

            if new_quantity > product.stock:
                return Response(
                    {"error": "Not enough stock"}, status=status.HTTP_400_BAD_REQUEST
                )

            cart_item.quantity = new_quantity
            cart_item.save()

        serializer = CartSerializer(cart)

        return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@throttle_scope("cart")
def cart_item_detail(request, item_id):

    # Uniform cart-family 404 (conventions.md: no existence leaks): no
    # session is answered exactly like a missing cart row.
    if not request.session.session_key:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    session_id = request.session.session_key

    try:
        cart_item = CartItem.objects.get(id=item_id, cart__session_id=session_id)

    except CartItem.DoesNotExist:
        return Response(
            {"error": "Cart item not found"}, status=status.HTTP_404_NOT_FOUND
        )

    if request.method == "PATCH":
        quantity = _object_body(request.data).get("quantity")

        if quantity is None:
            return Response(
                {"error": "quantity is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            quantity = int(quantity)

        except (ValueError, TypeError):
            return Response(
                {"error": "Quantity must be a number"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if quantity <= 0:
            return Response(
                {"error": "Quantity must be greater than 0"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Advisory like the add path: payment's summed re-check is the
        # authority; this keeps an impossible quantity out of the row.
        if quantity > cart_item.product.stock:
            return Response(
                {"error": "Not enough stock"}, status=status.HTTP_400_BAD_REQUEST
            )

        cart_item.quantity = quantity
        cart_item.save()

        serializer = CartSerializer(cart_item.cart)

        return Response(serializer.data)

    elif request.method == "DELETE":
        cart = cart_item.cart

        cart_item.delete()

        serializer = CartSerializer(cart)

        return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(["POST", "DELETE"])
@throttle_scope("cart")
def cart_coupon(request):
    """R-9.3.5/R-9.3.6: apply/remove a coupon as persistent cart state.

    The coupon rides the Cart as a FK, so it survives across requests; the
    checkout flow re-validates it through its pre-existing coupon path, so
    a coupon invalidated between apply and checkout can never reach an
    order."""

    if not request.session.session_key:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        cart = Cart.objects.get(session_id=request.session.session_key)

    except Cart.DoesNotExist:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        # Idempotent by contract: removing when no coupon is applied still
        # succeeds (the caller's end state already holds), returning the
        # cart-family 200 with the cart body so the client can re-render
        # without a follow-up GET.
        with transaction.atomic():
            locked = Cart.objects.select_for_update().get(pk=cart.pk)
            if locked.coupon_id:
                locked.coupon = None
                locked.save(update_fields=["coupon"])
            serializer = CartSerializer(locked)

        return Response(serializer.data)

    code = _object_body(request.data).get("code")

    if not code:
        return Response(
            {"error": "Coupon code is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    # Delegated redemption check: the same gate the public preview uses,
    # so an unknown/inactive/expired/limit-hit/below-minimum coupon is
    # refused here with the one uniform body (V-11) and never stored.
    coupon, _subtotal, rejection = validate_redeemable_coupon(code, cart)

    if rejection is not None:
        return rejection

    with transaction.atomic():
        # Lock the cart row around the write: apply and remove are
        # user-scoped single-row mutations, and the lock serializes a
        # concurrent apply/remove pair (double-submit) on the cart
        # (conventions.md: side-effectful flows run under a row lock).
        locked = Cart.objects.select_for_update().get(pk=cart.pk)
        locked.coupon = coupon
        locked.save(update_fields=["coupon"])
        serializer = CartSerializer(locked)

    return Response(serializer.data, status=status.HTTP_200_OK)

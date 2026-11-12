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
from .services import resolve_cart


class CartMutationRateThrottle(ScopedRateThrottle):
    """Applies the 'cart' scope to mutating methods only.

    Reads must not consume the budget that guards write abuse, so safe methods
    bypass the throttle.
    """

    def allow_request(self, request, view):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return super().allow_request(request, view)


def _object_body(data):
    """The write body as a dict, or ``{}`` when it is not a JSON object.

    A list/scalar/null body parses fine but has no ``.get``, so reading keys off
    it 500s; as ``{}`` it falls into the endpoint's own missing-key 400 instead.
    """
    return data if isinstance(data, dict) else {}


# This GET is the SPA's boot call (the header cart badge runs it on page load),
# so it issues the csrftoken cookie before the first gated mutation; enforcement
# itself lives in common.authentication.SessionCartCSRFAuthentication.
@api_view(["GET", "POST"])
@ensure_csrf_cookie
@throttle_classes([CartMutationRateThrottle])
@throttle_scope("cart")
def cart_detail(request):

    # Ownership lives in one place: a signed-in caller gets the account cart, a
    # guest the session cart, whose key is minted lazily for a first boot GET.
    cart = resolve_cart(request)

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

        # A delisted product is answered exactly like an unknown one — same 404,
        # same body — so cart add is not an oracle distinguishing "never existed"
        # from "taken off sale".
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

        # Advisory only: the summed re-check at payment time is the authority.
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

    # Uniform 404 (no existence leaks): no cart for this caller answers exactly
    # like a missing item, and lookup is scoped to the caller's own cart.
    cart = resolve_cart(request, create=False)

    if cart is None:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        cart_item = CartItem.objects.get(id=item_id, cart=cart)

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

        # Advisory like the add path: payment's summed re-check is the authority.
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
    """Apply/remove a coupon as persistent cart state.

    The coupon rides the Cart as a FK, so it survives across requests; checkout
    re-validates it, so a coupon invalidated between apply and checkout can never
    reach an order.
    """

    cart = resolve_cart(request, create=False)

    if cart is None:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        # Idempotent by contract: removing with no coupon applied still succeeds
        # and returns the cart body so the client can re-render without a GET.
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

    # Same gate as the public preview: an unknown/inactive/expired/limit-hit/
    # below-minimum coupon is refused with the same uniform body and never stored.
    coupon, _subtotal, rejection = validate_redeemable_coupon(code, cart)

    if rejection is not None:
        return rejection

    with transaction.atomic():
        # Lock the cart row around the write: this serializes a concurrent
        # apply/remove pair (double submit) on the cart.
        locked = Cart.objects.select_for_update().get(pk=cart.pk)
        locked.coupon = coupon
        locked.save(update_fields=["coupon"])
        serializer = CartSerializer(locked)

    return Response(serializer.data, status=status.HTTP_200_OK)

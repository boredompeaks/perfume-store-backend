"""Checkout: the order lifecycle's opening move, plus the guest order read and
the public coupon preview. Checkout is account-only, so an anonymous submission
gets one uniform refusal before anything is read or written; order creation owns
both collapse guards (duplicate window and header-keyed idempotency)."""

import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_scope
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from cart.models import Cart
from cart.services import resolve_cart
from common.models import AuditEvent
from common.money import quantize_money

# Checkout mints the lifecycle's stock holds.
from products.models import StockReservation

# The client names a delivery OPTION only, never an amount, so the shipping
# cost stays out of the client's hands.
from shipping.pricing import ShippingUnavailable, quote_shipping

from ..models import Coupon, Order, OrderItem, OrderStatusEvent
from ..serializers import CouponPreviewSerializer, OrderSerializer, money_string

# Order-creation transition audit trigger.
from ..state import TRIGGER_ORDER_CREATE

logger = logging.getLogger(__name__)

# Cap mirrors the Order.idempotency_key column width, so an oversized value is a
# 400 here instead of a database error at insert time.
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX_LENGTH = 128


def _object_body(data):
    """The write body as a dict, or ``{}`` when it is not a JSON object. A
    non-object body has no ``.get``, so reading one would 500 a public write
    endpoint; inlined from ``cart.views`` to avoid an import cycle."""
    return data if isinstance(data, dict) else {}


# The guest read credential is a header, never a query param: URLs leak into
# access logs, history and Referer.
GUEST_TOKEN_HEADER = "X-Guest-Order-Token"

# Anything wider than the matched column (64) is refused before the index.
GUEST_TOKEN_MAX_LENGTH = 64


def _guest_checkout_refused():
    """The ONE answer every anonymous checkout submission gets. The gate runs
    before any read or write, and one body for every reason keeps a caller from
    probing which inputs a guest path accepts."""
    return Response(
        {
            "error": (
                "Guest checkout is not available. "
                "Sign in or create an account to place an order."
            )
        },
        status=status.HTTP_403_FORBIDDEN,
    )


def _field_length_rejection(field, value):
    """A 400 when ``value`` is wider than the column it is written to. SQLite
    ignores ``varchar(n)`` while Postgres raises ``DataError``, so the limit is
    read off the model field to keep refusal and column in step."""
    max_length = Order._meta.get_field(field).max_length
    if max_length is None:
        return None
    if len(str(value)) <= max_length:
        return None
    return Response(
        {"error": f"{field} must be at most {max_length} characters"},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _guest_lookup_miss():
    """The one answer every guest lookup failure gets: identical for a bad
    token, an unknown number and somebody else's order, so the endpoint is not
    an order-existence oracle."""
    return Response(
        {"error": "Order not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def _checkout_response(order, status_code):
    """The checkout body, with no guest credential to disclose:
    ``OrderSerializer`` never carries ``guest_token``."""
    return Response(OrderSerializer(order).data, status=status_code)


# Must stay in step with create_order's required_fields (same payload).
_SHIPPING_FIELDS = ("full_name", "phone", "address", "city", "state", "pincode")


def _order_lines(cart_items):
    """Order identity as sorted (product_id, quantity) pairs, independent of
    cart iteration order."""
    return sorted((item.product_id, item.quantity) for item in cart_items)


def _same_purchase(order, cart_items, coupon, payload):
    """Does ``order`` describe the purchase this submission is making? Four
    agreements beyond owner identity: cart lines, coupon, full shipping payload
    and delivery option."""
    if _order_lines(order.items.all()) != _order_lines(cart_items):
        return False
    if order.coupon_id != (coupon.pk if coupon else None):
        # a deliberate coupon difference is a new purchase, not a retry
        return False
    if not _same_shipping_choice(order, payload):
        return False
    shipping = tuple(payload.get(field) for field in _SHIPPING_FIELDS)
    return tuple(getattr(order, field) for field in _SHIPPING_FIELDS) == shipping


# The client-supplied delivery OPTION is a method code, never an amount.
SHIPPING_METHOD_FIELD = "shipping_method"


def _requested_shipping_code(payload):
    """The normalized delivery option this submission names, or None. An absent
    or blank value means the server priced the destination itself."""
    raw = payload.get(SHIPPING_METHOD_FIELD)
    if raw is None:
        return None
    return str(raw).strip() or None


def _same_shipping_choice(order, payload):
    """Does the delivery option this submission names agree with the order's? A
    keyless replay that named no option agrees with whatever the server chose."""
    requested = _requested_shipping_code(payload)
    if requested is None:
        return True
    priced_as = order.shipping_method.code if order.shipping_method_id else None
    return priced_as == requested


def _find_duplicate_pending_order(owner, cart_items, coupon, payload):
    """Return the owner's recent pending order with the identical payload, or
    None to create. Checkout doesn't clear the cart, so a double-click would
    otherwise mint a second payable order (and a second charge target)."""
    cutoff = timezone.now() - timedelta(seconds=settings.CHECKOUT_DEDUP_WINDOW_SECONDS)
    candidates = (
        Order.objects.filter(
            **owner,
            status="pending",
            created_at__gte=cutoff,
        )
        # Payable gate mirrors verify_payment.
        .filter(Q(razorpay_payment_id__isnull=True) | Q(razorpay_payment_id=""))
        .prefetch_related("items")
        .order_by("-created_at")
    )
    for candidate in candidates:
        if _same_purchase(candidate, cart_items, coupon, payload):
            return candidate
    return None


# A checkout still losing the number race after this many attempts fails loudly
# (the whole transaction rolls back) instead of looping forever.
ORDER_NUMBER_ATTEMPTS = 5


def _current_year():
    """The order-number year bucket, the only clock the format depends on."""
    return timezone.now().year


def _generate_order_number():
    """Mint a customer-facing order number: a year bucket plus a random suffix
    from ``secrets`` (never ``random``). The unique constraint on
    ``Order.order_number`` remains the concurrency authority."""
    year = _current_year()
    return f"ORD-{year}-{secrets.token_hex(4).upper()}"


def _mint_order_reservations(order, lines, user):
    """Mint one time-limited hold per order line inside the caller's atomic
    block, so a rollback leaves no hold. Deliberately lock-free: verify_payment's
    locked re-check remains the oversell authority."""
    for line in lines:
        try:
            # Savepoint: a lost (order, product) mint race rolls back only this
            # insert, leaving the outer transaction usable for the re-target.
            with transaction.atomic():
                StockReservation.objects.create(
                    product=line.product,
                    order=order,
                    owner=user,
                    quantity=line.quantity,
                    expires_at=StockReservation.expiry_from_now(),
                )
        except IntegrityError:
            # The (order, product) unique constraint is the authority: a replay
            # re-targets the existing hold instead of double-counting stock.
            reservation = StockReservation.objects.select_for_update().get(
                order=order, product=line.product
            )
            reservation.quantity = line.quantity
            reservation.expires_at = StockReservation.expiry_from_now()
            reservation.status = StockReservation.Status.ACTIVE
            reservation.save(update_fields=["quantity", "expires_at", "status"])


@api_view(["POST"])
# AllowAny is deliberate: the refusal is ours to write, not DRF's generic 401.
# An authenticated caller is still scoped to their own orders below.
@permission_classes([AllowAny])
# Public mutating endpoint sharing the cart budget: checkout is the terminal
# mutation of that same session-cart funnel.
@throttle_scope("cart")
def create_order(request):

    # FIRST line of the body: every anonymous submission gets the one uniform
    # refusal, before any input is read.
    if not request.user.is_authenticated:
        return _guest_checkout_refused()

    # Same key means the SAME submission; the atomic block dedupes on (user, key).
    # Blank means no key, since an empty value carries no submission identity.
    idempotency_key = (
        request.headers.get(IDEMPOTENCY_KEY_HEADER) or ""
    ).strip() or None

    if (
        idempotency_key is not None
        and len(idempotency_key) > IDEMPOTENCY_KEY_MAX_LENGTH
    ):
        return Response(
            {"error": "Idempotency-Key is too long"}, status=status.HTTP_400_BAD_REQUEST
        )

    # Who this submission belongs to, settled once and reused by every writer
    # below. A body-supplied guest_email is ignored (one owner per row).
    user = request.user
    guest_email = ""
    owner = {"user": user}

    # Same ownership rule as the cart endpoints; create=False keeps the
    # documented "Cart not found" 404 instead of minting an empty cart.
    cart = resolve_cart(request, create=False)

    if cart is None:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    cart_items = cart.items.select_related("product").all()

    if not cart_items.exists():
        return Response({"error": "Cart is empty"}, status=status.HTTP_400_BAD_REQUEST)

    body = _object_body(request.data)

    required_fields = [
        "full_name",
        "phone",
        "address",
        "city",
        "state",
        "pincode",
    ]

    for field in required_fields:
        if not body.get(field):
            return Response(
                {"error": f"{field} is required"}, status=status.HTTP_400_BAD_REQUEST
            )

    # SQLite does not enforce these varchar widths, so an over-length value is a
    # 201 in tests and a DataError on production Postgres.
    for field in _SHIPPING_FIELDS:
        length_rejection = _field_length_rejection(field, body[field])

        if length_rejection is not None:
            return length_rejection

    # A cart line that outlasted its stock must not become a payable order;
    # this gate is advisory, verify_payment re-checks under a row lock.
    unavailable = []

    # A delisted line is not buyable at ANY quantity, so it gets its own
    # refusal; the line stays in the cart because removal is the customer's call.
    delisted = []

    subtotal_amount = Decimal("0.00")

    for cart_item in cart_items:
        product = cart_item.product

        if not product.is_active:
            delisted.append(product)
            continue

        if cart_item.quantity > product.stock:
            unavailable.append(
                {
                    "name": product.name,
                    "requested": cart_item.quantity,
                    "available": product.stock,
                }
            )

        subtotal_amount += product.price * cart_item.quantity

    if delisted:
        # Delist takes precedence over the stock refusal (quantity cannot fix
        # it); ordering by pk keeps the refusal body deterministic.
        delisted.sort(key=lambda product: product.id)
        names = ", ".join(f'"{product.name}"' for product in delisted)
        if len(delisted) == 1:
            action = "Remove the item from the cart to continue."
        else:
            action = "Remove these items from the cart to continue."
        return Response(
            {
                "error": f"No longer available for purchase: {names}. {action}",
                "products": [
                    {"id": product.id, "name": product.name} for product in delisted
                ],
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if unavailable:
        details = ", ".join(
            f'"{item["name"]}" (requested {item["requested"]}, '
            f"only {item['available']} in stock)"
            for item in unavailable
        )

        if len(unavailable) == 1:
            action = "Reduce the quantity or remove the item to continue."
        else:
            action = "Reduce the quantity or remove these items to continue."

        return Response(
            {
                "error": f"Not enough stock for {details}. {action}",
                "products": unavailable,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    coupon = None
    discount_amount = Decimal("0.00")

    coupon_code = body.get("coupon_code")

    # With no explicit code, the coupon persisted on the cart is the source;
    # every rule still re-runs here because state may have changed since apply.
    if not coupon_code and cart.coupon_id:
        coupon_code = cart.coupon.code

    if coupon_code:
        try:
            coupon = Coupon.objects.get(code__iexact=coupon_code)

        except Coupon.DoesNotExist:
            return Response(
                {"error": "Invalid coupon code"}, status=status.HTTP_400_BAD_REQUEST
            )

        if not coupon.active:
            return Response(
                {"error": "This coupon is inactive"}, status=status.HTTP_400_BAD_REQUEST
            )

        now = timezone.now()

        if now < coupon.valid_from:
            return Response(
                {"error": "This coupon is not active yet"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if now > coupon.valid_until:
            return Response(
                {"error": "This coupon has expired"}, status=status.HTTP_400_BAD_REQUEST
            )

        if coupon.usage_limit is not None and coupon.used_count >= coupon.usage_limit:
            return Response(
                {"error": "This coupon has reached its usage limit"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if subtotal_amount < coupon.minimum_order_amount:
            return Response(
                {
                    "error": "Minimum order amount is required",
                    # The envelope would re-serialize a bare Decimal as a float,
                    # so money stays a 2-dp string on the wire.
                    "minimum_order_amount": money_string(coupon.minimum_order_amount),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # One helper for the coupon-type math, so this order row and the public
        # preview cannot disagree (a free-shipping coupon returns 0.00 here).
        discount_amount = coupon_merchandise_discount(coupon, subtotal_amount)

    # Still the MERCHANDISE total; shipping is added below so a replay never
    # reprices an order that already exists.

    merchandise_total = subtotal_amount - discount_amount

    # A shipping refusal must propagate out of the atomic block, not return
    # from inside it: a partial order with no shipping cost would be free.
    try:
        with transaction.atomic():
            # Serialize same-session submissions on the cart row, so the loser
            # re-runs dedup after the winner commits; no new lock-order cycle.
            cart = Cart.objects.select_for_update().get(pk=cart.pk)

            duplicate = _find_duplicate_pending_order(owner, cart_items, coupon, body)

            if duplicate is not None:
                # Benign double-submit retry, so INFO.
                logger.info(
                    "Checkout dedup: order %s replayed for %s "
                    "(identical pending submission)",
                    duplicate.id,
                    duplicate.customer_name,
                )

                # Scoped by owner and ``_same_purchase``: identity, shipping,
                # delivery option, cart and coupon all agree inside the window.
                return _checkout_response(duplicate, status.HTTP_200_OK)

            # Key replay: lock the user row so concurrent keyed writers
            # serialize; the (user, idempotency_key) constraint stays final.
            if idempotency_key is not None:
                User.objects.select_for_update().get(pk=user.pk)

                replay = (
                    Order.objects.filter(**owner, idempotency_key=idempotency_key)
                    .prefetch_related("items")
                    .first()
                )

                if replay is not None:
                    # An authenticated (user, key) pair is strong enough to
                    # collapse regardless of payload drift.
                    logger.info(
                        "Checkout idempotency: order %s replayed for %s "
                        "(Idempotency-Key)",
                        replay.id,
                        replay.customer_name,
                    )

                    return _checkout_response(replay, status.HTTP_200_OK)

            # Price delivery after the collapse guards, inside the atomic block;
            # clients name an option only, and lock=True keeps a staff edit out.
            shipping_quote = quote_shipping(
                region=body.get("state"),
                postal_code=body.get("pincode"),
                merchandise_total=merchandise_total,
                method_code=_requested_shipping_code(body),
                lock=True,
            )

            # A None quote means no shipping configured at all, recorded as an
            # honest 0.00 rather than a zero rate.
            shipping_amount = (
                shipping_quote.amount if shipping_quote is not None else Decimal("0.00")
            )
            # Either waiver zeroes the one order-level charge (both equals one);
            # the quote still ran, so an unserviceable destination is refused.
            free_delivery = any(item.product.free_shipping for item in cart_items) or (
                coupon is not None and coupon.discount_type == Coupon.FREE_SHIPPING
            )
            if free_delivery:
                shipping_amount = Decimal("0.00")
            shipping_method_id = (
                shipping_quote.method_id if shipping_quote is not None else None
            )
            # Snapshot the option label beside the FK so the order still names
            # it after the method row is deleted; empty only when unconfigured.
            shipping_method_code = (
                shipping_quote.method_code if shipping_quote is not None else ""
            )
            if shipping_quote is None:
                # Store has no active shipping method. WARNING because it is
                # silent revenue; the 201 stands and the amount stays 0.00.
                logger.warning(
                    "Checkout priced with NO shipping charge for %s: the store "
                    "has no active shipping method configured (seed one with "
                    "`manage.py seed_shipping_methods` to start charging)",
                    user,
                )
            total_amount = quantize_money(merchandise_total + shipping_amount)

            # Minted inside the same atomic block so a rollback never burns a
            # number; each attempt is a savepoint so a lost race retries cleanly.
            for attempt in range(ORDER_NUMBER_ATTEMPTS):
                candidate = _generate_order_number()
                try:
                    with transaction.atomic():
                        order = Order.objects.create(
                            user=user,
                            # Account rows carry the empty guest_email (never
                            # NULL) and no guest_token.
                            guest_email=guest_email,
                            full_name=body.get("full_name"),
                            phone=body.get("phone"),
                            address=body.get("address"),
                            city=body.get("city"),
                            state=body.get("state"),
                            pincode=body.get("pincode"),
                            coupon=coupon,
                            discount_amount=discount_amount,
                            shipping_method_id=shipping_method_id,
                            shipping_method_code=shipping_method_code,
                            shipping_amount=shipping_amount,
                            total_amount=total_amount,
                            order_number=candidate,
                        )
                except IntegrityError:
                    # Lost the number race: the savepoint rolled the insert back,
                    # so the retry starts from committed state.
                    if attempt == ORDER_NUMBER_ATTEMPTS - 1:
                        raise
                    continue
                break

            # Bind the cart to the order in the same transaction so the capture
            # path (even a later webhook with no session) can find and empty it.
            cart.order = order
            cart.save(update_fields=["order"])

            # Creation is the lifecycle's first transition; the audit trail opens
            # here in the same transaction, so a rollback leaves no event behind.
            OrderStatusEvent.objects.create(
                order=order,
                from_status=None,
                to_status=order.status,
                actor=user,
                trigger=TRIGGER_ORDER_CREATE,
            )

            # Bind the key inside this transaction: the probe above proved no
            # committed order holds it and the user-row lock excludes a rival.
            if idempotency_key is not None:
                order.idempotency_key = idempotency_key
                order.save(update_fields=["idempotency_key"])

            # Inventory, coupon usage and cart cleanup happen only after the
            # payment provider confirms this order.

            for cart_item in cart_items:
                product = cart_item.product
                quantity = cart_item.quantity

                price = product.price

                item_subtotal = price * quantity

                OrderItem.objects.create(
                    order=order,
                    product=product,
                    product_name=product.name,
                    # Freeze catalogue identity: no variant selection exists yet
                    # and the product carries no SKU, so sku stays empty.
                    sku="",
                    variant_name=product.name,
                    price=price,
                    quantity=quantity,
                    subtotal=item_subtotal,
                )

            # Mint the checkout's holds in this same block (helper above).
            _mint_order_reservations(order, cart_items, user)

            # Creation trail lands in the same transaction, so a committed order
            # is never trail-less and a rollback leaves no row.
            AuditEvent.record(
                AuditEvent.EventType.ORDER_CREATED,
                actor=user,
                order=order,
                detail={
                    "order_id": order.id,
                    "total_amount": str(order.total_amount),
                    "coupon": coupon.code if coupon else None,
                    "item_count": len(cart_items),
                    # The delivery charge is money too, so the trail records it.
                    "shipping_method": shipping_quote.method_code
                    if shipping_quote is not None
                    else None,
                    "shipping_amount": str(shipping_amount),
                },
            )

    except ShippingUnavailable as exc:
        # The transaction rolled back whole: 400 with the reason, never a 500 and
        # never a silent free shipment.
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return _checkout_response(order, status.HTTP_201_CREATED)


@api_view(["GET"])
# Opened deliberately: a guest has no session, so the 256-bit token is the
# credential and the lookup below is a guard by possession.
@permission_classes([AllowAny])
def guest_order_detail(request, order_number=None):
    """GET /api/orders/guest/[<order_number>/] — a guest's OWN order. A unique
    token alone identifies one order; every miss gets the one uniform answer."""
    token = (request.headers.get(GUEST_TOKEN_HEADER) or "").strip()

    if not token or len(token) > GUEST_TOKEN_MAX_LENGTH:
        return _guest_lookup_miss()

    lookup = Order.objects.filter(guest_token=token)
    if order_number is not None:
        lookup = lookup.filter(order_number=order_number)

    order = lookup.first()
    if order is None:
        return _guest_lookup_miss()

    # The token is NULL on non-guest rows, so the filter IS the authorization.
    return Response(OrderSerializer(order).data)


def _uniform_coupon_rejection():
    """Every coupon failure on the public preview gets this same body and
    status, so the response never reveals whether a code exists."""
    return Response(
        {"error": "Invalid coupon code"}, status=status.HTTP_400_BAD_REQUEST
    )


def validate_redeemable_coupon(code, cart):
    """Shared redeemable gate for the coupon surfaces that resolve against a
    session cart: the full rule set, one uniform body so validation state never
    leaks. Returns ``(coupon, subtotal, None)`` or ``(None, None, rejection)``."""
    try:
        coupon = Coupon.objects.get(code__iexact=code)

    except Coupon.DoesNotExist:
        return None, None, _uniform_coupon_rejection()

    now = timezone.now()

    if (
        not coupon.active
        or now < coupon.valid_from
        or now > coupon.valid_until
        or (coupon.usage_limit is not None and coupon.used_count >= coupon.usage_limit)
    ):
        return None, None, _uniform_coupon_rejection()

    subtotal = Decimal("0.00")

    for item in cart.items.select_related("product"):
        subtotal += item.product.price * item.quantity

    if subtotal < coupon.minimum_order_amount:
        return None, None, _uniform_coupon_rejection()

    return coupon, subtotal, None


def coupon_merchandise_discount(coupon, subtotal):
    """The merchandise discount ``coupon`` grants on ``subtotal``, the one place
    the coupon-type math lives. A free-shipping coupon always returns 0.00: its
    waiver applies to the delivery quote instead."""
    if coupon.discount_type == Coupon.FREE_SHIPPING:
        return Decimal("0.00")

    if coupon.discount_type == "percentage":
        discount = quantize_money((subtotal * coupon.discount_value) / Decimal("100"))

        if coupon.maximum_discount is not None:
            discount = min(discount, coupon.maximum_discount)

    else:
        discount = coupon.discount_value

    return min(discount, subtotal)


@api_view(["POST"])
@throttle_scope("coupon")
def apply_coupon(request):

    code = _object_body(request.data).get("code")

    if not code:
        return Response(
            {"error": "Coupon code is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    # Resolve the cart first, so a cartless caller cannot probe code existence
    # by seeing the coupon error instead of the cart error.
    cart = resolve_cart(request, create=False)

    if cart is None:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    # Every rejection from here shares the one uniform response.
    coupon, subtotal, rejection = validate_redeemable_coupon(code, cart)

    if rejection is not None:
        return rejection

    # Same helper and quantize parity as checkout: the preview shows the exact
    # 2-dp discount the order will store.
    discount = coupon_merchandise_discount(coupon, subtotal)

    final_total = subtotal - discount

    # Decimals stay through the math; the serializer spells 2-dp strings on the
    # wire.
    return Response(
        CouponPreviewSerializer(
            {
                "coupon": coupon.code,
                "subtotal": subtotal,
                "discount": discount,
                "final_total": final_total,
            }
        ).data
    )

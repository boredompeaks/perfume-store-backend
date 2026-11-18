"""Checkout: the order lifecycle's opening move, plus the guest order read and
the public coupon preview. Checkout is account-only, so an anonymous submission
gets one uniform refusal before anything is read or written; order creation owns
both collapse guards (duplicate window and header-keyed idempotency)."""

import logging
import secrets
from datetime import timedelta
from decimal import Decimal
from functools import wraps

from django.apps import apps
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


def _maintenance_active():
    """Whether the operator has put the store into maintenance.

    # task: T-20261118-006
    Read on every call, never cached, so clearing the flag takes effect on the
    next request without a restart. The ops model is resolved through the app
    registry because the layer graph admits ``ops -> orders`` but not the
    reverse, and this read must not add a static edge. ``SiteSettings.load()``
    is deliberately not used: its ``get_or_create`` writes the singleton on a
    store that has never saved settings, and this gate runs before any write.
    """
    site_settings = apps.get_model("ops", "SiteSettings")
    return bool(
        site_settings.objects.filter(pk=1)
        .values_list("maintenance_mode", flat=True)
        .first()
    )


def _maintenance_refusal():
    """The one refusal both write doors answer while maintenance is on.

    Constant and input-independent: a caller cannot distinguish an existing
    order from a missing one, or a valid submission from an invalid one,
    through the maintenance response.
    """
    if not _maintenance_active():
        return None
    return Response(
        {"error": "The store is temporarily unavailable for maintenance."},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _maintenance_gated(view):
    """Refuse an authenticated write while the store is in maintenance.

    Wraps the view so the gate runs inside DRF's dispatch. Anonymous callers
    are skipped: checkout's uniform guest refusal must stay their first answer.
    """

    @wraps(view)
    def guarded(request, *args, **kwargs):
        if request.user.is_authenticated:
            refusal = _maintenance_refusal()
            if refusal is not None:
                return refusal
        return view(request, *args, **kwargs)

    return guarded


class _CouponUsageUnavailable(Exception):
    """No coupon usage slot remains for the order being created."""

    def __init__(self):
        super().__init__("This coupon has reached its usage limit")


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


def _resolve_checkout_cart(request):
    """The caller's cart and lines, or the refusal response that ends the view.

    D-04a/D-07: the SAME ownership rule the cart endpoints use. A customer
    who signed in checks out their ACCOUNT cart; a legacy guest order read
    stays scoped to the caller's session cart. create=False keeps the
    documented "Cart not found" 404 instead of minting an empty cart.
    """
    cart = resolve_cart(request, create=False)

    if cart is None:
        return (
            None,
            None,
            Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND),
        )

    cart_items = cart.items.select_related("product").all()

    if not cart_items.exists():
        return (
            cart,
            cart_items,
            Response({"error": "Cart is empty"}, status=status.HTTP_400_BAD_REQUEST),
        )

    return cart, cart_items, None


def _validate_checkout_payload(body):
    """The shipping payload's required fields and column widths, or a 400.

    The two loops stay separate on purpose: every missing field is answered
    before any width is checked, which is the order this endpoint has always
    answered in, so merging them would make an over-long present field win
    over a missing one.
    """
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

    # [R-1.13] The same varchar-width trap the guest email had, on the rest
    # of the shipping payload: SQLite does not enforce `full_name`'s 150 or
    # `phone`'s 15, so an over-length value is a 201 in the suite and a
    # DataError on the production Postgres. Refused here, before the atomic
    # block opens and therefore before any row exists.
    for field in _SHIPPING_FIELDS:
        length_rejection = _field_length_rejection(field, body[field])

        if length_rejection is not None:
            return length_rejection

    return None


def _scan_cart_lines(cart_items):
    """Collect delisted products, short lines and the merchandise subtotal.

    SPEC-6-01 [6.2.22]: a cart line that outlasted its stock (stock can
    drop after the item was added) must not become an order the customer
    can pay for. This gate is advisory and read-only -- stock can still
    change between create and pay, so verify_payment re-checks under a
    row lock before decrementing; that remains the authoritative backstop.

    MERCHANT-DELIST: a line whose product was taken off sale after it was
    added is not buyable at ANY quantity, so it is collected for its own
    refusal rather than the stock gate -- fixing the quantity can never fix
    it. The whole checkout is refused (never a silent skip of the line,
    which would charge the customer for a cart they did not submit), and the
    line itself is deliberately left in the cart: removal is the customer's
    call -- they see the line and the refusal names it -- and a relist makes
    the line buyable again without the customer having to rebuild the cart.
    """
    unavailable = []

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

    return delisted, unavailable, subtotal_amount


def _cart_line_rejection(delisted, unavailable):
    """The 400 for delisted products or short lines, or None to continue.

    The delist refusal takes precedence over the stock one: a line that is
    not for sale is unfixable by quantity, so telling the customer to
    "reduce the quantity" first would be wrong advice.
    """
    if delisted:
        # Sorted by pk: the cart queryset carries no ORDER BY, and a
        # deterministic body (same names, same order, every retry) is what
        # lets a client reliably match refusal entries to cart lines.
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

    return None


def _checkout_coupon_code(body, cart):
    """The code this submission names, else the one persisted on the cart.

    [R-9.3.5/R-9.3.6] The coupon applied to the cart persists as cart
    state: when the checkout payload posts no explicit code, the
    persisted one becomes the coupon source. The pre-existing validation
    below still re-runs every rule at checkout time (state may have
    changed since apply), so an invalidated coupon is rejected here
    rather than silently riding the FK into the order; a deleted coupon
    is already gone (SET_NULL) and simply applies nothing.
    """
    coupon_code = body.get("coupon_code")

    if not coupon_code and cart.coupon_id:
        coupon_code = cart.coupon.code

    return coupon_code


def _load_checkout_coupon(coupon_code, subtotal_amount):
    """Resolve and validate a named coupon: coupon, discount, or a 400.

    Returns ``(coupon, discount_amount, rejection)``. A coupon that fails a
    rule stops the checkout here, before the atomic creation block; the
    caller answers with ``rejection`` unchanged.
    """
    try:
        coupon = Coupon.objects.get(code__iexact=coupon_code)

    except Coupon.DoesNotExist:
        return (
            None,
            Decimal("0.00"),
            Response(
                {"error": "Invalid coupon code"}, status=status.HTTP_400_BAD_REQUEST
            ),
        )

    if not coupon.active:
        return (
            None,
            Decimal("0.00"),
            Response(
                {"error": "This coupon is inactive"},
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )

    now = timezone.now()

    if now < coupon.valid_from:
        return (
            None,
            Decimal("0.00"),
            Response(
                {"error": "This coupon is not active yet"},
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )

    if now > coupon.valid_until:
        return (
            None,
            Decimal("0.00"),
            Response(
                {"error": "This coupon has expired"},
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )

    if coupon.usage_limit is not None and coupon.used_count >= coupon.usage_limit:
        return (
            None,
            Decimal("0.00"),
            Response(
                {"error": "This coupon has reached its usage limit"},
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )

    if subtotal_amount < coupon.minimum_order_amount:
        return (
            None,
            Decimal("0.00"),
            Response(
                {
                    "error": "Minimum order amount is required",
                    # The envelope moves this key into details and re-serializes
                    # with DRF's encoder, which would turn a bare Decimal into
                    # the float 5000.0; the wire contract for money is the same
                    # 2-dp string here as everywhere else in the payload.
                    "minimum_order_amount": money_string(coupon.minimum_order_amount),
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )

    # The coupon-type math lives in one helper so this order row and the
    # public preview can never disagree about what a coupon is worth
    # (F-11). A free-shipping coupon returns 0.00 merchandise discount
    # here; its waiver is applied to the delivery quote below instead.
    discount_amount = coupon_merchandise_discount(coupon, subtotal_amount)

    return coupon, discount_amount, None


def _resolve_checkout_coupon(body, cart, subtotal_amount):
    """The coupon and merchandise discount for this submission, or a 400."""
    coupon_code = _checkout_coupon_code(body, cart)

    if not coupon_code:
        return None, Decimal("0.00"), None

    return _load_checkout_coupon(coupon_code, subtotal_amount)


def _reserve_coupon(coupon, order):
    """Hold one usage slot for ``order`` before it can reach capture.

    # task: T-20261118-002
    ``_load_checkout_coupon`` reads ``used_count`` without a lock, so two
    submissions can both see the last slot free. The Coupon row is the
    serialization point: lock it after the order row this transaction just
    inserted (the writers' Order -> Coupon order) and count the orders that
    still hold the coupon. Those rows ARE the reservations, so a rollback
    releases the slot without restoring a counter, and ``used_count`` stays
    the capture-time record of redemptions that the audit path reads.
    """
    if coupon is None:
        return

    Order.objects.select_for_update().get(pk=order.pk)
    locked_coupon = Coupon.objects.select_for_update().get(pk=coupon.pk)

    if locked_coupon.usage_limit is None:
        return

    held_by_other_orders = (
        Order.objects.filter(coupon_id=locked_coupon.pk)
        .exclude(pk=order.pk)
        .exclude(status="cancelled")
        .count()
    )
    if (
        held_by_other_orders >= locked_coupon.usage_limit
        or locked_coupon.used_count >= locked_coupon.usage_limit
    ):
        raise _CouponUsageUnavailable


def _collapse_onto_duplicate(duplicate, idempotency_key, user):
    """The 200 collapse for a duplicate, and the key adoption it may need.

    Returns None when the submission must continue to the keyed probe and
    creation.
    """
    if idempotency_key is None:
        # Benign double-submit retry, so INFO: a WARNING here would
        # spam the log on every impatient re-click (same judgement
        # as verify_payment's already-processed skip).
        logger.info(
            "Checkout dedup: order %s replayed (identical pending submission)",
            duplicate.id,  # task: T-20261118-009: order pk, not customer name
        )

        # Scoped by `owner` above AND by `_same_purchase`, so this
        # can only ever hand back a submission that agrees with the
        # caller's own identity, on the complete shipping payload,
        # on the delivery option, on the cart contents and on the
        # coupon, inside the dedup window -- five independent
        # agreements, which is what "the same purchase attempt"
        # means for a double-click or a client retry.
        return _checkout_response(duplicate, status.HTTP_200_OK)

    # [R-9.3.14] A keyed retry may adopt only a still-keyless
    # candidate. A candidate another key already owns is a
    # different submission, so it falls through to the keyed probe
    # (and creation) instead of resolving this key to that order.
    if duplicate.idempotency_key is None:
        User.objects.select_for_update().get(pk=user.pk)
        duplicate.refresh_from_db(fields=["idempotency_key"])

        # task: IDEM-20261117-local-001
        # The user-row lock excludes every other keyed writer, so
        # a key that already owns an order is read here, never
        # re-bound: the keyed probe that follows answers with that order.
        if duplicate.idempotency_key is None and not (
            Order.objects.filter(user=user, idempotency_key=idempotency_key).exists()
        ):
            duplicate.idempotency_key = idempotency_key
            duplicate.save(update_fields=["idempotency_key"])

    return None


def _replay_by_key(user, owner, idempotency_key):
    """The keyed-replay collapse: the 200 when this key already owns an order.

    [R-9.3.14]/[R-9.3.19] SPEC-9-01: key-based replay collapse. The user
    row is locked first so two cross-session submissions carrying the same
    key serialize here -- the loser's probe runs only after the winner
    committed, which makes the probe-then-bind race-free (the
    (user, idempotency_key) unique constraint stays the last-resort
    authority). This composes behind the SPEC-21-1 guard that runs before
    it, which stays the first line of defence for keyless same-session
    double-clicks: a keyed replay collapses onto the original order
    regardless of payload drift, the dedup window, or a settled first
    attempt, and never mints a second order_number.
    """
    if idempotency_key is None:
        return None

    User.objects.select_for_update().get(pk=user.pk)

    replay = (
        Order.objects.filter(**owner, idempotency_key=idempotency_key)
        .prefetch_related("items")
        .first()
    )

    if replay is not None:
        # An authenticated caller proved who they are at login,
        # so (user, key) is a strong pair and SPEC-9-01's
        # "collapse regardless of payload drift" stands for
        # them. (The keyed GUEST branch that additionally
        # demanded whole-purchase agreement died with the
        # TIER1-GUEST-DEADEND refusal.)
        #
        # Benign keyed retry, so INFO: same judgement as the dedup
        # guard's collapse log above.
        logger.info(
            "Checkout idempotency: order %s replayed (Idempotency-Key)",
            replay.id,  # task: T-20261118-009: order pk, not customer name
        )

        return _checkout_response(replay, status.HTTP_200_OK)

    return None


def _price_delivery(user, body, cart_items, coupon, merchandise_total):
    """Price the delivery inside the caller's atomic block.

    Returns ``(shipping_quote, shipping_amount, shipping_method_id,
    shipping_method_code, total_amount)``.

    [R-1.07] SPEC-1-B05: price the delivery, HERE - after both collapse
    guards and inside the same atomic block as the order row, so a
    rolled-back checkout takes the shipping cost with it and a collapse can
    never reprice an order that already exists.

    The inputs are the destination the customer typed and the delivery
    OPTION they picked (a method code). Nothing the client sent is read as
    an amount - `shipping_amount`, `shipping_cost`, `shipping` and
    `total_amount` have no reader anywhere in this path, so a forged
    `shipping_amount: 0` cannot reach the total. `lock=True` row-locks the
    matched rate, so a staff edit committing mid-checkout cannot tear the
    amount the order is charged.
    """
    shipping_quote = quote_shipping(
        region=body.get("state"),
        postal_code=body.get("pincode"),
        merchandise_total=merchandise_total,
        method_code=_requested_shipping_code(body),
        lock=True,
    )

    # A quote of None means the store has configured no shipping at
    # all - a real zero charge recorded as one, never a rate of zero
    # standing in for "we could not price this".
    shipping_amount = (
        shipping_quote.amount if shipping_quote is not None else Decimal("0.00")
    )
    # A free delivery charge comes from either waiver, and both land
    # on the ONE order-level amount: a merchant-set product flag
    # (SPEC-C-04) or a free-shipping coupon. Convergence here is what
    # keeps the pair from double-counting: there is a single charge
    # to zero, so applying both is byte-identical to applying one.
    #
    # ANY qualifying merchandise line is enough (the spec's first
    # option): the cart has one delivery cost, and a line advertised
    # as shipping free is the reason to waive it. Flip the predicate
    # to ``all(...)`` to require every line to qualify; nothing else
    # in this block changes.
    #
    # The quote above still ran, so an unserviceable destination is
    # still refused -- these waivers waive the charge, not the
    # service -- and only the AMOUNT is zeroed. The quoted method is
    # retained exactly as before, which is what tells this zero apart
    # from the no-shipping-configured zero where the method is null.
    free_delivery = any(item.product.free_shipping for item in cart_items) or (
        coupon is not None and coupon.discount_type == Coupon.FREE_SHIPPING
    )
    if free_delivery:
        shipping_amount = Decimal("0.00")
    shipping_method_id = (
        shipping_quote.method_id if shipping_quote is not None else None
    )
    # [R-8.13] The delivery-option label is snapshotted beside the FK
    # (spec 8.3: an order retains what was actually purchased), so the
    # order still names the option it was charged for after the method
    # row is deleted. Empty exactly when the FK is null for the other
    # reason - the store had no shipping configured - which is what
    # keeps those two cases tellable apart.
    shipping_method_code = (
        shipping_quote.method_code if shipping_quote is not None else ""
    )
    if shipping_quote is None:
        # The silent zero, made audible (SPEC-1-B05): this order is
        # being accepted with no delivery charge because the store has
        # no active shipping method, and nothing else in the request
        # or the response says so. WARNING rather than INFO because
        # the cost is silent revenue, and one line per order placed is
        # a rate an operator can act on - a merchant who sees it can
        # run `manage.py seed_shipping_methods`. The 201 is unchanged:
        # refusing the order is a different decision than this task
        # makes, and a store that has not configured shipping yet is
        # not broken. `shipping_amount` stays the honest 0.00.
        logger.warning(
            "Checkout priced with NO shipping charge for user=%s: the store "
            "has no active shipping method configured (seed one with "
            "`manage.py seed_shipping_methods` to start charging)",
            user.pk,  # task: T-20261118-009: pk, not username
        )
    total_amount = quantize_money(merchandise_total + shipping_amount)

    return (
        shipping_quote,
        shipping_amount,
        shipping_method_id,
        shipping_method_code,
        total_amount,
    )


def _create_order_row(user, guest_email, body, coupon, discount_amount, shipping):
    """Mint the order row, retrying lost order-number races in savepoints.

    [R-8.4] The order number is minted inside the caller's atomic block, so
    a rolled-back checkout never burns a number. Each attempt runs in a
    savepoint: a lost race (another connection committed the same candidate
    first) rolls back only the failed insert and the next turn regenerates
    from committed state. Same-session replays never reach this loop -- the
    dedup guard above returns first.
    """
    (
        _quote,
        shipping_amount,
        shipping_method_id,
        shipping_method_code,
        total_amount,
    ) = shipping
    for attempt in range(ORDER_NUMBER_ATTEMPTS):
        candidate = _generate_order_number()
        try:
            with transaction.atomic():
                order = Order.objects.create(
                    user=user,
                    # The two-owner constraint: an account row carries
                    # the empty guest_email (never NULL) and no
                    # guest_token (TIER1-GUEST-DEADEND: no token is
                    # ever minted while the refusal stands).
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
            # Lost the number race: the unique constraint rejected the
            # candidate, so the savepoint above rolled the failed
            # insert back and this transaction stays usable for the
            # retry. The bound is a safety net, not the expected path:
            # one retry converges because the collision window is a
            # single insert.
            if attempt == ORDER_NUMBER_ATTEMPTS - 1:
                raise
            continue
        break
    return order


def _record_order_status_event(user, order):
    """Open the order's transition trail: none -> pending, in the caller's tx.

    [R-10.12]/[R-10.17] SPEC-10-02: creation is the lifecycle's first
    transition (no source state -> pending), so the audit trail opens
    here — inside the same outer atomic block as the order row, the
    minted number, the snapshots and the key binding. A rolled-back
    checkout leaves no order and no event; the actor is the customer
    who placed the order (OrderStatusEvent.actor stays nullable for
    the guest rows that predate the TIER1-GUEST-DEADEND refusal,
    like StockMovement.created_by).
    """
    OrderStatusEvent.objects.create(
        order=order,
        from_status=None,
        to_status=order.status,
        actor=user,
        trigger=TRIGGER_ORDER_CREATE,
    )


def _bind_order_key(order, idempotency_key):
    """Bind the submission key inside the atomic block that minted the order.

    [R-9.3.14] SPEC-9-01: bind the submission key to the freshly
    minted order inside this same transaction, so a later replay's
    probe above finds it and collapses. The write is an UPDATE of the
    row this transaction just created, after the probe proved no
    committed order holds (user, key) and with the user-row lock
    excluding a concurrent keyed writer -- so the unique constraint
    cannot reject here.
    """
    if idempotency_key is not None:
        order.idempotency_key = idempotency_key
        order.save(update_fields=["idempotency_key"])


def _snapshot_order_items(order, cart_items):
    """Freeze each cart line onto the order as an OrderItem row.

    Inventory, coupon usage, and cart cleanup occur only after the payment
    provider confirms this specific order, so creation snapshots what was
    bought without touching any of them.
    """
    for cart_item in cart_items:
        product = cart_item.product
        quantity = cart_item.quantity

        price = product.price

        item_subtotal = price * quantity

        OrderItem.objects.create(
            order=order,
            product=product,
            product_name=product.name,
            # [R-8.13] Freeze the catalogue identity the customer bought:
            # no variant-selection input exists yet (CartItem is
            # product-only, picking rides SPEC-3-21/SPEC-6-08) and the
            # product carries no product-level SKU, so sku snapshots
            # empty and variant_name mirrors the product name. Set once
            # here; no later save path mutates them.
            sku="",
            variant_name=product.name,
            price=price,
            quantity=quantity,
            subtotal=item_subtotal,
        )


def _record_order_trail(user, order, cart_items, coupon, shipping):
    """The ORDER_CREATED business-event row, the atomic block's last writer.

    [R-7.20] Business-event trail: the order's creation is recorded in
    the same transaction as the order rows, so a rolled-back checkout
    leaves no phantom trail row and a committed order is never
    trail-less.
    """
    shipping_quote = shipping[0]
    shipping_amount = shipping[1]

    AuditEvent.record(
        AuditEvent.EventType.ORDER_CREATED,
        actor=user,
        order=order,
        detail={
            "order_id": order.id,
            "total_amount": str(order.total_amount),
            "coupon": coupon.code if coupon else None,
            "item_count": len(cart_items),
            # [R-1.07] What the order was charged to deliver it, in the
            # same trail row: the delivery charge is money like any other
            # and a reconciliation asks "what did this order cost" of
            # the trail, not only of the order row.
            "shipping_method": shipping_quote.method_code
            if shipping_quote is not None
            else None,
            "shipping_amount": str(shipping_amount),
        },
    )


@api_view(["POST"])
# AllowAny is deliberate: the refusal is ours to write, not DRF's generic 401.
# An authenticated caller is still scoped to their own orders below.
@permission_classes([AllowAny])
# Public mutating endpoint sharing the cart budget: checkout is the terminal
# mutation of that same session-cart funnel.
@throttle_scope("cart")
@_maintenance_gated
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

    return _place_order(request, idempotency_key)


def _place_order(request, idempotency_key):
    """Read and validate the submission, then hand it to the atomic writer."""

    # [R-1.13] Who this submission belongs to, settled once and reused by
    # every writer below (dedup probe, key binding, the row, the status
    # event and the stock holds), so no two of them can disagree about it.
    # Only an account caller is past the gate, so the owner is just the
    # user; a body-supplied guest_email is ignored outright rather than
    # stored (the Meta constraint allows exactly one owner per row, and an
    # account row carries the empty guest_email, never NULL).
    user = request.user
    guest_email = ""
    owner = {"user": user}

    cart, cart_items, rejection = _resolve_checkout_cart(request)
    if rejection is not None:
        return rejection

    body = _object_body(request.data)

    rejection = _validate_checkout_payload(body)
    if rejection is not None:
        return rejection

    delisted, unavailable, subtotal_amount = _scan_cart_lines(cart_items)

    rejection = _cart_line_rejection(delisted, unavailable)
    if rejection is not None:
        return rejection

    coupon, discount_amount, rejection = _resolve_checkout_coupon(
        body, cart, subtotal_amount
    )
    if rejection is not None:
        return rejection

    # Still the MERCHANDISE total; shipping is added below so a replay never
    # reprices an order that already exists.

    merchandise_total = subtotal_amount - discount_amount

    return _create_order_atomically(
        user,
        guest_email,
        owner,
        cart,
        cart_items,
        body,
        coupon,
        discount_amount,
        merchandise_total,
        idempotency_key,
    )


def _create_order_atomically(
    user,
    guest_email,
    owner,
    cart,
    cart_items,
    body,
    coupon,
    discount_amount,
    merchandise_total,
    idempotency_key,
):
    """Create the order and its rows, or return the collapse/refusal response.

    The whole block is wrapped in the try so a shipping refusal PROPAGATES
    out of it and rolls it back whole, rather than being answered from
    inside it: a return from inside an atomic block commits whatever the
    block had already written, and "shipping could not be priced" must
    never leave a partial order behind (an order with no shipping cost is
    a free shipment).
    """
    try:
        with transaction.atomic():
            # Serialize same-session submissions on the cart row, so the loser
            # re-runs dedup after the winner commits; no new lock-order cycle.
            cart = Cart.objects.select_for_update().get(pk=cart.pk)

            duplicate = _find_duplicate_pending_order(owner, cart_items, coupon, body)

            if duplicate is not None:
                rejection = _collapse_onto_duplicate(duplicate, idempotency_key, user)
                if rejection is not None:
                    return rejection

            rejection = _replay_by_key(user, owner, idempotency_key)
            if rejection is not None:
                return rejection

            shipping = _price_delivery(
                user, body, cart_items, coupon, merchandise_total
            )

            order = _create_order_row(
                user, guest_email, body, coupon, discount_amount, shipping
            )

            _reserve_coupon(coupon, order)

            # Bind the cart to the order in the same transaction so the capture
            # path (even a later webhook with no session) can find and empty it.
            cart.order = order
            cart.save(update_fields=["order"])

            _record_order_status_event(user, order)

            _bind_order_key(order, idempotency_key)

            _snapshot_order_items(order, cart_items)

            # Mint the checkout's holds in this same block (helper above).
            _mint_order_reservations(order, cart_items, user)

            _record_order_trail(user, order, cart_items, coupon, shipping)

    except ShippingUnavailable as exc:
        # The transaction rolled back whole: 400 with the reason, never a 500 and
        # never a silent free shipment.
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except _CouponUsageUnavailable as exc:
        # The order row is the reservation and it rolled back with the block, so
        # the loser is refused before any provider intent exists.
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
# Public: the coupon is validated against the guest's session cart.
@permission_classes([AllowAny])
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

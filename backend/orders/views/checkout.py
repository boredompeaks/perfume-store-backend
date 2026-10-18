"""Checkout: the order lifecycle's opening move, plus the two public reads that
bracket it (the guest's own order, and the coupon preview).

One of six modules split out of the former single-file `orders/views.py`. The
two collapse guards (the SPEC-21-1 accidental-duplicate window and SPEC-9-01's
header-keyed idempotency) live here because they are properties of ORDER
CREATION, not of payment.

TIER1-GUEST-DEADEND: checkout is ACCOUNT-ONLY. Full guest checkout is deferred
(owner decision 2026-10-09, recorded in ``backend/docs/run/TIER1-launch.md``:
SPEC-3-02 off the Tier-1 list), and what the store did in the meantime was
worse than not having it - an anonymous POST minted an order that could never
be paid (the payment endpoints are account-scoped) while it held a stock
reservation and carried the coupon FK: a dead end that parked saleable
inventory. ``create_order`` now refuses every anonymous submission with ONE
uniform answer before anything is read or written (``_guest_checkout_refused``).
The guest ORDER READ below (``guest_order_detail``) stays: rows minted before
the refusal are still data, and a later section rebuilding guest checkout
around a real checkout session (spec route rule 3) will decide their fate.
"""

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
from common.models import AuditEvent
from common.money import quantize_money

# [SPEC-12-02] StockReservation rides the existing products.models import
# line (insertion-only style): the checkout lifecycle mints them in
# create_order.
from products.models import StockReservation

# [R-1.07] SPEC-1-B05: the server-side shipping price. The client may name a
# delivery OPTION here and nothing else - never an amount - which is what keeps
# the shipping cost out of the client's hands.
from shipping.pricing import ShippingUnavailable, quote_shipping

from ..models import Coupon, Order, OrderItem, OrderStatusEvent
from ..serializers import OrderSerializer

# [R-10.12] SPEC-10-02: transition-audit writers. Own import line so every
# hunk above stays insertion-only.
from ..state import TRIGGER_ORDER_CREATE

logger = logging.getLogger(__name__)

# [R-9.3.14] SPEC-9-01: header-keyed checkout idempotency. The cap mirrors
# the Order.idempotency_key column width, so an oversized value is rejected
# with a 400 here instead of a database error at insert time.
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX_LENGTH = 128


def _object_body(data):
    """The write body as a dict, or ``{}`` when it is not a JSON object.

    A JSON array / scalar / null body parses fine but has no ``.get`` to
    call, so reading keys off it raised AttributeError — a 500 on a public
    write endpoint (F7-B1). Reading it as the empty object routes every
    such body into the endpoint's own missing-key 400 below
    (first required shipping field / coupon code required), byte-identical
    to posting ``{}``; form posts are QueryDict (a dict subclass) and are
    unaffected. Same helper as ``cart.views._object_body``, inlined
    because cart.views imports orders.views — importing it back here
    would be an order-dependent circular import."""
    return data if isinstance(data, dict) else {}


# ==================================
# Guest checkout refusal (TIER1-GUEST-DEADEND)
# ==================================

# How a guest (legacy row) presents its read credential on the guest order
# lookup below. A header, never a query param: a URL ends up in access logs,
# browser history and Referer, and this value is the sole credential for
# someone else's order details. No NEW token is minted while the refusal
# stands - the column, the constraint and the read all survive for rows
# created before it.
GUEST_TOKEN_HEADER = "X-Guest-Order-Token"

# The read accepts nothing wider than the column it is matched against
# (64), so an oversized value is refused before it can reach the index.
GUEST_TOKEN_MAX_LENGTH = 64


def _guest_checkout_refused():
    """The ONE answer every anonymous checkout submission gets.

    The dead-end this gate closes: an anonymous POST used to mint a guest
    order that could never be paid (create_payment/verify_payment are
    account-scoped), while the row held a stock reservation and the coupon
    FK - saleable inventory parked on a purchase nobody could complete. The
    gate therefore runs before the cart is read, the coupon resolved or the
    atomic block opens: the refusal creates NOTHING - no order, no
    reservation, no audit row, no coupon attachment, no burnt order number.

    ONE body for every reason an anonymous submission might otherwise fail
    (missing, invalid or over-length guest_email; no cart; empty cart; a
    spent Idempotency-Key; an unserviceable destination): a differentiated
    answer would let a caller probe which inputs any guest path accepts
    (conventions.md: uniform responses on anonymous flows, no existence
    leaks). The SPEC-9-03 envelope middleware wraps this into the store-wide
    {"error", "code", "details"} shape, and the status family label is
    "permission_denied" on every reason alike.
    """
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
    """A 400 when ``value`` is wider than the column it is written to.

    SQLite ignores a ``varchar(n)`` width, so an over-length value saves
    cleanly in the test suite and raises ``DataError`` on the production
    Postgres — a 500 on a public endpoint rather than a 400. The limit is
    read off the model field, so the refusal and the column cannot drift
    apart (and a field with no width, ``address``'s TextField, passes).
    """
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
    """The one answer every guest lookup failure gets.

    No token, a wrong token, an unknown order number and a token that belongs
    to somebody else's order are byte-identical here: a different answer for
    any of them would turn this endpoint into an oracle for guessing which
    order numbers exist (conventions.md: uniform responses on anonymous
    flows, no existence leaks). 404 rather than 403 for the same reason —
    "forbidden" would confirm the order is real.
    """
    return Response(
        {"error": "Order not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def _checkout_response(order, status_code):
    """The checkout body: the order as the serializer shapes it.

    No guest credential is disclosed here because none exists to disclose:
    while the TIER1-GUEST-DEADEND refusal stands this view mints no guest
    rows, and ``OrderSerializer`` never carries ``guest_token`` - so even a
    legacy guest row cannot leak its read credential through any response
    this module produces.
    """
    return Response(OrderSerializer(order).data, status=status_code)


# The shipping fields that identify a checkout submission; must stay in step
# with the required_fields list inside create_order (both describe the same
# payload), which is why the duplicate guard reads them from the request
# exactly the way create_order persists them.
_SHIPPING_FIELDS = ("full_name", "phone", "address", "city", "state", "pincode")


def _order_lines(cart_items):
    """Order identity as sorted (product_id, quantity) pairs, so the
    duplicate comparison does not depend on cart-line iteration order."""
    return sorted((item.product_id, item.quantity) for item in cart_items)


def _same_purchase(order, cart_items, coupon, payload):
    """Does ``order`` describe the purchase this submission is making?

    Four agreements beyond owner identity: the cart lines (order-
    independent, via ``_order_lines``), the coupon, the complete
    shipping payload, and the delivery option. The owner filter already
    checked identity; this is the "same purchase attempt" test the
    keyless dedup guard asks, so it is asked once, here.

    Historically this also gated the keyed GUEST replay (a guest proved
    only an email and a low-entropy key, so the whole purchase had to
    agree). That branch died with the TIER1-GUEST-DEADEND refusal: an
    authenticated caller's (user, key) pair is strong enough that
    SPEC-9-01's "collapse regardless of payload drift" stands for them,
    and no anonymous caller gets past the gate at all.
    """
    if _order_lines(order.items.all()) != _order_lines(cart_items):
        return False
    if order.coupon_id != (coupon.pk if coupon else None):
        # a deliberate coupon difference is a new purchase, not a retry
        return False
    if not _same_shipping_choice(order, payload):
        return False
    shipping = tuple(payload.get(field) for field in _SHIPPING_FIELDS)
    return tuple(getattr(order, field) for field in _SHIPPING_FIELDS) == shipping


# [R-1.07] The client-supplied delivery OPTION (SPEC-1-B05): a method code,
# never an amount. Read through one helper so the guard and the pricing call
# below cannot disagree about what the client asked for.
SHIPPING_METHOD_FIELD = "shipping_method"


def _requested_shipping_code(payload):
    """The delivery option this submission names, normalized, or None.

    An absent or blank value is None: the client offered no choice and the
    server priced the destination itself. Normalizing here is what lets the
    agreement test below compare a client string against a stored slug
    without either side's whitespace deciding the answer.
    """
    raw = payload.get(SHIPPING_METHOD_FIELD)
    if raw is None:
        return None
    return str(raw).strip() or None


def _same_shipping_choice(order, payload):
    """Does the delivery option this submission names agree with the order's?

    A submission that NAMES a method must name the one the order was priced
    with, so a keyless duplicate cannot collapse onto an order priced a
    different way while supplying everything else the guard checks.

    A submission that names NOTHING agrees with whatever the server chose,
    because the server chose it: pricing happens after the collapse guard,
    so the order already holds a resolved method and a replay that never
    expressed a preference cannot disagree with it. This is what the SPEC-1-
    B05 client-amount property rests on - a collapse returns the ORIGINAL
    order, with its own persisted shipping_amount and total_amount, so no
    replay can reprice anything whatever it posts.
    """
    requested = _requested_shipping_code(payload)
    if requested is None:
        return True
    priced_as = order.shipping_method.code if order.shipping_method_id else None
    return priced_as == requested


def _find_duplicate_pending_order(owner, cart_items, coupon, payload):
    """[R-21.2.6] The accidental-duplicate window: checkout never clears the
    cart (cleanup happens after payment confirmation), so a double-click or
    client retry resubmits the byte-identical payload while the first order
    is still payable -- which used to mint a second payable order and with
    it a second charge target. Returns the owner's recent pending order with
    the identical payload, or None to proceed with creation.

    Deliberately narrower than SPEC-9-01: the header-keyed idempotency
    (Idempotency-Key + unique-by-key store, covering cross-session
    simultaneous duplicates) stays there. This guard only closes the
    accidental same-session window, and only for orders that are still
    payable -- verify_payment's already-processed gate remains the authority
    past that point."""
    cutoff = timezone.now() - timedelta(seconds=settings.CHECKOUT_DEDUP_WINDOW_SECONDS)
    candidates = (
        Order.objects.filter(
            **owner,
            status="pending",
            created_at__gte=cutoff,
        )
        # payable gate mirrors verify_payment: a pending order with a
        # payment id attached has already been taken past this point
        .filter(Q(razorpay_payment_id__isnull=True) | Q(razorpay_payment_id=""))
        .prefetch_related("items")
        .order_by("-created_at")
    )
    for candidate in candidates:
        if _same_purchase(candidate, cart_items, coupon, payload):
            return candidate
    return None


# ==================================
# Order number generation (R-8.4)
# ==================================

# Bounded retries: a checkout still losing the order-number race after this
# many attempts indicates something is deeply wrong, so it fails loudly (the
# whole checkout transaction rolls back) instead of looping forever.
ORDER_NUMBER_ATTEMPTS = 5


def _current_year():
    """The order-number year bucket: the sequence restarts each January 1st
    (R-8.4), so this is the only clock the format depends on."""
    return timezone.now().year


def _generate_order_number():
    """Mint a customer-facing order number with a random component.

    The year bucket keeps the display stable (ORD-YYYY-XXXXXXXX); the random
    suffix comes from ``secrets`` (never ``random``) so it is not
    predictable. The unique constraint on ``Order.order_number`` remains the
    concurrency authority and the caller retries on IntegrityError."""
    year = _current_year()
    return f"ORD-{year}-{secrets.token_hex(4).upper()}"


def _mint_order_reservations(order, lines, user):
    """[R-12.6] SPEC-12-02 §12.1 step 3: one time-limited hold per order
    line, minted inside the caller's atomic block so a rolled-back checkout
    never leaves a hold behind.

    Deliberately lock-free and rule-free on stock: create_order's advisory
    gate already validated live availability, and verify_payment's locked
    sufficiency re-check stays the oversell authority (§12.2's
    pay-then-409 residual for two concurrent last-unit checkouts is
    by-design). The TTL comes from expiry_from_now(), the single
    env-driven source (RESERVATION_TTL)."""
    for line in lines:
        try:
            # Savepoint: a lost (order, product) mint race rolls back only
            # this insert and leaves the outer transaction usable for the
            # re-target below (same shape as the order-number mint loop).
            with transaction.atomic():
                StockReservation.objects.create(
                    product=line.product,
                    order=order,
                    owner=user,
                    quantity=line.quantity,
                    expires_at=StockReservation.expiry_from_now(),
                )
        except IntegrityError:
            # The (order, product) unique constraint is the concurrency
            # authority: a replay that slipped past both collapse guards
            # re-targets the existing hold instead of double-creating it
            # (§12.1: one hold per checkout line — a duplicate row would
            # double-count reserved stock). Re-targeting re-arms the hold
            # for this attempt: the retried line's quantity, a fresh TTL,
            # active again.
            reservation = StockReservation.objects.select_for_update().get(
                order=order, product=line.product
            )
            reservation.quantity = line.quantity
            reservation.expires_at = StockReservation.expiry_from_now()
            reservation.status = StockReservation.Status.ACTIVE
            reservation.save(update_fields=["quantity", "expires_at", "status"])


@api_view(["POST"])
# AllowAny is deliberate (conventions.md:26) even though guests are refused:
# the refusal is OURS to write, not the auth stack's. An IsAuthenticated
# permission would answer the anonymous caller DRF's generic 401 body, which
# says nothing about guest checkout; the explicit gate below answers the one
# clear, uniform message instead. An authenticated caller is still scoped to
# their own orders everywhere below, and nothing here reads a body-supplied
# user id.
@permission_classes([AllowAny])
# A public MUTATING endpoint, so conventions.md:24 requires a scope. It
# shares the cart budget on purpose: checkout is the terminal mutation of
# exactly the session-cart flow those two rates already govern (add to cart
# -> apply coupon -> submit), so the whole funnel draws on one budget rather
# than two that can be spent independently. The throttle runs before the
# view body, so an anonymous caller can be rate-limited into the same 429 a
# logged-in one gets - never past it into the refusal below.
@throttle_scope("cart")
def create_order(request):

    # [TIER1-GUEST-DEADEND] FIRST line of the body, before the idempotency
    # header is even read: every anonymous submission - any payload, any
    # headers, with or without a cart - gets the one uniform refusal, so no
    # input can move a guest onto a different answer (and nothing below this
    # gate can write anything on a guest's behalf).
    if not request.user.is_authenticated:
        return _guest_checkout_refused()

    # [R-9.3.14] Honor the Idempotency-Key header when the client sends it:
    # every retry carrying the same value is the SAME submission, so the
    # atomic block below dedupes on (user, key). Keyless clients keep the
    # legacy contract (the SPEC-21-1 guard plus window). Blank means no
    # key, since an empty value carries no submission identity.
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

    if not request.session.session_key:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    session_id = request.session.session_key

    try:
        cart = Cart.objects.get(session_id=session_id)

    except Cart.DoesNotExist:
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

    # [R-1.13] The same varchar-width trap the guest email had, on the rest
    # of the shipping payload: SQLite does not enforce `full_name`'s 150 or
    # `phone`'s 15, so an over-length value is a 201 in the suite and a
    # DataError on the production Postgres. Refused here, before the atomic
    # block opens and therefore before any row exists.
    for field in _SHIPPING_FIELDS:
        length_rejection = _field_length_rejection(field, body[field])

        if length_rejection is not None:
            return length_rejection

    # SPEC-6-01 [6.2.22]: a cart line that outlasted its stock (stock can
    # drop after the item was added) must not become an order the customer
    # can pay for. This gate is advisory and read-only -- stock can still
    # change between create and pay, so verify_payment re-checks under a
    # row lock before decrementing; that remains the authoritative backstop.
    unavailable = []

    # MERCHANT-DELIST: a line whose product was taken off sale after it was
    # added is not buyable at ANY quantity, so it is collected for its own
    # refusal below rather than the stock gate -- fixing the quantity can
    # never fix it. The whole checkout is refused (never a silent skip of
    # the line, which would charge the customer for a cart they did not
    # submit), and the line itself is deliberately left in the cart:
    # removal is the customer's call -- they see the line and the refusal
    # names it -- and a relist makes the line buyable again without the
    # customer having to rebuild the cart.
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
        # The delist refusal takes precedence over the stock one: a line
        # that is not for sale is unfixable by quantity, so telling the
        # customer to "reduce the quantity" first would be wrong advice.
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

    coupon = None
    discount_amount = Decimal("0.00")

    coupon_code = body.get("coupon_code")

    # [R-9.3.5/R-9.3.6] The coupon applied to the cart persists as cart
    # state: when the checkout payload posts no explicit code, the
    # persisted one becomes the coupon source. The pre-existing validation
    # below still re-runs every rule at checkout time (state may have
    # changed since apply), so an invalidated coupon is rejected here
    # rather than silently riding the FK into the order; a deleted coupon
    # is already gone (SET_NULL) and simply applies nothing.
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
                    "minimum_order_amount": coupon.minimum_order_amount,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if coupon.discount_type == "percentage":
            # Quantize before any comparison or storage: the raw division
            # carries extra decimal places, and an unquantized discount
            # drifts the display, the audit trail and the stored 2-dp
            # order amount apart (F-11).
            discount_amount = quantize_money(
                (subtotal_amount * coupon.discount_value) / Decimal("100")
            )

            if coupon.maximum_discount is not None:
                discount_amount = min(discount_amount, coupon.maximum_discount)

        else:
            discount_amount = coupon.discount_value

        # Never discount more than subtotal
        discount_amount = min(discount_amount, subtotal_amount)

    # [R-1.07] SPEC-1-B05: still the MERCHANDISE total. The shipping charge is
    # added inside the atomic block below, once the collapse guards have had
    # their say, so a replay never reprices an order that already exists.

    merchandise_total = subtotal_amount - discount_amount

    # The whole block is wrapped in the try so a shipping refusal PROPAGATES
    # out of it and rolls it back whole, rather than being answered from
    # inside it: a return from inside an atomic block commits whatever the
    # block had already written, and "shipping could not be priced" must
    # never leave a partial order behind (an order with no shipping cost is
    # a free shipment).
    try:
        with transaction.atomic():
            # [R-21.2.6] Serialize same-session submissions on the cart row: two
            # rapid POSTs queue here, so the loser re-runs the dedup lookup after
            # the winner has committed and collapses onto the same order instead
            # of minting a second payable one. The cart row is locked only on
            # this path (verify_payment locks Order -> Products -> Coupon), so no
            # new lock-order cycle is introduced.
            cart = Cart.objects.select_for_update().get(pk=cart.pk)

            duplicate = _find_duplicate_pending_order(owner, cart_items, coupon, body)

            if duplicate is not None:
                # Benign double-submit retry, so INFO: a WARNING here would spam
                # the log on every impatient re-click (same judgement as
                # verify_payment's already-processed skip).
                logger.info(
                    "Checkout dedup: order %s replayed for %s "
                    "(identical pending submission)",
                    duplicate.id,
                    duplicate.customer_name,
                )

                # Scoped by `owner` above AND by `_same_purchase`, so this
                # can only ever hand back a submission that agrees with the
                # caller's own identity, on the complete shipping payload,
                # on the delivery option, on the cart contents and on the
                # coupon, inside the dedup window -- five independent
                # agreements, which is what "the same purchase attempt"
                # means for a double-click or a client retry.
                return _checkout_response(duplicate, status.HTTP_200_OK)

            # [R-9.3.14]/[R-9.3.19] SPEC-9-01: key-based replay collapse. The
            # user row is locked first so two cross-session submissions carrying
            # the same key serialize here -- the loser's probe runs only after
            # the winner committed, which makes the probe-then-bind below
            # race-free (the (user, idempotency_key) unique constraint stays the
            # last-resort authority). This composes behind the SPEC-21-1 guard
            # above, which stays the first line of defence for keyless
            # same-session double-clicks: a keyed replay collapses onto the
            # original order regardless of payload drift, the dedup window, or
            # a settled first attempt, and never mints a second order_number.
            if idempotency_key is not None:
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
                        "Checkout idempotency: order %s replayed for %s "
                        "(Idempotency-Key)",
                        replay.id,
                        replay.customer_name,
                    )

                    return _checkout_response(replay, status.HTTP_200_OK)

            # [R-1.07] SPEC-1-B05: price the delivery, HERE - after both
            # collapse guards and inside the same atomic block as the order
            # row, so a rolled-back checkout takes the shipping cost with it
            # and a collapse can never reprice an order that already exists.
            #
            # The inputs are the destination the customer typed and the
            # delivery OPTION they picked (a method code). Nothing the client
            # sent is read as an amount - `shipping_amount`, `shipping_cost`,
            # `shipping` and `total_amount` have no reader anywhere in this
            # path, so a forged `shipping_amount: 0` cannot reach the total.
            # `lock=True` row-locks the matched rate, so a staff edit
            # committing mid-checkout cannot tear the amount the order is
            # charged.
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
                    "Checkout priced with NO shipping charge for %s: the store "
                    "has no active shipping method configured (seed one with "
                    "`manage.py seed_shipping_methods` to start charging)",
                    user,
                )
            total_amount = quantize_money(merchandise_total + shipping_amount)

            # [R-8.4] The order number is minted inside this same atomic block,
            # so a rolled-back checkout never burns a number. Each attempt runs
            # in a savepoint: a lost race (another connection committed the same
            # candidate first) rolls back only the failed insert and the next
            # turn regenerates from committed state. Same-session replays never
            # reach this loop -- the dedup guard above returns first.
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

            # TIER1-WEBHOOK-CART: bind this cart to the order inside the
            # same atomic block that mints the order (rollback-together), so
            # the capture - browser verify or a much-later gateway webhook,
            # which has no session cookie - can find this cart and empty it
            # (orders.inventory.commit_order_sale). The cart row is already
            # locked above; the OneToOne unique constraint on Cart.order is
            # the last-resort authority should two sessions ever share a
            # cart row.
            cart.order = order
            cart.save(update_fields=["order"])

            # [R-10.12]/[R-10.17] SPEC-10-02: creation is the lifecycle's first
            # transition (no source state -> pending), so the audit trail opens
            # here — inside the same outer atomic block as the order row, the
            # minted number, the snapshots and the key binding. A rolled-back
            # checkout leaves no order and no event; the actor is the customer
            # who placed the order (OrderStatusEvent.actor stays nullable for
            # the guest rows that predate the TIER1-GUEST-DEADEND refusal,
            # like StockMovement.created_by).
            OrderStatusEvent.objects.create(
                order=order,
                from_status=None,
                to_status=order.status,
                actor=user,
                trigger=TRIGGER_ORDER_CREATE,
            )

            # [R-9.3.14] SPEC-9-01: bind the submission key to the freshly
            # minted order inside this same transaction, so a later replay's
            # probe above finds it and collapses. The write is an UPDATE of the
            # row this transaction just created, after the probe proved no
            # committed order holds (user, key) and with the user-row lock
            # excluding a concurrent keyed writer -- so the unique constraint
            # cannot reject here.
            if idempotency_key is not None:
                order.idempotency_key = idempotency_key
                order.save(update_fields=["idempotency_key"])

            # Snapshot cart items. Inventory, coupon usage, and cart cleanup occur
            # only after the payment provider confirms this specific order.

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

            # [R-12.6] SPEC-12-02 §12.1 step 3: mint the checkout's time-limited
            # holds inside this same atomic block, so a rolled-back checkout
            # never leaves a hold behind (helper above carries the semantics).
            # StockReservation.owner stays nullable for the guest rows that
            # predate the refusal; a new hold always carries the account user.
            _mint_order_reservations(order, cart_items, user)

            # [R-7.20] Business-event trail: the order's creation is recorded in
            # the same transaction as the order rows, so a rolled-back checkout
            # leaves no phantom trail row and a committed order is never
            # trail-less.
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

    except ShippingUnavailable as exc:
        # [R-1.07] The destination cannot be shipped to (or the named method
        # does not exist), so the whole transaction above rolled back: no
        # order, no reservations, no audit row, no burnt order number. 400
        # with the reason -- never a 500, and never a silent free shipment.
        return Response(
            {"error": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return _checkout_response(order, status.HTTP_201_CREATED)


# ==================================
# Guest order lookup (SPEC-1-B04, legacy rows)
# ==================================


@api_view(["GET"])
# Opened deliberately (conventions.md:26): a guest has no session to
# authorize against, which is precisely why the credential is a 256-bit token
# (minted by the pre-refusal checkout path for rows that still exist --
# TIER1-GUEST-DEADEND mints no new ones). AllowAny here does NOT mean
# unguarded - the lookup below is the guard, and it is a guard by possession,
# not by identity: it can only ever return the one row whose stored token
# equals the presented one.
@permission_classes([AllowAny])
def guest_order_detail(request, order_number=None):
    """GET /api/orders/guest/[<order_number>/] - a guest's OWN order.

    Two shapes, one view, because ``guest_token`` is UNIQUE: a token alone
    identifies exactly one order, and the order number is an optional
    cross-check on top of it. A caller who has the token can always find the
    order; a caller who has only the order number cannot, because the number
    is a guessable per-year sequence and is never the credential.

    The failure is one answer for every miss - no token, a wrong token, an
    unknown number, or somebody else's number with a valid token - so the
    endpoint confirms nothing about which orders exist (``_guest_lookup_miss``).
    The token arrives in a header, never the URL, for the same reason.
    """
    token = (request.headers.get(GUEST_TOKEN_HEADER) or "").strip()

    if not token or len(token) > GUEST_TOKEN_MAX_LENGTH:
        return _guest_lookup_miss()

    lookup = Order.objects.filter(guest_token=token)
    if order_number is not None:
        lookup = lookup.filter(order_number=order_number)

    order = lookup.first()
    if order is None:
        return _guest_lookup_miss()

    # A hit is by definition a guest row (the token is NULL everywhere else),
    # so no separate user check is needed: the filter IS the authorization.
    return Response(OrderSerializer(order).data)


# ==================================
# Coupon preview (public)
# ==================================


def _uniform_coupon_rejection():
    """Every coupon failure on the public preview returns this same body and
    status, so the response never reveals whether a code exists or why it
    was rejected (V-11 existence/validation-state leak). Differentiated
    feedback stays on the authenticated checkout, where callers are not
    brute-forcing the code space."""
    return Response(
        {"error": "Invalid coupon code"}, status=status.HTTP_400_BAD_REQUEST
    )


def validate_redeemable_coupon(code, cart):
    """Shared gate for the coupon surfaces that answer against a session
    cart: the public preview and the cart-state apply (R-9.3.5). Resolves
    `code` case-insensitively and enforces the full redeemable rule set --
    active, within the validity window, under the usage limit, and not
    below the minimum order amount -- rejecting with the ONE uniform body
    so validation state never leaks (V-11). Extracted from apply_coupon so
    the cart endpoints delegate to the same rules instead of copying them
    (SPEC-9-05).

    Returns (coupon, subtotal, None) when redeemable -- subtotal is handed
    back because the preview needs the exact figure the minimum check used
    for its discount math -- or (None, None, rejection) otherwise."""
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


@api_view(["POST"])
@throttle_scope("coupon")
def apply_coupon(request):

    code = _object_body(request.data).get("code")

    if not code:
        return Response(
            {"error": "Coupon code is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    # Resolve the cart before the coupon: otherwise a caller with no cart
    # could still probe code existence by watching for the coupon error
    # instead of the cart error. With the cart first, every cartless caller
    # gets the same answer for every code.
    if not request.session.session_key:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    session_id = request.session.session_key

    try:
        cart = Cart.objects.get(session_id=session_id)

    except Cart.DoesNotExist:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    # From here on every rejection shares one uniform response: unknown,
    # inactive, not-yet-valid, expired, usage limit, and below minimum.
    coupon, subtotal, rejection = validate_redeemable_coupon(code, cart)

    if rejection is not None:
        return rejection

    if coupon.discount_type == "percentage":
        # Same quantize parity as checkout (F-11): the preview must show
        # the exact 2-dp discount the order will store.
        discount = quantize_money((subtotal * coupon.discount_value) / Decimal("100"))

        if coupon.maximum_discount is not None:
            discount = min(discount, coupon.maximum_discount)

    else:
        discount = coupon.discount_value

    # Never allow discount greater than subtotal
    discount = min(discount, subtotal)

    final_total = subtotal - discount

    return Response(
        {
            "coupon": coupon.code,
            "subtotal": subtotal,
            "discount": discount,
            "final_total": final_total,
        }
    )

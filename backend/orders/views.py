import logging
import secrets
from datetime import timedelta
from decimal import Decimal

import razorpay
from django.conf import settings
from django.contrib.admin.models import ADDITION, CHANGE
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_scope
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from cart.models import Cart
from common import notifications
from common.audit import log_api_action
from common.models import AuditEvent
from common.money import quantize_money
from common.permissions import (
    HasOrdersCancel,
    HasOrdersFulfill,
    HasOrdersRead,
    HasRefundsCreate,
    user_has_capability,
)

# [R-1.16] SPEC-1-B07d: the merchant-configurable return window the
# eligibility gate honours. ops.models imports nothing from orders, so this is
# a leaf import and cannot cycle; it is named in full rather than aliased so
# the reader of ``_return_window_days`` can see where the policy comes from.
# ``CLOSED_RETURN_WINDOW_DAYS`` rides the same import (SPEC-1-B07f-a): it is
# part of that window's meaning, and the gate compares the resolved number
# against it BEFORE it does any date arithmetic.
from ops.models import CLOSED_RETURN_WINDOW_DAYS, SiteSettings

# [SPEC-12-02] StockReservation rides the existing products.models import
# line (insertion-only style): the checkout lifecycle mints them in
# create_order and transitions them in verify_payment / admin_order_cancel.
from products.models import StockMovement, StockReservation, products

# [R-1.07] SPEC-1-B05: the server-side shipping price. Imported as its own
# line so every hunk in this file stays insertion-only. The client may name a
# delivery OPTION here and nothing else - never an amount - which is what keeps
# the shipping cost out of the client's hands.
from shipping.pricing import ShippingUnavailable, quote_shipping

# [R-10.16] SPEC-10-05: the per-transition side-effect contract (one
# dispatch point, shared with the admin writers).
from .events import notify_transition

# [R-10.1] The order machine (transition table, gate, fulfilment step map)
# lives in orders.state — the single source; views only consume it.
# [R-1.14] SPEC-1-05: the refund row and the gateway seam the refund writer
# drives. Own import lines so every hunk in this file stays insertion-only.
# [R-10.12] SPEC-10-02: transition-audit writers. Own import lines so every
# hunk in this file stays insertion-only.
# [R-1.16] SPEC-1-B07a: the return-request row and the open-status tuple its
# duplicate guard reads. Own import line so every hunk above stays
# insertion-only.
from .models import (
    RETURN_OPEN_STATUSES,
    Coupon,
    Order,
    OrderItem,
    OrderStatusEvent,
    Refund,
    ReturnRequest,
)
from .refunds import RefundGatewayError, refund_payment
from .serializers import OrderSerializer, ReturnRequestSerializer

# [R-10.1] SPEC-10-01b: dimension mappings for the writers. Kept as its own
# line so every hunk in this file stays insertion-only.
# [R-10.4] SPEC-10-04: the failed-verify audit trigger + the
# payment-dimension transition gate. Own import lines so every hunk in
# this file stays insertion-only.
from .state import (
    ADMIN_FULFILMENT_NEXT,
    ALLOWED_TRANSITIONS,
    CAPTURED_MONEY_PAYMENT_STATUSES,
    FULFILMENT_QUEUE_STATUSES,
    TRIGGER_ADMIN_API_CANCEL,
    TRIGGER_ADMIN_API_FULFIL,
    TRIGGER_ORDER_CREATE,
    TRIGGER_PAYMENT_FAILED,
    TRIGGER_PAYMENT_VERIFY,
    fulfilment_for_status,
    payment_for_status,
    payment_transition_allowed,
    precondition_failures,
    transition_allowed,
)

logger = logging.getLogger(__name__)
# ==================================
# Order List
# ==================================

# [R-9.3.14] SPEC-9-01: header-keyed checkout idempotency. The cap mirrors
# the Order.idempotency_key column width, so an oversized value is rejected
# with a 400 here instead of a database error at insert time.
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX_LENGTH = 128

# SPEC-9-04 [R-9.2.14]: history page size. Spec §9.2 prescribes the
# order-history endpoint without pinning a page size, so the default is
# deployment config (conventions.md: no hardcoded thresholds), capped for
# ?page_size callers so a client cannot request unbounded pages.
HISTORY_PAGE_SIZE_QUERY_PARAM = "page_size"

# SPEC-1-B07b [R-1.16]: the same query param name the order-history listing
# accepts, for the returns listing that replaces the interim bare enumeration.
# One name across both account listings is deliberate: a storefront page that
# paginates orders should be able to paginate returns with the same query string
# and get the same envelope.
RETURNS_PAGE_SIZE_QUERY_PARAM = HISTORY_PAGE_SIZE_QUERY_PARAM


def _history_page_size(raw):
    """Resolve the ?page_size query param: an integer in
    [1, ORDER_HISTORY_MAX_PAGE_SIZE]; anything unparseable, non-positive or
    over the cap falls back to the configured default / cap respectively."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return settings.ORDER_HISTORY_PAGE_SIZE
    if value < 1:
        return settings.ORDER_HISTORY_PAGE_SIZE
    return min(value, settings.ORDER_HISTORY_MAX_PAGE_SIZE)


# ==================================
# [R-1.13] SPEC-1-B04: guest checkout
# ==================================

# Spec line 74 gives the guest one role: "browse products and optionally
# check out without an account", and spec 9.3 requires a checkout session to
# be bound "to the correct customer or guest session". The guest's session is
# the cart it built; its durable identity is the pair below.

# How the guest presents the token on a read. A header, never a query param:
# a URL ends up in access logs, browser history and Referer, and this value is
# the sole credential for someone else's order details.
GUEST_TOKEN_HEADER = "X-Guest-Order-Token"

# 32 bytes of CSPRNG entropy (secrets, never random) render as 43 URL-safe
# characters. The column is 64 wide, so this leaves headroom for a format
# change without a migration, and the read below rejects anything wider
# before it can reach the index.
GUEST_TOKEN_BYTES = 32
GUEST_TOKEN_MAX_LENGTH = 64


def _mint_guest_token():
    """A fresh guest credential.

    ``secrets`` is the whole point: the token is the only thing standing
    between a stranger and a stranger's address, so a predictable one (a
    counter, a uuid4 the caller can correlate, ``random``) would make guest
    checkout an order-enumeration oracle.
    """
    return secrets.token_urlsafe(GUEST_TOKEN_BYTES)


def _canonical_guest_email(raw):
    """The ONE form of a guest address this store writes and matches on.

    Surrounding whitespace is a typo, not a different mailbox, and RFC 5321
    makes the domain case-insensitive (local-part case sensitivity is
    deprecated in practice: no mainstream mailbox treats
    ``Victim@Corp.Example`` and ``victim@corp.example`` as two people).
    Stored un-normalized, those spellings are two different strings to the
    owner filter below, so one buyer ends up with two addressable order rows
    and the (guest_email, idempotency_key) constraint stops being an
    authority across them. Folding here — at the boundary, before the row
    exists — makes the identity single-valued.

    ``casefold`` over ``lower`` because it is the aggressive, idempotent
    fold: the only thing this value is used for is scoping and matching, so
    folding two spellings together is always the safe direction, and the
    equality classes never split.
    """
    return (raw or "").strip().casefold()


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


def _checkout_owner(user, guest_email):
    """Queryset kwargs scoping an order lookup to ONE checkout owner.

    An account order and a guest order are disjoint sets, so the guest branch
    is ``user IS NULL AND guest_email = ...`` rather than a bare
    ``user=None`` — the latter is every guest order in the table, which would
    let one guest collapse onto (and read) another's order. The guest email
    arrives already canonicalised (``_canonical_guest_email``), so this
    comparison is against the same value the row stores.
    """
    if user is None:
        return {"user__isnull": True, "guest_email": guest_email}
    return {"user": user}


def _resolve_checkout_owner(request):
    """(user, guest_email, rejection) for one checkout submission.

    An authenticated caller owns their account: ``guest_email`` in the body
    is ignored outright rather than stored, because the Meta constraint on
    Order allows exactly one owner per row and an order with both is refused
    at the database.

    An anonymous caller has no account, so the guest email IS the identity
    spec 9.3 binds the session to — required input, canonicalised
    (``_canonical_guest_email``) so it is single-valued, and validated with
    the same validator the model field declares, so the store never records
    an address it would refuse to mail.

    The width gate comes BEFORE the validator on purpose: ``validate_email``
    accepts any length, and SQLite does not enforce ``varchar(254)``, so an
    over-length address would reach the INSERT and only blow up on the
    production Postgres (``_field_length_rejection``).
    """
    if request.user.is_authenticated:
        return request.user, "", None

    guest_email = _canonical_guest_email(request.data.get("guest_email"))
    if not guest_email:
        return (
            None,
            "",
            Response(
                {"error": "guest_email is required"},
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )
    length_rejection = _field_length_rejection("guest_email", guest_email)
    if length_rejection is not None:
        return None, "", length_rejection
    try:
        validate_email(guest_email)
    except ValidationError:
        return (
            None,
            "",
            Response(
                {"error": "guest_email must be a valid email address"},
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )
    return None, guest_email, None


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


def _checkout_response(order, status_code, disclose_guest_token):
    """The checkout body: the order, plus the guest's token on a mint.

    INVARIANT (the P1 fix, SPEC-1-B04 audit cycle 2): ``guest_token`` is
    disclosed by exactly ONE response in the whole system — the 201 that
    mints it — and by no collapse, keyed or keyless, ever.

    The token is a permanent read credential for that order, so every
    response that is not the mint is one more place a mistake could disclose
    it: a collapse reached with a caller who only knows a low-entropy key, a
    payload that drifted, or a future guard added without thought. Disclosing
    it only where it is created makes the disclosure independent of how
    carefully any collapse guard is written; no serializer, list, detail or
    admin read carries it either, so a leaked order read cannot be replayed
    as a guest lookup.

    The cost is deliberate and worth stating: a guest whose 201 never reached
    their client gets the order back from a replay but NOT the credential,
    so they cannot read that order through the token. That is the price of
    not making a permanent credential replayable, and it is paid on a path
    whose order is unpayable anyway while guest payment is a spec gap.
    """
    data = dict(OrderSerializer(order).data)
    if disclose_guest_token and order.user_id is None:
        data["guest_token"] = order.guest_token
    return Response(data, status=status_code)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_list(request):

    orders = Order.objects.filter(user=request.user).order_by("-created_at")

    # SPEC-9-04: the unique id tiebreaker makes the sort total, so a
    # paginated partition never repeats or skips a row across requests
    # (same reasoning as the products listing's F-12 fix).
    orders = orders.order_by("-created_at", "-id")

    paginator = Paginator(
        orders,
        _history_page_size(request.query_params.get(HISTORY_PAGE_SIZE_QUERY_PARAM)),
    )
    # get_page never raises: an unparsable page falls back to 1, a page
    # past the end to the last page — no 404 for a stale page link.
    page = paginator.get_page(request.query_params.get("page", 1))

    serializer = OrderSerializer(page.object_list, many=True)

    # House page-number envelope (products-listing parity).
    return Response(
        {
            "count": paginator.count,
            "total_pages": paginator.num_pages,
            "current_page": page.number,
            "next_page": page.has_next(),
            "previous_page": page.has_previous(),
            "results": serializer.data,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_detail(request, order_id):
    """[R-9.2.15] GET /account/orders/:id — the caller's OWN order only.

    Ownership is part of the lookup itself: a foreign user's order (and an
    unknown id alike) gets the same uniform 404 — never a 200 (the IDOR
    pin) and never a 403 that would confirm the id's existence
    (conventions.md: no existence leaks)."""
    try:
        order = Order.objects.get(id=order_id, user=request.user)

    except Order.DoesNotExist:
        return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

    serializer = OrderSerializer(order)

    return Response(serializer.data)


# ==================================
# Create Order / Checkout
# ==================================

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
    checked identity; this is the "same purchase attempt" test both collapse
    guards ask, so it is asked once, here, and both of them use the same
    answer.

    This is the P1 fix's first line of defence: an attacker holding only a
    guest's email and a low-entropy Idempotency-Key has to reproduce the
    victim's exact cart, coupon, six shipping fields and delivery option to
    make a collapse agree -- knowledge the email and the key do not give them.
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
    with, so a keyed guest replay cannot ride a collapse onto an order priced
    a different way while supplying everything else the guard checks.

    A submission that names NOTHING agrees with whatever the server chose,
    because the server chose it: pricing happens after both collapse guards,
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


def _idempotency_key_conflict():
    """The one answer a keyed submission gets when its key is already spent
    on a DIFFERENT purchase.

    SPEC-9-01 collapses a retry onto the original order; it cannot mint a
    second one for the same key, because the (owner, key) constraint forbids
    it. So a guest who presents a key held by an order that disagrees on the
    cart, the coupon or the shipping payload gets this instead of that
    order's body and its credential. It is a 409 rather than a silent second
    order because "your key is spent" is what the client has to be told --
    the same contract a payment provider's idempotency endpoint gives -- and
    it carries no order data, no token and no existence signal beyond the
    key the caller supplied themselves.
    """
    return Response(
        {
            "error": (
                "This Idempotency-Key was already used for a different "
                "checkout submission"
            )
        },
        status=status.HTTP_409_CONFLICT,
    )


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

# [R-11.1] SPEC-11-01: the payment-intent persistence carries the same shape
# of safety net. The unique constraint on razorpay_order_id is the
# concurrency authority; the bounded loop only converts a lost race into a
# reuse of the winner's committed id (one retry converges -- the collision
# window is a single write), and exhaustion fails loudly instead of looping.
PAYMENT_INTENT_ATTEMPTS = 3


def _current_year():
    """The order-number year bucket: the sequence restarts each January 1st
    (R-8.4), so this is the only clock the format depends on."""
    return timezone.now().year


def _next_order_sequence(year):
    """Next sequence for the year's ORD-YYYY- prefix: the highest committed
    number's sequence plus one. Fixed-width zero padding keeps lexicographic
    order equal to numeric order. This lookup is check-then-act and
    deliberately NOT the concurrency authority -- two connections can read
    the same max before either commits -- the unique constraint on
    Order.order_number is, and the caller retries on the IntegrityError
    (conventions.md:17)."""
    prefix = f"ORD-{year}-"
    latest = (
        Order.objects.filter(order_number__startswith=prefix)
        .order_by("-order_number")
        .values_list("order_number", flat=True)
        .first()
    )
    if latest is None:
        return 1
    return int(latest.rsplit("-", 1)[1]) + 1


def _generate_order_number():
    year = _current_year()
    return f"ORD-{year}-{_next_order_sequence(year):06d}"


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
# [R-1.13] SPEC-1-B04: opened deliberately (conventions.md:26). Spec line 74
# ("optionally check out without an account") and 9.3's guest-session binding
# both require it, and the authorization the endpoint used to lean on is not
# removed, it is REPLACED: an authenticated caller is still scoped to their own
# orders everywhere below, and an anonymous one is scoped to the guest email
# they present plus the token this response mints for them. Nothing here reads
# a body-supplied user id.
@permission_classes([AllowAny])
# [R-1.13] A public MUTATING endpoint, so conventions.md:24 requires a scope.
# It shares the cart budget on purpose: checkout is the terminal mutation of
# exactly the session-cart flow those two rates already govern (add to cart ->
# apply coupon -> submit), so a guest's whole funnel draws on one budget
# rather than two that can be spent independently.
@throttle_scope("cart")
def create_order(request):

    # [R-9.3.14] Honor the Idempotency-Key header when the client sends it:
    # every retry carrying the same value is the SAME submission, so the
    # atomic block below dedupes on (owner, key). Keyless clients keep the
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
    user, guest_email, owner_rejection = _resolve_checkout_owner(request)
    if owner_rejection is not None:
        return owner_rejection

    owner = _checkout_owner(user, guest_email)

    # =========================
    # Get current session
    # =========================

    if not request.session.session_key:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    session_id = request.session.session_key

    # =========================
    # Find cart
    # =========================

    try:
        cart = Cart.objects.get(session_id=session_id)

    except Cart.DoesNotExist:
        return Response({"error": "Cart not found"}, status=status.HTTP_404_NOT_FOUND)

    # =========================
    # Get cart items
    # =========================

    cart_items = cart.items.select_related("product").all()

    if not cart_items.exists():
        return Response({"error": "Cart is empty"}, status=status.HTTP_400_BAD_REQUEST)

    # =========================
    # Validate checkout data
    # =========================

    required_fields = [
        "full_name",
        "phone",
        "address",
        "city",
        "state",
        "pincode",
    ]

    for field in required_fields:
        if not request.data.get(field):
            return Response(
                {"error": f"{field} is required"}, status=status.HTTP_400_BAD_REQUEST
            )

    # [R-1.13] The same varchar-width trap the guest email had, on the rest
    # of the shipping payload: SQLite does not enforce `full_name`'s 150 or
    # `phone`'s 15, so an over-length value is a 201 in the suite and a
    # DataError on the production Postgres. Refused here, before the atomic
    # block opens and therefore before any row exists.
    for field in _SHIPPING_FIELDS:
        length_rejection = _field_length_rejection(field, request.data[field])

        if length_rejection is not None:
            return length_rejection

    # =========================
    # Calculate cart subtotal
    # =========================

    # SPEC-6-01 [6.2.22]: a cart line that outlasted its stock (stock can
    # drop after the item was added) must not become an order the customer
    # can pay for. This gate is advisory and read-only -- stock can still
    # change between create and pay, so verify_payment re-checks under a
    # row lock before decrementing; that remains the authoritative backstop.
    unavailable = []

    subtotal_amount = Decimal("0.00")

    for cart_item in cart_items:
        product = cart_item.product

        if cart_item.quantity > product.stock:
            unavailable.append(
                {
                    "name": product.name,
                    "requested": cart_item.quantity,
                    "available": product.stock,
                }
            )

        subtotal_amount += product.price * cart_item.quantity

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

    # =========================
    # Coupon
    # =========================

    coupon = None
    discount_amount = Decimal("0.00")

    coupon_code = request.data.get("coupon_code")

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

        # Check active
        if not coupon.active:
            return Response(
                {"error": "This coupon is inactive"}, status=status.HTTP_400_BAD_REQUEST
            )

        # Check validity dates
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

        # Check usage limit
        if coupon.usage_limit is not None and coupon.used_count >= coupon.usage_limit:
            return Response(
                {"error": "This coupon has reached its usage limit"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check minimum order amount
        if subtotal_amount < coupon.minimum_order_amount:
            return Response(
                {
                    "error": "Minimum order amount is required",
                    "minimum_order_amount": coupon.minimum_order_amount,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Calculate discount
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

    # =========================
    # Final total
    # =========================
    #
    # [R-1.07] SPEC-1-B05: still the MERCHANDISE total. The shipping charge is
    # added inside the atomic block below, once the collapse guards have had
    # their say, so a replay never reprices an order that already exists.

    merchandise_total = subtotal_amount - discount_amount

    # =========================
    # Create order
    # =========================
    #
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

            duplicate = _find_duplicate_pending_order(
                owner, cart_items, coupon, request.data
            )

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

                # [R-1.13] The collapse returns the order but NOT the credential
                # (`_checkout_response` discloses the token on a mint only):
                # returning it here would make a permanent read credential
                # replayable by anyone who can reach a collapse. Scoped by
                # `owner` above AND by `_same_purchase`, so this can only ever
                # hand back a submission that agrees with the caller's on
                # identity (the guest email), on the complete shipping payload,
                # on the delivery option, on the cart contents and on the
                # coupon, inside the dedup window -- five independent
                # agreements, which is what "the same purchase attempt" means
                # for a caller whose only identity is the address they typed.
                return _checkout_response(
                    duplicate, status.HTTP_200_OK, disclose_guest_token=False
                )

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
                # [R-1.13] Only an account submission has a user row to serialize
                # on. A guest submission is already serialized by the cart lock
                # above (a same-session replay queues there), and its cross-session
                # authority is the (guest_email, idempotency_key) constraint the
                # migration adds -- declared conditional so the account pair keeps
                # answering for account rows.
                if user is not None:
                    User.objects.select_for_update().get(pk=user.pk)

                replay = (
                    Order.objects.filter(**owner, idempotency_key=idempotency_key)
                    .prefetch_related("items")
                    .first()
                )

                if replay is not None:
                    # [R-1.13] P1 FIX: the agreement requirement is scoped to the
                    # guest branch on purpose. An authenticated caller proved who
                    # they are at login, so (user, key) is a strong pair and
                    # SPEC-9-01's "collapse regardless of payload drift" stands
                    # for them. A guest proved only an email and a
                    # low-entropy key -- so this guard additionally requires the
                    # whole purchase to agree (`_same_purchase`, the same
                    # question the keyless dedup guard asks), and a disagreement
                    # is a 409 rather than another guest's order body. Without
                    # this, one email plus one guessable key was enough to read a
                    # stranger's address and take their token.
                    if user is None and not _same_purchase(
                        replay, cart_items, coupon, request.data
                    ):
                        return _idempotency_key_conflict()

                    # Benign keyed retry, so INFO: same judgement as the dedup
                    # guard's collapse log above.
                    logger.info(
                        "Checkout idempotency: order %s replayed for %s "
                        "(Idempotency-Key)",
                        replay.id,
                        replay.customer_name,
                    )

                    # No token: see the invariant on `_checkout_response`.
                    return _checkout_response(
                        replay, status.HTTP_200_OK, disclose_guest_token=False
                    )

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
                region=request.data.get("state"),
                postal_code=request.data.get("pincode"),
                merchandise_total=merchandise_total,
                method_code=_requested_shipping_code(request.data),
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
                    guest_email or user,
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
                # [R-1.13] The guest token is minted in the same loop and is
                # equally unique-constrained, so a collision on it converges the
                # same way the number race does (conventions.md:17 — the
                # constraint is the authority, the retry is how it is honored).
                # Account orders never mint one: their token stays NULL.
                guest_token = None if user is not None else _mint_guest_token()
                try:
                    with transaction.atomic():
                        order = Order.objects.create(
                            user=user,
                            guest_email=guest_email,
                            guest_token=guest_token,
                            full_name=request.data.get("full_name"),
                            phone=request.data.get("phone"),
                            address=request.data.get("address"),
                            city=request.data.get("city"),
                            state=request.data.get("state"),
                            pincode=request.data.get("pincode"),
                            coupon=coupon,
                            discount_amount=discount_amount,
                            shipping_method_id=shipping_method_id,
                            shipping_method_code=shipping_method_code,
                            shipping_amount=shipping_amount,
                            total_amount=total_amount,
                            order_number=candidate,
                        )
                except IntegrityError:
                    # Lost the number (or token) race: the unique constraint
                    # rejected the candidate, so the savepoint above rolled the
                    # failed insert back and this transaction stays usable for the
                    # retry. The bound is a safety net, not the expected path: one
                    # retry converges because the collision window is a single
                    # insert.
                    if attempt == ORDER_NUMBER_ATTEMPTS - 1:
                        raise
                    continue
                break

            # [R-10.12]/[R-10.17] SPEC-10-02: creation is the lifecycle's first
            # transition (no source state -> pending), so the audit trail opens
            # here — inside the same outer atomic block as the order row, the
            # minted number, the snapshots and the key binding. A rolled-back
            # checkout leaves no order and no event; the actor is the customer
            # who placed the order — and NULL for a guest, because there is no
            # account to attribute it to (the row's own guest_email is the record
            # of who placed it, and OrderStatusEvent.actor is nullable for
            # exactly this case, like StockMovement.created_by).
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
            # [R-1.13] `user` is None for a guest, which is exactly what the
            # nullable StockReservation.owner column accepts: a guest checkout
            # reserves its units the same way an account one does, so the
            # oversell guarantee is unchanged for the new caller.
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

    # =========================
    # Return order
    # =========================

    # This is the ONLY response that carries the credential, because it is
    # the only one that minted it (`_checkout_response`'s invariant).
    return _checkout_response(order, status.HTTP_201_CREATED, disclose_guest_token=True)


# ==================================
# Guest order lookup (SPEC-1-B04)
# ==================================


@api_view(["GET"])
# [R-1.13] Opened deliberately (conventions.md:26): a guest has no session to
# authorize against, which is precisely why the credential is a 256-bit token
# minted at checkout. AllowAny here does NOT mean unguarded - the lookup below
# is the guard, and it is a guard by possession, not by identity: it can only
# ever return the one row whose stored token equals the presented one.
@permission_classes([AllowAny])
def guest_order_detail(request, order_number=None):
    """[R-1.13] GET /api/orders/guest/[<order_number>/] - a guest's OWN order.

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

    # Calculate cart subtotal
    subtotal = Decimal("0.00")

    for item in cart.items.select_related("product"):
        subtotal += item.product.price * item.quantity

    # Check minimum order amount
    if subtotal < coupon.minimum_order_amount:
        return None, None, _uniform_coupon_rejection()

    return coupon, subtotal, None


@api_view(["POST"])
@throttle_scope("coupon")
def apply_coupon(request):

    code = request.data.get("code")

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

    # Calculate discount
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


# ==================================
# Create Razorpay Payment
# ==================================


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_scope("payment")
def create_payment(request):

    order_id = request.data.get("order_id")

    if not order_id:
        return Response(
            {"error": "order_id is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    # Find user's order
    try:
        order = Order.objects.get(id=order_id, user=request.user)

    except Order.DoesNotExist:
        return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

    # Payment may only be started for an unpaid order.
    if order.status != "pending":
        return Response(
            {"error": "This order cannot be paid"}, status=status.HTTP_400_BAD_REQUEST
        )

    # Razorpay client
    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )

    # Amount must be in paise
    amount = int(order.total_amount * Decimal("100"))

    if order.razorpay_order_id:
        razorpay_order_id = order.razorpay_order_id
    else:
        try:
            # [R-8.11] The gateway is charged in the denomination the order
            # was minted with, read off the row — never a hardcoded code.
            razorpay_order = client.order.create(
                {
                    "amount": amount,
                    "currency": order.currency,
                    "receipt": f"order_{order.id}",
                }
            )
        except Exception:
            # [SPEC-7-02] Without this, a gateway/network failure surfaces
            # only as a bare 500 with no order reference; the re-raise
            # preserves the 500 semantics exactly.
            logger.exception("Payment intent creation failed for order %s", order.id)
            raise
        razorpay_order_id = razorpay_order["id"]
        # [R-7.20] First persistence of the gateway intent is a payment
        # event: the intent and its trail row commit together, so a crash
        # between the two cannot leave an intent the trail never saw. The
        # reuse path above writes nothing, so it emits nothing.
        # [SPEC-11-01] conventions.md:16,17 -- the pre-check above is an
        # unlocked read, not the concurrency authority. The row is
        # re-fetched under select_for_update inside the atomic block, so
        # the loser of a race reuses the winner's committed id instead of
        # double-writing, and the unique constraint on razorpay_order_id
        # stays the last-resort authority: a violated write retries rather
        # than surfacing a 500 (that retry, like the reuse path, emits
        # nothing of its own).
        for attempt in range(PAYMENT_INTENT_ATTEMPTS):
            try:
                with transaction.atomic():
                    locked = Order.objects.select_for_update().get(pk=order.pk)
                    if locked.razorpay_order_id:
                        razorpay_order_id = locked.razorpay_order_id
                        break
                    locked.razorpay_order_id = razorpay_order_id
                    locked.save(update_fields=["razorpay_order_id"])
                    AuditEvent.record(
                        AuditEvent.EventType.PAYMENT_INITIATED,
                        actor=request.user,
                        order=locked,
                        detail={
                            "razorpay_order_id": razorpay_order_id,
                            "amount_paise": amount,
                        },
                    )
                break
            except IntegrityError:
                # The savepoint above rolled the violated write back, so
                # the next turn starts from committed state. The bound is a
                # safety net, not the expected path: one retry converges.
                if attempt == PAYMENT_INTENT_ATTEMPTS - 1:
                    raise
                continue

    return Response(
        {
            "order_id": order.id,
            "razorpay_order_id": razorpay_order_id,
            "amount": amount,
            "amount_in_rupees": order.total_amount,
            # [R-8.11] Same currency the gateway payload used: the order's own.
            "currency": order.currency,
            "key_id": settings.RAZORPAY_KEY_ID,
        }
    )


# ==================================
# Verify Razorpay Payment
# ==================================


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify_payment(request):

    razorpay_order_id = request.data.get("razorpay_order_id")

    razorpay_payment_id = request.data.get("razorpay_payment_id")

    razorpay_signature = request.data.get("razorpay_signature")

    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature]):
        return Response(
            {"error": "Payment details are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Verify payment signature
    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )

    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
            }
        )

    except razorpay.errors.SignatureVerificationError:
        # [R-7.20] A rejected signature is a verify failure with no other
        # side effect to share a transaction with: the single insert is
        # atomic on its own, and the claimed gateway references ride in
        # detail because no order relationship is proven yet.
        AuditEvent.record(
            AuditEvent.EventType.PAYMENT_SIGNATURE_REJECTED,
            actor=request.user,
            detail={
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
            },
        )

        # [SPEC-7-02] The audit row is the structured record; this log line
        # makes the failure greppable for an operator (same pattern in
        # every verify failure branch below).
        logger.warning(
            "Payment signature rejected (gateway order %s, payment %s)",
            razorpay_order_id,
            razorpay_payment_id,
        )

        # [R-10.4] SPEC-10-04: the failure path marks the payment dimension
        # failed (spec 10.2) while the ORDER stays pending — retryable by
        # design: neither status nor razorpay_payment_id moves, so the
        # already-processed gate passes and a later successful verify
        # captures normally (failed -> captured, the machine's retry edge).
        # Scoped to the caller's own order whose stored gateway ref equals
        # the claimed one, with no prior capture claim: a forged or
        # mismatched reference can never write payment state, and the
        # response below stays byte-identical either way (no existence
        # leak). The write and its audit row share one transaction (the
        # 10-02 rollback-together contract).
        with transaction.atomic():
            failed_order = (
                Order.objects.select_for_update()
                .filter(
                    razorpay_order_id=razorpay_order_id,
                    user=request.user,
                )
                .first()
            )
            if (
                failed_order
                and failed_order.status == "pending"
                and not failed_order.razorpay_payment_id
                and payment_transition_allowed(failed_order.payment_status, "failed")
            ):
                failed_order.payment_status = "failed"
                failed_order.save(update_fields=["payment_status"])
                # [R-10.12]/[R-10.17] The audit row rides the same
                # transaction. The order status did not move on a failed
                # attempt, so the row records from == to ('pending') and
                # the trigger names the failure; actor NULL — the customer
                # flow has no admin actor.
                OrderStatusEvent.objects.create(
                    order=failed_order,
                    from_status=failed_order.status,
                    to_status=failed_order.status,
                    actor=None,
                    trigger=TRIGGER_PAYMENT_FAILED,
                )

                # [R-12.8] SPEC-12-02 §12.1 step 6: the checkout attempt
                # failed, so its holds are dead — release them in this same
                # transaction. A retry (the machine's failed->captured edge)
                # then re-checks stock cleanly and converts only holds that
                # are still active; a hold this failure abandoned is never
                # resurrected into a sale.
                failed_order.stock_reservations.filter(
                    status=StockReservation.Status.ACTIVE
                ).update(status=StockReservation.Status.RELEASED)

        return Response(
            {"error": "Payment verification failed"}, status=status.HTTP_400_BAD_REQUEST
        )

    order_id = request.data.get("order_id")
    if not order_id:
        return Response(
            {"error": "order_id is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(
                id=order_id, user=request.user
            )
        except Order.DoesNotExist:
            # [R-7.20] The verify attempt names an order the caller does not
            # own: there is no FK target, so the claimed id rides in detail.
            AuditEvent.record(
                AuditEvent.EventType.PAYMENT_ORDER_NOT_FOUND,
                actor=request.user,
                detail={"order_id": order_id},
            )

            logger.warning(
                "Payment verify failed: order %s not found for this user",
                order_id,
            )

            return Response(
                {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if order.status != "pending" or order.razorpay_payment_id:
            AuditEvent.record(
                AuditEvent.EventType.PAYMENT_ALREADY_PROCESSED,
                actor=request.user,
                order=order,
                detail={
                    "razorpay_order_id": razorpay_order_id,
                    "razorpay_payment_id": razorpay_payment_id,
                    "order_status": order.status,
                },
            )

            # Benign double-submit retry, so INFO: a WARNING here would
            # spam the log on every impatient re-click.
            logger.info(
                "Payment verify skipped: order %s already processed (%s)",
                order.id,
                order.status,
            )

            return Response(
                {"error": "This order has already been processed"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if order.razorpay_order_id != razorpay_order_id:
            AuditEvent.record(
                AuditEvent.EventType.PAYMENT_REFERENCE_MISMATCH,
                actor=request.user,
                order=order,
                detail={
                    "claimed_razorpay_order_id": razorpay_order_id,
                    "razorpay_payment_id": razorpay_payment_id,
                },
            )

            logger.warning(
                "Payment verify failed: order %s is bound to gateway order %s, not %s",
                order.id,
                order.razorpay_order_id,
                razorpay_order_id,
            )

            return Response(
                {"error": "Payment does not belong to this order"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        order_items = list(order.items.all())
        product_ids = [item.product_id for item in order_items]
        # [SPEC-12-02] Deterministic lock acquisition (ascending id):
        # unordered ``filter(id__in=...)`` let the plan pick the sequence,
        # so two carts sharing products in different insertion orders could
        # deadlock across the lock handoff (section-12 verified-facts
        # advisory — the loser died with a rollback 500). One total order
        # closes the cycle; the sufficiency re-check below is unchanged.
        locked_products = {
            product.id: product
            for product in products.objects.select_for_update()
            .filter(id__in=product_ids)
            .order_by("id")
        }

        for item in order_items:
            product = locked_products.get(item.product_id)
            if product is None or product.stock < item.quantity:
                AuditEvent.record(
                    AuditEvent.EventType.PAYMENT_STOCK_CONFLICT,
                    actor=request.user,
                    order=order,
                    detail={
                        "product_id": item.product_id,
                        "requested": item.quantity,
                        "available": product.stock if product else 0,
                    },
                )

                # A lost checkout race, not an attack: INFO.
                logger.info(
                    "Payment verify failed: order %s stock conflict "
                    "(product %s requested %s, available %s)",
                    order.id,
                    item.product_id,
                    item.quantity,
                    product.stock if product else 0,
                )

                # [R-12.8] SPEC-12-02 §12.1 step 6: this attempt failed the
                # sufficiency re-check (the oversell race's loser), so its
                # holds are dead — release them now instead of leaving
                # phantom pressure on available-to-sell until the TTL sweep
                # (SPEC-12-03). The order stays pending/retryable: a later
                # retry re-checks against live stock with no hold to
                # convert, exactly like the payment-failure retry edge.
                order.stock_reservations.filter(
                    status=StockReservation.Status.ACTIVE
                ).update(status=StockReservation.Status.RELEASED)

                return Response(
                    {
                        "error": "An item is no longer available in the requested quantity"
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        # The coupon is located by its id on the already-locked Order row
        # and locked on its own FOR UPDATE below, never joined into the
        # order's locked query. `coupon` is nullable, so a select_related
        # here compiles to a LEFT OUTER JOIN, and FOR UPDATE over the
        # nullable side of an outer join is rejected by PostgreSQL
        # ("FOR UPDATE cannot be applied to the nullable side of an outer
        # join") while SQLite never emits FOR UPDATE at all
        # (features.has_select_for_update is False) - so the join made
        # every verify return 500 in production and no SQLite test could
        # ever see it. Nothing is unlocked by dropping it: every coupon
        # field read below (active / valid_from / valid_until /
        # usage_limit / used_count) comes off the instance fetched here,
        # after its own row lock, never off the order's join.
        coupon = None
        if order.coupon_id is not None:
            coupon = Coupon.objects.select_for_update().get(pk=order.coupon_id)
            now = timezone.now()
            if (
                not coupon.active
                or now < coupon.valid_from
                or now > coupon.valid_until
                or (
                    coupon.usage_limit is not None
                    and coupon.used_count >= coupon.usage_limit
                )
            ):
                AuditEvent.record(
                    AuditEvent.EventType.PAYMENT_COUPON_INVALID,
                    actor=request.user,
                    order=order,
                    detail={"coupon_id": coupon.pk},
                )

                # Same race shape as the stock conflict: INFO.
                logger.info(
                    "Payment verify failed: order %s coupon %s no longer valid",
                    order.id,
                    coupon.pk,
                )

                # [R-12.8] SPEC-12-02 §12.1 step 6: this attempt failed a
                # checkout precondition, so its holds are released like
                # every other failed-verify path; the order stays
                # pending/retryable for a corrected re-attempt.
                order.stock_reservations.filter(
                    status=StockReservation.Status.ACTIVE
                ).update(status=StockReservation.Status.RELEASED)

                return Response(
                    {"error": "The coupon is no longer valid"},
                    status=status.HTTP_409_CONFLICT,
                )

        # [R-12.7] SPEC-12-02 §12.1 step 5: confirmation converts the
        # order's live holds into committed sales. The decrement below
        # stays the stock authority and the sufficiency re-check above
        # remains the oversell backstop (R-12.12: the sale never re-learns
        # availability from a reservation); this flip is the reservation
        # ledger's truth. Filtering on active makes it idempotent and
        # retry-safe: a hold released by an earlier failed attempt is
        # terminal (never resurrected into a sale), and an order with no
        # holds (legacy, or the post-failure retry) verifies unchanged. A
        # lapsed TTL is deliberately NOT a conversion gate — the captured
        # payment proceeds on the re-checked stock; expiry belongs to the
        # SPEC-12-03 reconciler.
        order.stock_reservations.filter(status=StockReservation.Status.ACTIVE).update(
            status=StockReservation.Status.CONVERTED
        )

        for item in order_items:
            product = locked_products[item.product_id]
            product.stock -= item.quantity
            product.save(update_fields=["stock"])
            # [6.5.17] No silent inventory edits: a paid sale is an inventory
            # mutation like any other, so every decrement lands in the ledger
            # with the order as its reference and no actor (system). The row
            # is locked and the new value was just computed here, so
            # stock_after is the real post-decrement quantity.
            StockMovement.objects.create(
                product=product,
                delta=-item.quantity,
                reason=StockMovement.Reason.SALE,
                stock_after=product.stock,
                note=f"Order #{order.id}",
                created_by=None,
            )

        if coupon:
            coupon.used_count += 1
            coupon.save(update_fields=["used_count"])

        # [R-8.16] paid_at is the business-event timestamp of exactly this
        # transition, so it is written beside it inside the same atomic
        # block (rollback together). The already-processed gate above makes
        # a replay unreachable here; the or-guard pins "written exactly
        # once, never mutated" even if a future path re-enters.
        # [R-10.12] SPEC-10-02: capture the pre-transition status for the
        # audit row written below (insertion-only hunk; the byte-frozen
        # 8-04 region below is untouched).
        previous_status = order.status
        order.paid_at = order.paid_at or timezone.now()
        order.status = "confirmed"
        order.razorpay_payment_id = razorpay_payment_id
        order.save(update_fields=["status", "razorpay_payment_id", "paid_at"])
        # [R-10.1] SPEC-10-01b: the payment dimension is captured by the
        # same confirmed-payment event (the only payment-dimension writer
        # in this batch — COD/failure states are SPEC-10-04). The save
        # above is a byte-frozen region (SPEC-8-04), so the dimension rides
        # this second persistence of the already-locked row in the SAME
        # atomic block: both UPDATEs commit or roll back together, and the
        # already-processed gate keeps this path unreachable on replay.
        order.payment_status = payment_for_status("confirmed")
        order.save(update_fields=["payment_status"])
        # [R-10.12]/[R-10.17] SPEC-10-02: the transition's audit row rides
        # this same atomic block — a rolled-back verify leaves no event
        # behind (pinned). No admin acts on this path, so the trigger
        # records the source and the actor stays NULL (spec 10.3: "Actor
        # or triggering event" — one of the two is enough).
        OrderStatusEvent.objects.create(
            order=order,
            from_status=previous_status,
            to_status=order.status,
            actor=None,
            trigger=TRIGGER_PAYMENT_VERIFY,
        )

        if request.session.session_key:
            cart = Cart.objects.filter(session_id=request.session.session_key).first()
            if cart:
                cart.items.filter(product_id__in=product_ids).delete()

        # [R-7.20] The success story has two halves: the gateway
        # reconciliation view wants payment.verified with the razorpay
        # references, the order timeline wants order.paid. Both are written
        # inside this atomic block, so a verify that rolls back (for any
        # reason) leaves neither behind.
        AuditEvent.record(
            AuditEvent.EventType.PAYMENT_VERIFIED,
            actor=request.user,
            order=order,
            detail={
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
            },
        )
        AuditEvent.record(
            AuditEvent.EventType.ORDER_PAID,
            actor=request.user,
            order=order,
            detail={
                "order_id": order.id,
                "total_amount": str(order.total_amount),
            },
        )
        # [R-19.0] Event-driven customer notification beside the audit
        # hook, in this same atomic block. Registered, not sent: the send
        # itself moves to transaction.on_commit (ASYNC-2b1) so this block's
        # select_for_update rows (Order, products, Coupon) are released at
        # commit instead of being held for the length of the SMTP round
        # trip — a slow mail provider no longer blocks other checkouts on
        # the same SKU or coupon. dispatch never raises: a send failure is
        # logged on the notifications channel at ERROR with its traceback
        # and the captured payment stays confirmed (the SMTP-503
        # account-still-created behavior, mirrored).
        notifications.dispatch_on_commit(
            AuditEvent.EventType.ORDER_PAID,
            {"order": order},
        )

    return Response(
        {
            "message": "Payment verified successfully",
            "order_id": order.id,
            "status": order.status,
            "razorpay_payment_id": razorpay_payment_id,
        }
    )


# ==================================
# Admin orders JSON seam (SPEC-9-07, spec 9.4 Orders module)
# ==================================

# SPEC-9-07: the fulfilment endpoint drives the order one legal step per
# call along the flow the admin surface's bulk actions encode.
# ADMIN_FULFILMENT_NEXT (the step map) and transition_allowed (the gate)
# live in orders.state — [R-10.1] single source.


@api_view(["GET"])
@permission_classes([HasOrdersRead])
def admin_order_list(request):
    """[R-9.4.8] GET /api/admin/orders/ — every order, staff eyes only.

    The customer list scopes to ``user=request.user``; this seam is reached
    only through ``orders.read`` (support/finance/admin roles), so it serves
    the unscoped queryset. Same house page-number envelope and page-size
    config as the customer history — the paginator cap exists so no caller,
    staff included, can request an unbounded page."""
    orders = Order.objects.select_related("coupon").order_by("-created_at", "-id")

    paginator = Paginator(
        orders,
        _history_page_size(request.query_params.get(HISTORY_PAGE_SIZE_QUERY_PARAM)),
    )
    page = paginator.get_page(request.query_params.get("page", 1))

    serializer = OrderSerializer(page.object_list, many=True)
    return Response(
        {
            "count": paginator.count,
            "total_pages": paginator.num_pages,
            "current_page": page.number,
            "next_page": page.has_next(),
            "previous_page": page.has_previous(),
            "results": serializer.data,
        }
    )


@api_view(["GET"])
@permission_classes([HasOrdersRead])
def admin_order_detail(request, order_id):
    """[R-9.4.9] GET /api/admin/orders/:id/ — one order, staff eyes only.

    Unlike the customer detail endpoint there is no ownership scoping to
    enforce, so an unknown id is a plain uniform 404 (never an existence
    leak — the caller has already passed the orders.read gate)."""
    try:
        order = Order.objects.select_related("coupon").get(id=order_id)
    except Order.DoesNotExist:
        return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

    return Response(OrderSerializer(order).data)


def _may_fulfil(user, order):
    """Whether ``user`` may advance ``order`` one fulfilment step.

    Two questions, not one. A caller holding ``orders.read`` sees the whole
    order book, so the fulfilment walk is unscoped for it (support and admin,
    the two roles that hold both capabilities; finance holds ``orders.read``
    but not ``orders.fulfill``, so it never reaches this seam at all). A
    caller holding ONLY ``orders.fulfill`` is the packing operator, whose
    authority is the queue — the statuses the walk can still advance, the
    same constant ``OrderAdmin``'s scoped grid lists — and nothing else.

    Django's ``is_superuser`` flag is preserved as the bypass every other
    surface keeps (mirroring ``capability_required``): the trust anchor must
    not be narrowed by a least-privilege rule meant for staff roles.

    Deny-by-default in both directions: an unknown capability grants nothing,
    and a caller with no role at all has already been refused by
    ``HasOrdersFulfill`` before this runs.
    """
    if user.is_superuser or user_has_capability(user, "orders.read"):
        return True
    return user_has_capability(user, "orders.fulfill") and (
        order.status in FULFILMENT_QUEUE_STATUSES
    )


@api_view(["POST"])
@permission_classes([HasOrdersFulfill])
def admin_order_fulfill(request, order_id):
    """[R-9.4.10] POST /api/admin/orders/:id/fulfill — advance one step.

    The gate-then-update pair runs under the row lock: two concurrent
    fulfils cannot both pass the gate on the same stale status, so an order
    can never skip two steps in one call. Deliberately NOT idempotent —
    each accepted call performs one visible transition; the machine itself
    rejects re-running a step from the new status (409). No business-event
    stamp is written here: the admin surface's mark_shipped/mark_delivered
    do not write shipped_at/delivered_at either, and the named-stamp
    writers are their own later task — the JSON seam never invents a richer
    record than the admin surface for the same transition.

    [R-1-B03] The fulfilment capability is not order visibility (spec 1.1
    line 110 splits them), so a caller holding ``orders.fulfill`` WITHOUT
    ``orders.read`` — the inventory/fulfilment operator — is scoped to the
    queue its own admin surface lists (``FULFILMENT_QUEUE_STATUSES``) and
    cannot advance an order outside it by guessing a pk. An out-of-queue
    order is answered with the SAME uniform 404 an unknown id gets: it must
    not confirm that the order exists, nor name its status, to a role that
    was deliberately not given order visibility. A caller that does hold
    ``orders.read`` (support, finance, admin) and Django's superuser flag
    keep the unrestricted contract above."""
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(id=order_id)
        except Order.DoesNotExist:
            return Response(
                {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if not _may_fulfil(request.user, order):
            return Response(
                {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
            )

        target = ADMIN_FULFILMENT_NEXT.get(order.status)
        if target is None or not transition_allowed(order.status, target):
            allowed = ", ".join(sorted(ALLOWED_TRANSITIONS.get(order.status, set())))
            return Response(
                {
                    "error": f"Order cannot be fulfilled from status '{order.status}'",
                    "allowed": allowed,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # [R-10.19]/[R-10.14] SPEC-10-03: the fulfil seam advances
        # confirmed→shipped too, so the shipped preconditions (payment
        # captured + items present) gate it here with the same authority
        # as the admin surface (insertion-only hunk; the 9-07 write below
        # stays byte-identical). The envelope middleware wraps this body,
        # so callers read the reasons at details.preconditions.
        precondition_reasons = precondition_failures(order, target)
        if precondition_reasons:
            return Response(
                {
                    "error": f"Order cannot be fulfilled from status '{order.status}'",
                    "preconditions": precondition_reasons,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # [R-10.12] SPEC-10-02: capture the pre-transition status for the
        # audit row written below (insertion-only hunk).
        previous_status = order.status
        order.status = target
        # [R-10.1] SPEC-10-01b: the fulfilment dimension rides the same
        # transition. Insertion-only hunk (the 9-07 save below stays
        # byte-identical), so the dimension persists via a second
        # same-transaction write to the row locked above.
        order.fulfilment_status = fulfilment_for_status(target)
        order.save(update_fields=["status"])
        order.save(update_fields=["fulfilment_status"])
        # [R-10.12]/[R-10.18] SPEC-10-02: the audit row lands in this same
        # transaction — a rolled-back fulfil never leaves a phantom event.
        OrderStatusEvent.objects.create(
            order=order,
            from_status=previous_status,
            to_status=target,
            actor=request.user,
            trigger=TRIGGER_ADMIN_API_FULFIL,
        )
        # [R-10.16] SPEC-10-05: the side-effect hook rides the same
        # atomic block, after the transition + its audit row
        # (insertion-only hunk).
        notify_transition(order, previous_status, target)
        # [6.12.6] API-side staff write: land the privileged-action record
        # the admin surface would have written (audit-log route reads it).
        log_api_action(
            request,
            order,
            CHANGE,
            f"Fulfilled via API: status moved to {target}.",
        )

    return Response(
        {
            "message": f"Order status advanced to {target}",
            "order_id": order.id,
            "status": order.status,
        }
    )


@api_view(["POST"])
@permission_classes([HasOrdersCancel])
def admin_order_cancel(request, order_id):
    """[R-9.4.11] POST /api/admin/orders/:id/cancel — cancel an unpaid order.

    The same machine gate the admin uses decides: only ``pending`` carries
    a cancel edge, so a paid order cannot be cancelled at all - its money
    comes back through the refund seam below instead, which records a
    Refund and moves the payment dimension without moving the status. The
    409 names that path.
    Idempotent: the machine's self-transition makes a re-cancel a no-op
    200 (no second stamp, no duplicate audit row). cancelled_at rides the
    transition exactly like admin ``cancel_pending`` — the is-none guard
    keeps a set event time immutable ([R-8.16])."""
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(id=order_id)
        except Order.DoesNotExist:
            return Response(
                {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if order.status == "cancelled":
            # The machine's self-transition: a replay, not a change.
            return Response(
                {
                    "message": "Order is already cancelled",
                    "order_id": order.id,
                    "status": order.status,
                }
            )

        if not transition_allowed(order.status, "cancelled"):
            return Response(
                {
                    "error": f"Order cannot be cancelled from status "
                    f"'{order.status}'. A paid order cannot be "
                    f"cancelled — issue a refund instead "
                    f"(POST /api/admin/orders/<id>/refund/).",
                },
                status=status.HTTP_409_CONFLICT,
            )

        # [R-10.12] SPEC-10-02: capture the pre-transition status for the
        # audit row written below (insertion-only hunk).
        previous_status = order.status
        order.status = "cancelled"
        order.cancelled_at = order.cancelled_at or timezone.now()
        # [R-10.1] SPEC-10-01b: fulfilment dimension rides the cancel
        # transition (insertion-only; second same-transaction write).
        order.fulfilment_status = fulfilment_for_status("cancelled")
        order.save(update_fields=["status", "cancelled_at"])
        order.save(update_fields=["fulfilment_status"])
        # [R-10.12]/[R-10.18] SPEC-10-02: the audit row lands in this same
        # transaction; the idempotent replay above returns before reaching
        # it, so a re-cancel never appends a second event.
        OrderStatusEvent.objects.create(
            order=order,
            from_status=previous_status,
            to_status="cancelled",
            actor=request.user,
            trigger=TRIGGER_ADMIN_API_CANCEL,
        )
        # [R-12.8] SPEC-12-02 §12.1 step 6: a cancelled checkout releases
        # its holds in this same transaction — cancelled units return to
        # available-to-sell immediately, not at the TTL sweep. The
        # idempotent replay above returns before this site, and the
        # active-only filter is a no-op on already-released holds.
        order.stock_reservations.filter(status=StockReservation.Status.ACTIVE).update(
            status=StockReservation.Status.RELEASED
        )
        # [R-10.16] SPEC-10-05: the side-effect hook rides the same
        # atomic block; the idempotent replay above returns before this
        # site, so a re-cancel never notifies twice (insertion-only hunk).
        notify_transition(order, previous_status, "cancelled")
        log_api_action(request, order, CHANGE, "Cancelled via API.")

    return Response(
        {
            "message": "Order cancelled",
            "order_id": order.id,
            "status": order.status,
        }
    )


# ==================================
# Admin refund seam (SPEC-1-05, spec 1.14 / [1.31])
# ==================================


def _refund_payload(refund):
    """The refund representation this seam returns (explicit fields).

    Built inline rather than through a serializer module: the record is
    written by this one writer and read only by its own callers, so a
    serializer class would exist purely to name the same eight keys. Money
    stays a Decimal here and the renderer stringifies it, exactly as
    OrderSerializer does with the order's amounts.
    """
    return {
        "id": refund.id,
        "amount": refund.amount,
        "kind": refund.kind,
        "status": refund.status,
        "reason": refund.reason,
        "gateway_refund_id": refund.gateway_refund_id,
        "actor_id": refund.actor_id,
        "created_at": refund.created_at,
    }


@api_view(["POST"])
@permission_classes([HasRefundsCreate])
def admin_order_refund(request, order_id):
    """[R-1.14] POST /api/admin/orders/:id/refund — refund a captured payment.

    Spec 1.14 makes a paid order refundable in full or in part, and the
    finance operator the one who reconciles payments and refunds ([1.31]), so
    authority is the ``refunds.create`` capability (finance/admin). That is
    the split spec 1.1 spells out: a catalogue manager must not be able to
    issue refunds, and neither can a support agent who may read and fulfil
    orders.

    Atomic and serialized: the Order row is locked for the whole attempt
    (this order's refund rows beside it, since they are the other half of the
    balance being spent), the balance is recomputed under those locks, and the
    gateway call happens inside the same transaction. So two concurrent
    refunds can never both pass the balance gate, and a gateway failure rolls
    the attempt back whole — no refund row, no payment-dimension move, no
    timestamp, no audit record.

    Idempotent-safe twice over: an ``Idempotency-Key`` collapses a retry onto
    the refund it already produced (no second gateway call), and without a key
    the balance gate alone still refuses to hand back the same money twice.

    The payment dimension moves to the choices orders.state already declares
    (``partially_refunded`` / ``refunded``) — this writer is their first
    writer, and no new choice was added for them.
    """
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

    reason = (request.data.get("reason") or "").strip()
    if not reason:
        # A refund is a financial action the reconciliation trail has to
        # explain later, so the operator's words are required input, not an
        # optional nicety (the admin cancel path asks for the same reason).
        return Response(
            {"error": "A refund reason is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    try:
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(id=order_id)
            except Order.DoesNotExist:
                return Response(
                    {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
                )

            # This order's refund rows, locked beside the order row, and
            # materialized because that is what makes the lock take effect.
            # What makes the ATTEMPT safe is the Order row lock above: every
            # refund writer for this order takes it first, so no other writer
            # can append a refund between this transaction's balance read and
            # its commit. This list serves the replay probe below; the balance itself
            # comes from refundable_remaining's aggregate, and the Order lock
            # is what keeps that read consistent.
            order_refunds = list(Refund.objects.select_for_update().filter(order=order))

            if idempotency_key is not None:
                replay = next(
                    (
                        row
                        for row in order_refunds
                        if row.idempotency_key == idempotency_key
                    ),
                    None,
                )
                if replay is not None:
                    # Benign keyed retry, so INFO rather than WARNING: the
                    # same judgement as checkout's replay-collapse log.
                    logger.info(
                        "Refund idempotency: refund %s replayed for order %s "
                        "(Idempotency-Key)",
                        replay.id,
                        order.pk,
                    )
                    return Response(
                        {
                            "message": "Refund already issued",
                            "order_id": order.id,
                            "status": order.status,
                            "payment_status": order.payment_status,
                            "refunded_total": Refund.refunded_total(order),
                            "refundable_remaining": order.refundable_remaining,
                            "refund": _refund_payload(replay),
                        }
                    )

            # The balance gate comes first because it is the reason a fully
            # refunded order cannot be refunded again; the payment-status gate
            # below then covers the orders that were never captured.
            remaining = order.refundable_remaining
            if remaining <= Decimal("0.00"):
                return Response(
                    {"error": "Order has no refundable balance left"},
                    status=status.HTTP_409_CONFLICT,
                )

            # [R-10.1] Eligibility is asked of the machine, not restated
            # here: a refund moves the payment dimension onto one of the two
            # refund values, so the row's current payment must be one the
            # machine declares an edge FROM (payment_transition_allowed over
            # PAYMENT_ALLOWED_TRANSITIONS - captured or partially_refunded
            # today). pending / authorized / failed declare no refund edge,
            # so they are refused before anything is written.
            if not any(
                payment_transition_allowed(order.payment_status, target)
                for target in ("refunded", "partially_refunded")
            ):
                return Response(
                    {
                        "error": f"Order payment is '{order.payment_status}'; "
                        f"only a captured payment can be refunded."
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            if not order.razorpay_payment_id:
                # A cash-on-delivery order carries no provider payment to
                # reverse, and the seam has nothing to call: refuse rather
                # than write a refund row no money moved behind.
                return Response(
                    {"error": "Order has no captured payment to refund at the gateway"},
                    status=status.HTTP_409_CONFLICT,
                )

            requested = request.data.get("amount")
            if requested is None:
                # No amount asked for: the rest of the order's money.
                amount = remaining
            else:
                try:
                    # The value is stringified before it is parsed, so a
                    # JSON number never becomes binary-float money, and the
                    # quantization happens inside the guard because a value
                    # too large for 2-dp money (or not a number at all) must
                    # be a 400, never a 500 out of the arithmetic.
                    amount = quantize_money(Decimal(str(requested)))
                except (ArithmeticError, TypeError, ValueError):
                    return Response(
                        {"error": "amount must be a decimal amount"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                # NaN/Infinity parse as Decimals but are not money, and
                # comparing one raises - so they are refused before the
                # comparisons below (the is_finite() short-circuit is what
                # makes that safe).
                if not amount.is_finite() or amount <= Decimal("0.00"):
                    return Response(
                        {"error": "amount must be a positive decimal amount"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if amount > remaining:
                    return Response(
                        {
                            "error": f"Refund of {amount} exceeds the "
                            f"refundable balance of {remaining}"
                        },
                        status=status.HTTP_409_CONFLICT,
                    )

            refund = Refund.objects.create(
                order=order,
                amount=amount,
                reason=reason,
                # FULL means "this attempt cleared what was left"; the order's
                # payment dimension below is the authority on what is left.
                kind=(Refund.Kind.FULL if amount == remaining else Refund.Kind.PARTIAL),
                status=Refund.Status.PENDING,
                actor=request.user,
                idempotency_key=idempotency_key,
            )

            # Raises RefundGatewayError out of this atomic block, which is what
            # discards the PENDING row above: a refund the gateway refused
            # leaves no trace at all.
            gateway_refund_id = refund_payment(
                payment_id=order.razorpay_payment_id,
                amount=amount,
            )

            refund.gateway_refund_id = gateway_refund_id
            refund.status = Refund.Status.PROCESSED
            refund.save(
                update_fields=[
                    "gateway_refund_id",
                    "status",
                    "updated_at",
                ]
            )

            refunded_total = Refund.refunded_total(order)
            # [R-10.1] The balance gate is what chooses between the two refund
            # values the eligibility gate above proved reachable: nothing left
            # to refund -> refunded, some money still refundable ->
            # partially_refunded. Both are declared edges in
            # PAYMENT_ALLOWED_TRANSITIONS from every state this writer admits,
            # and the gate above is what guarantees the order can never
            # overshoot the captured amount.
            order.payment_status = (
                "refunded"
                if refunded_total >= quantize_money(order.total_amount)
                else "partially_refunded"
            )
            # [R-8.16] The business-event stamp the refund section was named
            # for; the is-none guard keeps a set event time immutable.
            order.refunded_at = order.refunded_at or timezone.now()
            order.save(update_fields=["payment_status", "refunded_at"])

            # [6.12.6] API-side staff write: the privileged-action record the
            # admin surface would have left, naming the order and the money so
            # the audit-log route can answer "who refunded what" without
            # joining the refund row. It rides this same transaction, so a
            # rolled-back refund leaves no record of itself.
            log_api_action(
                request,
                refund,
                ADDITION,
                f"Refund {amount} {order.currency} ({refund.kind}) issued "
                f"via API for order #{order.id} "
                f"(gateway refund {gateway_refund_id}).",
            )

    except RefundGatewayError as exc:
        # The provider's own text goes to the log; the caller gets the fact
        # and nothing about the provider's internals.
        logger.warning(
            "Refund gateway failure for order %s (amount %s): %s",
            order_id,
            request.data.get("amount"),
            exc,
        )
        return Response(
            {"error": "The payment gateway could not complete this refund"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(
        {
            "message": "Refund issued",
            "order_id": order.id,
            "status": order.status,
            "payment_status": order.payment_status,
            "refunded_total": Refund.refunded_total(order),
            "refundable_remaining": order.refundable_remaining,
            "refund": _refund_payload(refund),
        },
        status=status.HTTP_201_CREATED,
    )


# ==================================
# [R-1.16] SPEC-1-B07a: the customer's return request
# ==================================


# Every body field this seam reads. One tuple so the type gate below and its
# test cannot drift apart: a field added to the view without being listed here
# would be read without the gate that keeps it out of a string operation.
_RETURN_BODY_FIELDS = ("order_number", "reason_code", "reason_note")


class MalformedReturnRequestBody(Exception):
    """A body this seam cannot read as text. Carries its own answer.

    The message is built from the REQUEST alone - the field name, or the fact
    that the body was not an object - so it is identical whatever the order
    number in that body would have turned out to name. That is what keeps the
    refusal from becoming an order-existence oracle (see
    ``_return_request_miss``).
    """

    def __init__(self, error):
        super().__init__(error)
        self.error = error


def _return_body(request):
    """The three text fields of this seam's body, stripped.

    TYPE-GATED BEFORE ANY STRING OPERATION, and that is the whole point of
    this function: a request body is attacker-controlled JSON, so
    ``order_number: 1`` / ``["x"]`` / ``{"a": 1}`` / ``reason_note: true`` /
    ``null`` all arrive as non-``str``. Reading those with
    ``(request.data.get(...) or "").strip()`` raised AttributeError, i.e. a 500
    on a public endpoint, and the 500 was byte-identical for an existing and a
    non-existent order - so it was not an existence oracle, just a crash the
    auditor could reach with any JSON client.

    Rules, uniformly across all three fields:

    * absent or ``null`` reads as the empty string, which keeps ``reason_note``
      optional (it is the only field that may be omitted) and keeps a null
      ``order_number`` on the "required" refusal it has always got;
    * a ``str`` is stripped, so padding is still tolerated;
    * anything else - ``int``, ``float``, ``bool``, ``list``, ``dict`` - is a
      malformed body and is refused, never coerced. Coercing would mean
      inventing an order number out of ``["x"]`` and guessing a note out of
      ``{"a": 1}``; refusing says the truth about what the caller sent.

    A body that is not a JSON object at all (a bare list or a bare string) has
    no ``.get`` to call, so it is refused on the same path rather than
    crashing on the lookup that follows.
    """
    data = request.data
    if not isinstance(data, dict):
        raise MalformedReturnRequestBody("A JSON object body is required")
    fields = {}
    for field in _RETURN_BODY_FIELDS:
        raw = data.get(field)
        if raw is None:
            fields[field] = ""
        elif isinstance(raw, str):
            fields[field] = raw.strip()
        else:
            raise MalformedReturnRequestBody(f"{field} must be a string")
    return fields


# [R-1.16] SPEC-1-B07f-a: the refusals the returns gate can raise, as
# MACHINE-READABLE CODES rather than as prose. A customer refused because the
# store takes no returns at all, and a customer refused because their own
# window ran out, are different facts and the owner's ruling (2026-10-02)
# requires the first to SAY SO: "returns aren't available", not "not eligible".
#
# Prose alone would not carry it. SPEC-1-B07f-b has to surface the reason on
# orders the customer is not attempting to return right now, and the storefront
# has to render it, and neither can be built on an English string that a copy
# edit would change. So the code is the contract and the sentence is the
# rendering of it.
RETURN_REFUSAL_NOT_ELIGIBLE = "not_eligible"
RETURN_REFUSAL_WINDOW_CLOSED = "window_closed"
RETURN_REFUSAL_OUTSIDE_WINDOW = "outside_window"

# The two refusals that are about THIS order keep one sentence, byte-identical
# to the body this seam has always returned: the caller owns the order either
# way, so naming which of the two fired would disclose nothing but would fork a
# contract the probes pin as one. The closed refusal is the exception the ruling
# asks for, and it is the only body that carries a refusal code.
RETURN_REFUSAL_ERRORS = {
    RETURN_REFUSAL_NOT_ELIGIBLE: "This order is not eligible for a return",
    RETURN_REFUSAL_OUTSIDE_WINDOW: "This order is not eligible for a return",
    RETURN_REFUSAL_WINDOW_CLOSED: "Returns aren't available for this order",
}

# WHERE the refusal code rides in the body, and it is not ``code``.
#
# ``common.errors.ErrorEnvelopeMiddleware`` rewrites every recognised error body
# into its one envelope, and ``_envelope`` ASSIGNS ``code`` from the status
# family (409 -> "conflict") while moving every other key into ``details``. A
# view that returned ``{"error": ..., "code": "window_closed"}`` therefore
# published ``code: "conflict"`` and buried its own value at
# ``details.code`` - the code was destroyed, and the thing that survived was an
# accident of dict ordering rather than a contract anyone chose.
#
# So the code travels under its own key, which the envelope carries into
# ``details`` - the place that module reserves for exactly this, "other
# context". A closed refusal reaches the client as
# ``{"error": ..., "code": "conflict", "details": {"return_refusal":
# "window_closed"}}``, and an expired one as the same envelope with
# ``details`` empty. The envelope's own ``code`` stays the status family for
# every other endpoint in this app; only the closed refusal adds context.
RETURN_REFUSAL_BODY_KEY = "return_refusal"


def _return_eligible(order):
    """Whether ``order`` has anything at all a customer could send back.

    DERIVED FROM THE MACHINE, NOT LISTED. The cycle-2 audit caught the earlier
    version of this gate refusing ``cancelled`` with the rationale "nothing was
    fulfilled" and then ACCEPTING ``pending`` for the identical reason. It had
    to accept it: ``LEGACY_STATUS_DIMENSIONS`` (orders.state) is the machine's
    own map of every status onto the two dimensions spec 10.2 declares, and it
    records ``pending`` and ``cancelled`` as the same row twice -
    ``("pending", "unfulfilled")`` both. So any rule that refuses a cancelled
    order for "never fulfilled, nothing to send back" necessarily refuses a
    pending one for exactly the same reason. Refusing one and admitting the
    other was a contradiction dressed as a policy; this is the honest form.

    THE RULE: an order is ineligible only while NEITHER dimension says anything
    happened - the money is not held AND nothing has shipped. Both halves are
    read off the row, and neither is a window or a threshold.

    THE MONEY HALF IS NOT ``payment_status == "captured"``. It is membership of
    ``CAPTURED_MONEY_PAYMENT_STATUSES`` (orders.state), which is itself derived
    from ``PAYMENT_ALLOWED_TRANSITIONS`` rather than written out here: the
    values reachable from the capture point that the machine can still move a
    refund out of. That is ``captured`` and ``partially_refunded``. Cycle 3's
    audit is why: ``PAYMENT_ALLOWED_TRANSITIONS`` places
    ``partially_refunded`` STRICTLY AFTER ``captured``, orders/models.py's
    shipped-edge precondition admits it ALONGSIDE ``captured`` as real money
    ("a capture minus a recorded refund"), and the SPEC-1-05 refund seam is the
    writer that produces it - and that seam writes ``payment_status`` and NEVER
    ``fulfilment_status``, so the row a partly-refunded order is in is exactly
    ``partially_refunded / unfulfilled``. Testing the money half against the
    single capture literal refused that row 409 "This order is not eligible for
    a return" on the strength of a rule whose own sentence said the money had
    moved - the code and the rationale disagreeing, which is the defect class
    this docstring exists to prevent.

    The other half of the money half is a deliberate exclusion rather than an
    oversight: ``refunded`` is the one captured-money value that is NOT here,
    because ``PAYMENT_ALLOWED_TRANSITIONS`` declares no edge out of it - the
    money has all gone back and there is nothing left to send anything against.
    That refusal is a policy, it is now said out loud here and pinned by
    ``test_every_machine_value_gets_the_answer_this_rationale_claims``.

    CONSEQUENCES, all of them read off that one rule rather than appended as
    special cases. The full enumeration of what the machine admits, both
    dimensions, is:

    * ``captured / unfulfilled`` and ``partially_refunded / unfulfilled``:
      ACCEPTED. Money is held, so there is a purchase to be returning against.
    * ``pending``, ``authorized`` and ``failed / unfulfilled``: REFUSED. The
      machine reaches ``failed`` only from ``pending`` and declares no capture
      there, so "never paid for" is the same first half of this predicate - a
      return against an order the store holds no money for is not a return.
      ``authorized`` is the gateway's step BEFORE capture, so the store holds
      no funds on it either.
    * ``refunded / unfulfilled``: REFUSED, per the exclusion above.
    * ANY payment value with ``fulfilled`` or ``partially_fulfilled``: ACCEPTED.
      The goods went out, which is the half of the disjunction that does not
      care about the money at all - it is why this is a disjunction and not
      "payment == captured": the machine WAIVES the capture precondition for
      COD orders and puts their capture point at delivery (``orders.models``),
      so a COD order that has SHIPPED must stay returnable while its money is
      still ``pending``.

    NO WINDOW IS A BUILD-ORDER STATE AND IS NOW OVERRULED. SPEC-1-B07b found
    the spec silent on the NUMBER - line 1083's "Return/refund request where
    eligible" and line 1959's "Eligibility validation" both describe a window a
    whole-file sweep for a day count cannot find (the single hit is about
    deployment cadence, not returns) - so B07b shipped without one rather than
    fabricate policy wearing a configuration key. The product owner has since
    REQUIRED a configurable site-wide return window, which overrides that
    outcome rather than complementing it, and SPEC-1-B07d owns it. The rule
    below is B07a's derivation with B07d's window clause bolted onto it; the
    derivation above is untouched, and the window is a SECOND, independent
    refusal rather than a rewrite of the first.

    * the number is MERCHANT-FACING SETTING, not deployment config - each store
      sets its own - so it lives in ``ops.SiteSettings``, NOT in env and NOT
      here (the page-size keys B07b DID add beside the deployment config stay
      env-driven, because a page density is a property of the deployment and
      does not vary per store; the two differ in kind, and that difference is
      why they do not share a home).
    * age is now an input, read through ``_return_window_refusal`` below, and
      the seam no longer answers "has anything happened yet" alone.
    * 0 IS NOT A SHORT WINDOW. SPEC-1-B07f-a carries the owner's ruling of
      2026-10-02: a published 0 CLOSES returns, and it closes them at every
      age including the exact anchor instant, which the old arithmetic
      admitted and then refused a second later. The closed check is a named
      state consulted before the date arithmetic, and the refusal it produces
      is a named one too, so a customer is never told their window expired
      when the store is not taking returns at all.
    """
    money_moved = order.payment_status in CAPTURED_MONEY_PAYMENT_STATUSES
    goods_moved = order.fulfilment_status != "unfulfilled"
    if not (money_moved or goods_moved):
        return False
    return _return_window_refusal(order, timezone.now()) is None


def _return_window_days():
    """The return window this store publishes, in days (SPEC-1-B07d).

    Read from ``ops.SiteSettings`` - the admin surface the merchant already
    has - and never from ``django.conf.settings``, because a window is store
    POLICY (each merchant sets its own) while this module's other settings
    reads are deployment shape. An unset column resolves to the documented
    default in the model's own method, so the number the gate enforces and the
    number ``/api/settings/`` publishes are one value by construction rather
    than two copies that could drift.

    A published ``CLOSED_RETURN_WINDOW_DAYS`` (0) is returned AS 0, deliberately
    not defaulted: it is a value this store publishes, and
    ``_return_window_refusal`` reads it as the closed state before it reaches
    any date arithmetic (SPEC-1-B07f-a).

    ``load()`` is a ``get_or_create`` on the singleton's pk, so the very first
    call on a store that has never saved the row INSERTS it. That happens
    inside the create seam's ``transaction.atomic`` below and is safe: the row
    is the pk-1 singleton, so the insert is idempotent and commits or rolls
    back with the surrounding attempt without leaving a second row.
    """
    return SiteSettings.load().resolved_return_window_days()


def _return_window_anchor(order):
    """The instant the window is measured FROM (SPEC-1-B07d).

    DELIVERY WHERE THE GOODS HAVE ARRIVED, THE ORDER'S OWN DATE OTHERWISE -
    and the fallback is not an edge case, it is the production case. As of
    this task ``orders.models`` says of ``delivered_at``, ``shipped_at`` and
    ``fulfilled_at``: "no writer touches them yet". The admin surface's
    mark_delivered does not stamp them, and ``admin_order_fulfill`` declines
    to invent a richer record than the admin surface for the same transition.
    So every real delivered row in this repo has ``delivered_at IS NULL``, and
    a window anchored on delivery alone would either refuse every order that
    has actually been delivered or measure from nothing at all.

    The two readings DIVERGE, and the divergence is the argument for this
    form. Take an order paid on day 0, dispatched on day 3 and delivered on
    day 20:

    * measured from ``created_at``: on day 50 it is 50 days old and refused,
      even though the customer has had the goods for 30 days;
    * measured from ``delivered_at``, with this fallback: on day 50 it is 30
      days past arrival and admitted, on the last day of its window.

    The chosen form is the GENEROUS of the two wherever they disagree, because
    ``delivered_at`` is never earlier than ``created_at``: where a delivery
    stamp exists it is the later anchor and buys the customer more time, and
    where it does not exist the order date is the only anchor there is. Its
    one asymmetry is the unshipped-but-admitted row - an order admitted on
    the money half alone (``captured / unfulfilled``), which has no delivery to
    anchor to - and there both readings would use the order date anyway, which
    is why a window shorter than the store's own packing latency would expire
    a paying customer's right before their parcel exists.
    """
    return order.delivered_at or order.created_at


def _returns_closed():
    """Whether this store has published ``CLOSED_RETURN_WINDOW_DAYS`` (0).

    Read for the SENTENCE the refusal carries, not for the verdict:
    ``_return_eligible`` is the single place that decides whether an order may
    be returned against, and it reaches its own answer through
    ``_return_window_refusal``. Asking it a second time for the reason would
    mean a second ``timezone.now()`` inside one request that has to be decided
    by ONE clock read, and a request whose refusal sentence disagreed with its
    own verdict is a worse bug than the extra read on the singleton this costs.

    It is read AFTER the verdict, and it OVERRIDES the order-specific reason,
    because it is a fact about the store rather than about the order: a closed
    store refuses every order it has, and "your 30 days ran out" would be a
    misleading thing to tell a customer whose window was never open.

    THE COMPARISON agrees with the gate's, and the READS do not have to. This
    applies ``return_window_days == CLOSED_RETURN_WINDOW_DAYS`` to the RAW
    column, while ``_return_window_refusal`` applies the same comparison to
    ``resolved_return_window_days()``, and for every value the column admits
    the two EXPRESSIONS are equal - NULL resolves to 30, which is neither
    closed nor equal to 0; 0 resolves to 0, which is both; N > 0 resolves to
    itself, which is neither.
    ``test_the_three_states_are_three_and_each_is_spelled_out_by_hand`` drives
    all of those values and asserts both expressions together.

    What is NOT claimed is that they are ONE read. They are two, each its own
    ``get_or_create`` of the singleton, and the ``atomic`` block plus
    ``select_for_update()`` in the seam locks the ORDER - not SiteSettings - so
    a merchant saving the window between the two reads can make them disagree.

    THE BOUND ON THAT, which is what makes it a bounded defect rather than a
    hole: this is reached only INSIDE the ``not _return_eligible`` branch, so
    the verdict is already decided and a stale read can only MISLABEL a
    refusal that has already happened. It cannot admit a return - there is no
    path from here back to an acceptance - so the seam fails CLOSED, and the
    only artefact of the race is the wrong sentence and a missing refusal code
    on a refusal the customer was getting anyway.

    Two reads are kept deliberately. Collapsing them would mean threading the
    verdict's own code back out of ``_return_eligible`` and re-deriving the
    machine half of eligibility in the seam, trading a bounded, fail-closed
    mislabel for two sources of truth about whether an order may be returned.
    """
    return SiteSettings.load().returns_closed()


def _return_window_refusal(order, now):
    """The window's verdict on ``order`` at ``now``: a refusal code or None.

    THE CLOSED CHECK COMES FIRST, and it is a named state rather than an
    accident of the arithmetic. Before SPEC-1-B07f-a this function returned
    ``now <= anchor + timedelta(days=window)`` and let a published 0 through
    that comparison, which admitted an order AT THE EXACT ANCHOR INSTANT and
    refused the same order one second later. The owner's ruling (2026-10-02) is
    that 0 means returns are closed, so a closed store refuses at every age and
    the date arithmetic below is not reached at all.

    Every OTHER published value takes exactly the path it took before: the
    window is INCLUSIVE of its last day, so ``<=`` and not ``<``, on the
    anchor ``_return_window_anchor`` chooses. The ruling changed the meaning of
    0 and nothing else.

    ``now`` is passed in rather than read here so the CALLER owns the single
    clock read of the whole predicate: one read means a request cannot be
    accepted by a gate that consulted one clock and refused by a gate that
    consulted a later one.

    WHO CONSUMES THE CODE, precisely, because it is NOT the seam. The only
    production caller is ``_return_eligible``, which reduces this to a boolean
    with ``is None`` and therefore DISCARDS which code it was: both refusal
    codes collapse to the same False there. The body the customer finally gets
    is re-derived by the seam from an INDEPENDENT read - ``_returns_closed()``
    - so at the seam the closed code is load-bearing and the other two are
    not, and ``RETURN_REFUSAL_OUTSIDE_WINDOW`` reaches no response body at
    all. Stated here because a reader of the vocabulary would otherwise
    assume all three codes steer the shipped response. They do not; what the
    seam distinguishes is closed against everything else, and that is the
    distinction the ruling asks for. It is also why the closed/expired
    distinguishability pin survives mutating either code name: the seam's
    choice is made from the store's policy, not from the string.
    """
    window = _return_window_days()
    if window == CLOSED_RETURN_WINDOW_DAYS:
        return RETURN_REFUSAL_WINDOW_CLOSED
    if now > _return_window_anchor(order) + timedelta(days=window):
        return RETURN_REFUSAL_OUTSIDE_WINDOW
    return None


def _return_request_miss():
    """The one answer every return-request LOOKUP failure gets.

    Byte-identical for: an order number that does not exist, an order number
    belonging to somebody else, and an order number belonging to a GUEST order.
    Any difference between those cases would make this endpoint an
    order-existence oracle for a stranger, which is the exact leak
    ``_guest_lookup_miss`` and ``shipping.views._tracking_lookup_miss`` were each
    built to close (conventions.md: uniform responses on anonymous flows, no
    existence leaks). 404 rather than 403 for the same reason: "forbidden"
    would confirm the order is real.

    Deliberately NOT the answer for a malformed request: a body ``_return_body``
    cannot read as text - no order number, a reason code outside the
    vocabulary, a non-string or ``null`` where text is required, a body that is
    not an object at all - is refused with a 400 BEFORE any lookup runs. That
    is now true of EVERY malformed shape and not just of a missing field or a
    bad vocabulary string (cycle 1 overclaimed it here, and the gap was real:
    a non-string field raised AttributeError instead of being refused). Those
    refusals are built from the request alone and are identical whatever the
    order number would have turned out to be, so they cannot disclose anything
    about an order - and folding them into this 404 would only hide a real
    input error from the customer who made it.
    """
    return Response(
        {"error": "Order not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def _return_request_payload(request_row):
    """The confirmation body this seam returns.

    Now driven by ``ReturnRequestSerializer`` (SPEC-1-B07b), so the create
    confirmation and the list/detail bodies are ONE representation. B07a built
    this inline as an explicit interim shape and left the real serializer to
    B07b precisely so there would not be a second body to keep in step; the
    five keys it returned are a strict SUBSET of the serializer's seven, so
    nothing B07a documented is lost and the leak assertions over its bytes
    still hold.
    """
    return ReturnRequestSerializer(request_row).data


def _return_detail_miss():
    """The one answer every return-request DETAIL lookup miss gets.

    Byte-identical for: a pk that does not exist, a pk belonging to another
    customer's order, and a pk belonging to a GUEST order's return (which
    cannot exist today - guest returns are structurally unreachable - but the
    same answer is what it would get).

    404, never 403, and never a different BODY, for the same reason
    ``_return_request_miss`` and ``order_detail`` are: "forbidden" would
    confirm the pk is real and is a stranger's, which is precisely the IDOR
    oracle spec 17 line 4285 asks this API to be tested against. The lookup
    below folds ownership into the queryset so a stranger's row is simply not
    in the result set - it never reaches a view branch that could answer
    differently from a nonexistent pk.

    Deliberately its OWN helper rather than a reuse of ``_return_request_miss``:
    the two answer about different resources, so a future change to one miss
    contract (e.g. the order seam gaining a field) cannot silently move the
    detail seam's body with it. Each names the resource it is missing.
    """
    return Response(
        {"error": "Return request not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def _returns_page_size(raw):
    """Resolve the ?page_size query param for the returns listing.

    Identical in shape and intent to ``_history_page_size`` above: an integer
    in [1, RETURNS_HISTORY_MAX_PAGE_SIZE]; anything unparseable or non-positive
    falls back to the configured default, and an over-cap request is clamped to
    the cap. Not shared with the order-history resolver because each surface
    has its OWN configured default and cap in settings - the store may want a
    50-row returns page and a 10-row order page - and threading two setting
    names through one helper would be a signature that reads as though the two
    listings share one knob.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return settings.RETURNS_HISTORY_PAGE_SIZE
    if value < 1:
        return settings.RETURNS_HISTORY_PAGE_SIZE
    return min(value, settings.RETURNS_HISTORY_MAX_PAGE_SIZE)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def return_requests(request):
    """[R-1.16] SPEC-1-B07a/B07b: the customer's returns family root.

    GET lists the caller's own returns and POST creates one, because Django
    resolves the FIRST matching url pattern - two patterns on ``returns/``
    would leave the second unreachable behind the first (a listing that answers
    405 to every GET). One pattern, one dispatch.

    Splitting the work into two plain functions rather than an if/else with two
    bodies is what keeps SPEC-1-B07a's create contract intact: its function is
    byte-for-byte the one B07a shipped (only its decorators moved up here), and
    its docstring travels with it. ``permission_classes`` is declared ONCE, on
    this view, rather than repeated on two functions that are never routed
    independently - the single authorization point for the whole family
    (conventions.md:14).
    """
    if request.method == "POST":
        return _create_return_request(request)
    return _list_return_requests(request)


def _list_return_requests(request):
    """[R-1.16] SPEC-1-B07b: GET .../returns/ - the caller's OWN returns.

    The one sentence this exists to satisfy: spec 4 line 1083 puts "Return/refund
    request where eligible" on the account's Orders page, and spec 4 line 1045
    gives that page its own ``/account/returns`` route - a page that has to
    show the customer the returns they have already asked for, not only the one
    they can create.

    OWNERSHIP IS THE FILTER, not a check after the fetch. Scoping on
    ``order__user=request.user`` means a stranger's row is never in the
    queryset at all, so there is no branch in this code that could answer
    differently for "yours" and "theirs". The same filter makes GUEST returns
    structurally unreachable: a guest order's ``user`` is NULL and can never
    equal an authenticated user, so a NULL-user row cannot appear here. (Guest
    returns are not offered by decision - SPEC-1-B04 gives the guest browsing
    and checkout only.)

    A LIST, never a single row. This is the trap SPEC-1-B06's split-shipment bug
    was (a listing that reported only the newest row, so an older parcel
    vanished). ``.first()`` here would answer "the returns this customer has"
    with an arbitrary one of them and silently drop the rest; the
    ``RETURN_OPEN_STATUSES`` probe in the create path exists to prevent exactly
    that shape of loss on the write side.

    PAGINATED, in the house page-number envelope with the ORDER_HISTORY
    settings convention (SPEC-9-04 [R-9.2.14]): ``settings`` supplies the
    default page size and the cap, a ``?page_size`` caller is clamped to the
    cap, and an unparsable page falls back rather than 404-ing.
    conventions.md forbids a hardcoded page size, so the number lives in
    config/settings.py (env-driven) and not here.

    The queryset is explicitly ``order_by``-ed on the model's ``-created_at,
    -id`` with the unique-id tiebreaker, so the sort is TOTAL and a paginated
    partition can never repeat or skip a row across requests (the reasoning
    ``order_list`` records). ``ReturnRequest.Meta.ordering`` already declares
    that exact pair, and it is re-stated here so the guarantee does not depend
    on a model default that a future edit could change silently.
    """
    returns = (
        ReturnRequest.objects.filter(order__user=request.user)
        .select_related("order")
        .order_by("-created_at", "-id")
    )

    paginator = Paginator(
        returns,
        _returns_page_size(request.query_params.get(RETURNS_PAGE_SIZE_QUERY_PARAM)),
    )
    # get_page never raises: an unparsable page falls back to 1, a page past
    # the end to the last page - no 404 for a stale page link.
    page = paginator.get_page(request.query_params.get("page", 1))

    # House page-number envelope (products-listing parity).
    return Response(
        {
            "count": paginator.count,
            "total_pages": paginator.num_pages,
            "current_page": page.number,
            "next_page": page.has_next(),
            "previous_page": page.has_previous(),
            "results": ReturnRequestSerializer(page.object_list, many=True).data,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def return_request_detail(request, return_request_id):
    """[R-1.16] SPEC-1-B07b: GET .../returns/<pk>/ - ONE of the caller's returns.

    The classic IDOR surface, so the lookup is ownership-scoped and the miss is
    ONE uniform 404 (``_return_detail_miss``). A stranger probing another
    customer's return by its pk gets the byte-identical body and status a
    nonexistent pk gets - never a 200 (the leak) and never a 403 that confirms
    the pk is real (the existence oracle). This is exactly the property spec 17
    line 4285 asks to be tested ("insecure direct object references (IDOR),
    especially in order, address, return and customer APIs"), and the probe is
    in tests_returns.ReturnReadIdorTests.

    Account-only and guest-blind by the same structural argument as the list:
    the filter is ``order__user=request.user``, so a NULL-user guest order's
    row cannot match. It never traverses the order to decide anything - it asks
    the database for rows this caller owns and nothing else.
    """
    try:
        return_row = ReturnRequest.objects.select_related("order").get(
            pk=return_request_id,
            order__user=request.user,
        )
    except ReturnRequest.DoesNotExist:
        return _return_detail_miss()

    return Response(ReturnRequestSerializer(return_row).data)


def _create_return_request(request):
    """[R-1.16] POST /api/v1/store/orders/returns/ - request a return.

    SPEC-1-B07b moves this off ``return_requests`` (the family root dispatches
    here on POST) and off its own url pattern; the function body below is
    otherwise byte-for-byte B07a's, and every contract that shipped with it -
    the uniform miss, the type gate, the eligibility rule, the atomic
    duplicate guard - is unchanged by the move. Its docstring travels with the
    body so the reasoning is not stranded in a wrapper that no longer exists.

    Spec 4 line 1083 makes the return request a customer self-service feature
    ("Return/refund request where eligible", under the account's Orders page and
    line 1045's ``/account/returns``). The body names the order by its
    CUSTOMER-FACING reference, never by pk, and the reference alone authorizes
    nothing: the lookup below filters on ``user=request.user``, so a hit is by
    definition one of the caller's own account orders.

    ACCOUNT-ONLY, deliberately. The spec routes returns under the account
    section (line 1045) and gives the guest (line 74) browsing and checkout
    only - it never names returns among the guest's powers - so this seam does
    not accept the B04 guest token. A guest order is therefore not addressable
    here at all, and naming one answers the uniform miss above like any other
    miss. Half-building a guest path (a token accepted for orders but a
    different answer for guest rows) would have been worse than none.

    NOTHING HERE MOVES MONEY. No ``Refund`` is created, no ``payment_status``
    and no ``total_amount`` changes: spec 6.8 line 1985 ("Never equate 'refund
    requested' with 'refund completed'") makes the refund seam (SPEC-1-05,
    ``refunds.create``) the only writer of the payment dimension, and approval
    is a separate staff act rather than something this request implies.

    DUPLICATES ARE PREVENTED, atomically, twice over. The Order row is locked
    for the whole attempt and the existing open request for it is probed under
    that lock (a second, concurrent insert for the same order is caught by the
    partial unique index on ``ReturnRequest.order``), and a racing insert that
    still slips through raises IntegrityError, which the savepoint catches and
    answers with the same 409. So one order carries at most one OPEN request
    (``RETURN_OPEN_STATUSES``) at a time; after it is rejected or closed a
    later request may be filed.

    Not idempotent by design: each accepted request is one visible customer
    ask. A retry that arrives after the first committed is the 409 above, which
    is honest about what happened rather than pretending to create a second one.

    No throttle scope, DECLINED ON AUTHORIZATION GROUNDS - and the reason is
    what the seam IS, not what its tests happen to do. conventions.md requires
    a scope on public MUTATING endpoints; this one is ``IsAuthenticated`` and
    never ``AllowAny``, so it is not that class of endpoint. A throttle would
    buy little here specifically: an anonymous caller cannot reach the seam at
    all, and a caller looping over their OWN orders cannot amplify rows - the
    partial unique index allows at most one OPEN request per order, so the
    write is bounded by the number of orders the account owns rather than by
    the number of requests it sends.

    The cost a future scope would carry is real, and it is recorded here as
    that - a cost to solve, NOT the reason to skip the scope. The enumeration
    probe that proves the eligibility gate answers for EVERY payment x
    fulfilment pair issues ~18 requests inside one test, and
    ``ScopedRateThrottle`` keys on user pk, so any rate worth having (the
    repo's existing ones run 5-120/min) would 429 it. Restructuring that probe
    - distinct users per cell, or a cache reset between cells - is precisely
    the work a throttle on this seam would require. Until then the two
    authorization facts above are the decision, and the probe cost is the
    reason the eventual scope will need test work rather than a one-line
    decorator.
    """
    try:
        body = _return_body(request)
    except MalformedReturnRequestBody as malformed:
        return Response(
            {"error": malformed.error},
            status=status.HTTP_400_BAD_REQUEST,
        )
    order_number = body["order_number"]
    reason_code = body["reason_code"]
    reason_note = body["reason_note"]

    # Input validation runs BEFORE the order lookup and answers 400s that are
    # identical whatever the order number turns out to be, so no combination of
    # refusals can disclose whether an order exists.
    if not order_number:
        return Response(
            {"error": "order_number is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if reason_code not in ReturnRequest.ReasonCode.values:
        return Response(
            {"error": "A valid reason_code is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        # select_for_update on the ORDER row, not on the return rows: every
        # requester for this order takes this lock first, so no other writer can
        # insert an open request between this probe and this commit. The order
        # number is unique, so get() cannot hide a second row behind the first.
        try:
            order = Order.objects.select_for_update().get(
                order_number=order_number,
                user=request.user,
            )
        except Order.DoesNotExist:
            # One shape for "no such order", "not yours" and "a guest order".
            return _return_request_miss()

        if not _return_eligible(order):
            # Two INDEPENDENT refusals answer 409 with the same body: the
            # machine's dimensions (see _return_eligible - a pending and a
            # cancelled order are refused for the same reason because the
            # machine maps them onto the same dimension pair) and the store's
            # return window. One body for both is deliberate - the caller owns
            # the order either way, so naming which of the two fired would
            # disclose nothing but would fork a contract the tests pin as one,
            # and "not eligible" is the honest sentence for an order past its
            # deadline. A never-paid one is refused on the capture half of the
            # machine test, whichever status it happens to wear.
            #
            # SPEC-1-B07f-a adds the THIRD refusal, and it is the one refusal
            # here that is about the store rather than the order: returns closed
            # outright. The store-wide fact is what the customer needs to know and
            # an order-specific sentence would misdescribe it, so it overrides the
            # reason the verdict would otherwise have given. The code travels under
            # RETURN_REFUSAL_BODY_KEY - see there for why it cannot be ``code`` -
            # so B07f-b and the storefront can tell this apart from the two above
            # without matching on English.
            if _returns_closed():
                return Response(
                    {
                        "error": RETURN_REFUSAL_ERRORS[RETURN_REFUSAL_WINDOW_CLOSED],
                        RETURN_REFUSAL_BODY_KEY: RETURN_REFUSAL_WINDOW_CLOSED,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(
                {"error": RETURN_REFUSAL_ERRORS[RETURN_REFUSAL_NOT_ELIGIBLE]},
                status=status.HTTP_409_CONFLICT,
            )

        already_open = ReturnRequest.objects.filter(
            order=order,
            status__in=RETURN_OPEN_STATUSES,
        ).exists()
        if already_open:
            return Response(
                {"error": "This order already has an open return request"},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            # The savepoint is what makes the racing-insert refusal recoverable:
            # an IntegrityError raised inside this transaction would otherwise
            # poison the outer block, so the duplicate would be reported as a
            # 500 instead of the 409 it is.
            with transaction.atomic():
                return_request = ReturnRequest.objects.create(
                    order=order,
                    reason_code=reason_code,
                    reason_note=reason_note,
                )
        except IntegrityError:
            return Response(
                {"error": "This order already has an open return request"},
                status=status.HTTP_409_CONFLICT,
            )

    return Response(
        _return_request_payload(return_request),
        status=status.HTTP_201_CREATED,
    )

"""Checkout: the order lifecycle's opening move, plus the two public reads that
bracket it (the guest's own order, and the coupon preview).

One of six modules split out of the former single-file `orders/views.py`. The
two collapse guards (the SPEC-21-1 accidental-duplicate window and SPEC-9-01's
header-keyed idempotency) and the guest-credential invariant both live here
because they are properties of ORDER CREATION, not of payment: the token is
minted by the 201 below and disclosed by that response alone.

Nothing in this module was rewritten - the bodies moved verbatim - so the
guarantees documented on each helper are the ones the split inherits.
"""

import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
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

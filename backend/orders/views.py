from datetime import timedelta
from decimal import Decimal

from django.contrib.admin.models import CHANGE
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from rest_framework.decorators import api_view, permission_classes, throttle_scope
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

# [R-10.1] The order machine (transition table, gate, fulfilment step map)
# lives in orders.state — the single source; views only consume it.
from .models import Order, OrderItem, Coupon
from .serializers import OrderSerializer
from .state import ADMIN_FULFILMENT_NEXT, ALLOWED_TRANSITIONS, transition_allowed
# [R-10.1] SPEC-10-01b: dimension mappings for the writers. Kept as its own
# line so every hunk in this file stays insertion-only.
from .state import fulfilment_for_status, payment_for_status

from cart.models import Cart
from common import notifications
from common.audit import log_api_action
from common.models import AuditEvent
from common.money import quantize_money
from common.permissions import HasOrdersCancel, HasOrdersFulfill, HasOrdersRead
from products.models import StockMovement, products

import logging
import razorpay
from django.conf import settings
from django.contrib.auth.models import User

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


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_list(request):

    orders = Order.objects.filter(
        user=request.user
    ).order_by('-created_at')

    # SPEC-9-04: the unique id tiebreaker makes the sort total, so a
    # paginated partition never repeats or skips a row across requests
    # (same reasoning as the products listing's F-12 fix).
    orders = orders.order_by('-created_at', '-id')

    paginator = Paginator(
        orders, _history_page_size(
            request.query_params.get(HISTORY_PAGE_SIZE_QUERY_PARAM)
        )
    )
    # get_page never raises: an unparsable page falls back to 1, a page
    # past the end to the last page — no 404 for a stale page link.
    page = paginator.get_page(request.query_params.get('page', 1))

    serializer = OrderSerializer(
        page.object_list,
        many=True
    )

    # House page-number envelope (products-listing parity).
    return Response({
        'count': paginator.count,
        'total_pages': paginator.num_pages,
        'current_page': page.number,
        'next_page': page.has_next(),
        'previous_page': page.has_previous(),
        'results': serializer.data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_detail(request, order_id):
    """[R-9.2.15] GET /account/orders/:id — the caller's OWN order only.

    Ownership is part of the lookup itself: a foreign user's order (and an
    unknown id alike) gets the same uniform 404 — never a 200 (the IDOR
    pin) and never a 403 that would confirm the id's existence
    (conventions.md: no existence leaks)."""
    try:
        order = Order.objects.get(
            id=order_id,
            user=request.user
        )

    except Order.DoesNotExist:
        return Response(
            {"error": "Order not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    serializer = OrderSerializer(order)

    return Response(
        serializer.data
    )


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


def _find_duplicate_pending_order(user, cart_items, coupon, payload):
    """[R-21.2.6] The accidental-duplicate window: checkout never clears the
    cart (cleanup happens after payment confirmation), so a double-click or
    client retry resubmits the byte-identical payload while the first order
    is still payable -- which used to mint a second payable order and with
    it a second charge target. Returns the user's recent pending order with
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
            user=user,
            status="pending",
            created_at__gte=cutoff,
        )
        # payable gate mirrors verify_payment: a pending order with a
        # payment id attached has already been taken past this point
        .filter(Q(razorpay_payment_id__isnull=True) | Q(razorpay_payment_id=""))
        .prefetch_related("items")
        .order_by("-created_at")
    )
    lines = _order_lines(cart_items)
    shipping = tuple(payload.get(field) for field in _SHIPPING_FIELDS)
    for candidate in candidates:
        if _order_lines(candidate.items.all()) != lines:
            continue
        if candidate.coupon_id != (coupon.pk if coupon else None):
            # a deliberate coupon difference is a new purchase, not a retry
            continue
        if tuple(getattr(candidate, field) for field in _SHIPPING_FIELDS) != shipping:
            continue
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


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_order(request):

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
            {"error": "Idempotency-Key is too long"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # =========================
    # Get current session
    # =========================

    if not request.session.session_key:
        return Response(
            {"error": "Cart not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    session_id = request.session.session_key

    # =========================
    # Find cart
    # =========================

    try:
        cart = Cart.objects.get(
            session_id=session_id
        )

    except Cart.DoesNotExist:
        return Response(
            {"error": "Cart not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    # =========================
    # Get cart items
    # =========================

    cart_items = cart.items.select_related(
        'product'
    ).all()

    if not cart_items.exists():
        return Response(
            {"error": "Cart is empty"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # =========================
    # Validate checkout data
    # =========================

    required_fields = [
        'full_name',
        'phone',
        'address',
        'city',
        'state',
        'pincode',
    ]

    for field in required_fields:

        if not request.data.get(field):

            return Response(
                {
                    "error": f"{field} is required"
                },
                status=status.HTTP_400_BAD_REQUEST
            )

    # =========================
    # Calculate cart subtotal
    # =========================

    # SPEC-6-01 [6.2.22]: a cart line that outlasted its stock (stock can
    # drop after the item was added) must not become an order the customer
    # can pay for. This gate is advisory and read-only -- stock can still
    # change between create and pay, so verify_payment re-checks under a
    # row lock before decrementing; that remains the authoritative backstop.
    unavailable = []

    subtotal_amount = Decimal('0.00')

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

        subtotal_amount += (
            product.price * cart_item.quantity
        )

    if unavailable:

        details = ", ".join(
            f'"{item["name"]}" (requested {item["requested"]}, '
            f'only {item["available"]} in stock)'
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
            status=status.HTTP_400_BAD_REQUEST
        )

    # =========================
    # Coupon
    # =========================

    coupon = None
    discount_amount = Decimal('0.00')

    coupon_code = request.data.get('coupon_code')

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
            coupon = Coupon.objects.get(
                code__iexact=coupon_code
            )

        except Coupon.DoesNotExist:

            return Response(
                {"error": "Invalid coupon code"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check active
        if not coupon.active:

            return Response(
                {"error": "This coupon is inactive"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check validity dates
        now = timezone.now()

        if now < coupon.valid_from:

            return Response(
                {"error": "This coupon is not active yet"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if now > coupon.valid_until:

            return Response(
                {"error": "This coupon has expired"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check usage limit
        if (
            coupon.usage_limit is not None
            and coupon.used_count >= coupon.usage_limit
        ):

            return Response(
                {"error": "This coupon has reached its usage limit"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check minimum order amount
        if subtotal_amount < coupon.minimum_order_amount:

            return Response(
                {
                    "error": "Minimum order amount is required",
                    "minimum_order_amount": coupon.minimum_order_amount
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # Calculate discount
        if coupon.discount_type == 'percentage':

            # Quantize before any comparison or storage: the raw division
            # carries extra decimal places, and an unquantized discount
            # drifts the display, the audit trail and the stored 2-dp
            # order amount apart (F-11).
            discount_amount = quantize_money(
                (subtotal_amount * coupon.discount_value) / Decimal('100')
            )

            if coupon.maximum_discount is not None:

                discount_amount = min(
                    discount_amount,
                    coupon.maximum_discount
                )

        else:

            discount_amount = coupon.discount_value

        # Never discount more than subtotal
        discount_amount = min(
            discount_amount,
            subtotal_amount
        )

    # =========================
    # Final total
    # =========================

    total_amount = (
        subtotal_amount - discount_amount
    )

    # =========================
    # Create order
    # =========================

    with transaction.atomic():

        # [R-21.2.6] Serialize same-session submissions on the cart row: two
        # rapid POSTs queue here, so the loser re-runs the dedup lookup after
        # the winner has committed and collapses onto the same order instead
        # of minting a second payable one. The cart row is locked only on
        # this path (verify_payment locks Order -> Products -> Coupon), so no
        # new lock-order cycle is introduced.
        cart = Cart.objects.select_for_update().get(pk=cart.pk)

        duplicate = _find_duplicate_pending_order(
            request.user, cart_items, coupon, request.data
        )

        if duplicate is not None:
            # Benign double-submit retry, so INFO: a WARNING here would spam
            # the log on every impatient re-click (same judgement as
            # verify_payment's already-processed skip).
            logger.info(
                "Checkout dedup: order %s replayed for user %s "
                "(identical pending submission)",
                duplicate.id,
                request.user.pk,
            )

            serializer = OrderSerializer(duplicate)

            return Response(
                serializer.data,
                status=status.HTTP_200_OK
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
            User.objects.select_for_update().get(pk=request.user.pk)

            replay = Order.objects.filter(
                user=request.user, idempotency_key=idempotency_key
            ).first()

            if replay is not None:
                # Benign keyed retry, so INFO: same judgement as the dedup
                # guard's collapse log above.
                logger.info(
                    "Checkout idempotency: order %s replayed for user %s "
                    "(Idempotency-Key)",
                    replay.id,
                    request.user.pk,
                )

                serializer = OrderSerializer(replay)

                return Response(
                    serializer.data,
                    status=status.HTTP_200_OK
                )

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
                        user=request.user,
                        full_name=request.data.get('full_name'),
                        phone=request.data.get('phone'),
                        address=request.data.get('address'),
                        city=request.data.get('city'),
                        state=request.data.get('state'),
                        pincode=request.data.get('pincode'),
                        coupon=coupon,
                        discount_amount=discount_amount,
                        total_amount=total_amount,
                        order_number=candidate,
                    )
            except IntegrityError:
                # Lost the number race: the unique constraint rejected the
                # candidate, so the savepoint above rolled the failed insert
                # back and this transaction stays usable for the retry. The
                # bound is a safety net, not the expected path: one retry
                # converges because the collision window is a single insert.
                if attempt == ORDER_NUMBER_ATTEMPTS - 1:
                    raise
                continue
            break

        # [R-9.3.14] SPEC-9-01: bind the submission key to the freshly
        # minted order inside this same transaction, so a later replay's
        # probe above finds it and collapses. The write is an UPDATE of the
        # row this transaction just created, after the probe proved no
        # committed order holds (user, key) and with the user-row lock
        # excluding a concurrent keyed writer -- so the unique constraint
        # cannot reject here.
        if idempotency_key is not None:
            order.idempotency_key = idempotency_key
            order.save(update_fields=['idempotency_key'])

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
                sku='',
                variant_name=product.name,
                price=price,
                quantity=quantity,
                subtotal=item_subtotal
            )

        # [R-7.20] Business-event trail: the order's creation is recorded in
        # the same transaction as the order rows, so a rolled-back checkout
        # leaves no phantom trail row and a committed order is never
        # trail-less.
        AuditEvent.record(
            AuditEvent.EventType.ORDER_CREATED,
            actor=request.user,
            order=order,
            detail={
                "order_id": order.id,
                "total_amount": str(order.total_amount),
                "coupon": coupon.code if coupon else None,
                "item_count": len(cart_items),
            },
        )

    # =========================
    # Return order
    # =========================

    serializer = OrderSerializer(order)

    return Response(
        serializer.data,
        status=status.HTTP_201_CREATED
    )


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
        {"error": "Invalid coupon code"},
        status=status.HTTP_400_BAD_REQUEST
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
        coupon = Coupon.objects.get(
            code__iexact=code
        )

    except Coupon.DoesNotExist:
        return None, None, _uniform_coupon_rejection()

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
        return None, None, _uniform_coupon_rejection()

    # Calculate cart subtotal
    subtotal = Decimal('0.00')

    for item in cart.items.select_related('product'):
        subtotal += (
            item.product.price * item.quantity
        )

    # Check minimum order amount
    if subtotal < coupon.minimum_order_amount:
        return None, None, _uniform_coupon_rejection()

    return coupon, subtotal, None


@api_view(['POST'])
@throttle_scope('coupon')
def apply_coupon(request):

    code = request.data.get('code')

    if not code:
        return Response(
            {"error": "Coupon code is required"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Resolve the cart before the coupon: otherwise a caller with no cart
    # could still probe code existence by watching for the coupon error
    # instead of the cart error. With the cart first, every cartless caller
    # gets the same answer for every code.
    if not request.session.session_key:
        return Response(
            {"error": "Cart not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    session_id = request.session.session_key

    try:
        cart = Cart.objects.get(
            session_id=session_id
        )

    except Cart.DoesNotExist:
        return Response(
            {"error": "Cart not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    # From here on every rejection shares one uniform response: unknown,
    # inactive, not-yet-valid, expired, usage limit, and below minimum.
    coupon, subtotal, rejection = validate_redeemable_coupon(code, cart)

    if rejection is not None:
        return rejection

    # Calculate discount
    if coupon.discount_type == 'percentage':

        # Same quantize parity as checkout (F-11): the preview must show
        # the exact 2-dp discount the order will store.
        discount = quantize_money(
            (subtotal * coupon.discount_value) / Decimal('100')
        )

        if coupon.maximum_discount is not None:
            discount = min(
                discount,
                coupon.maximum_discount
            )

    else:

        discount = coupon.discount_value

    # Never allow discount greater than subtotal
    discount = min(
        discount,
        subtotal
    )

    final_total = subtotal - discount

    return Response({
        "coupon": coupon.code,
        "subtotal": subtotal,
        "discount": discount,
        "final_total": final_total
    })
# ==================================
# Create Razorpay Payment
# ==================================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_payment(request):

    order_id = request.data.get('order_id')

    if not order_id:
        return Response(
            {"error": "order_id is required"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Find user's order
    try:
        order = Order.objects.get(
            id=order_id,
            user=request.user
        )

    except Order.DoesNotExist:
        return Response(
            {"error": "Order not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    # Payment may only be started for an unpaid order.
    if order.status != 'pending':
        return Response(
            {"error": "This order cannot be paid"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Razorpay client
    client = razorpay.Client(
        auth=(
            settings.RAZORPAY_KEY_ID,
            settings.RAZORPAY_KEY_SECRET
        )
    )

    # Amount must be in paise
    amount = int(
        order.total_amount * Decimal('100')
    )

    if order.razorpay_order_id:
        razorpay_order_id = order.razorpay_order_id
    else:
        try:
            # [R-8.11] The gateway is charged in the denomination the order
            # was minted with, read off the row — never a hardcoded code.
            razorpay_order = client.order.create({
                'amount': amount,
                'currency': order.currency,
                'receipt': f'order_{order.id}',
            })
        except Exception:
            # [SPEC-7-02] Without this, a gateway/network failure surfaces
            # only as a bare 500 with no order reference; the re-raise
            # preserves the 500 semantics exactly.
            logger.exception(
                "Payment intent creation failed for order %s", order.id
            )
            raise
        razorpay_order_id = razorpay_order['id']
        # [R-7.20] First persistence of the gateway intent is a payment
        # event: the intent and its trail row commit together, so a crash
        # between the two cannot leave an intent the trail never saw. The
        # reuse path above writes nothing, so it emits nothing.
        with transaction.atomic():
            order.razorpay_order_id = razorpay_order_id
            order.save(update_fields=['razorpay_order_id'])
            AuditEvent.record(
                AuditEvent.EventType.PAYMENT_INITIATED,
                actor=request.user,
                order=order,
                detail={
                    "razorpay_order_id": razorpay_order_id,
                    "amount_paise": amount,
                },
            )

    return Response({
        "order_id": order.id,
        "razorpay_order_id": razorpay_order_id,
        "amount": amount,
        "amount_in_rupees": order.total_amount,
        # [R-8.11] Same currency the gateway payload used: the order's own.
        "currency": order.currency,
        "key_id": settings.RAZORPAY_KEY_ID,
    })
# ==================================
# Verify Razorpay Payment
# ==================================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def verify_payment(request):

    razorpay_order_id = request.data.get(
        'razorpay_order_id'
    )

    razorpay_payment_id = request.data.get(
        'razorpay_payment_id'
    )

    razorpay_signature = request.data.get(
        'razorpay_signature'
    )

    if not all([
        razorpay_order_id,
        razorpay_payment_id,
        razorpay_signature
    ]):
        return Response(
            {"error": "Payment details are required"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Verify payment signature
    client = razorpay.Client(
        auth=(
            settings.RAZORPAY_KEY_ID,
            settings.RAZORPAY_KEY_SECRET
        )
    )

    try:

        client.utility.verify_payment_signature({
            'razorpay_order_id': razorpay_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature
        })

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

        return Response(
            {"error": "Payment verification failed"},
            status=status.HTTP_400_BAD_REQUEST
        )

    order_id = request.data.get('order_id')
    if not order_id:
        return Response(
            {"error": "order_id is required"},
            status=status.HTTP_400_BAD_REQUEST
        )

    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().select_related('coupon').get(
                id=order_id,
                user=request.user
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
                {"error": "Order not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        if order.status != 'pending' or order.razorpay_payment_id:

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
                status=status.HTTP_400_BAD_REQUEST
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
                "Payment verify failed: order %s is bound to gateway "
                "order %s, not %s",
                order.id,
                order.razorpay_order_id,
                razorpay_order_id,
            )

            return Response(
                {"error": "Payment does not belong to this order"},
                status=status.HTTP_400_BAD_REQUEST
            )

        order_items = list(order.items.all())
        product_ids = [item.product_id for item in order_items]
        locked_products = {
            product.id: product
            for product in products.objects.select_for_update().filter(id__in=product_ids)
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

                return Response(
                    {"error": "An item is no longer available in the requested quantity"},
                    status=status.HTTP_409_CONFLICT
                )

        coupon = order.coupon
        if coupon:
            coupon = Coupon.objects.select_for_update().get(pk=coupon.pk)
            now = timezone.now()
            if (
                not coupon.active
                or now < coupon.valid_from
                or now > coupon.valid_until
                or (coupon.usage_limit is not None and coupon.used_count >= coupon.usage_limit)
            ):

                AuditEvent.record(
                    AuditEvent.EventType.PAYMENT_COUPON_INVALID,
                    actor=request.user,
                    order=order,
                    detail={"coupon_id": coupon.pk},
                )

                # Same race shape as the stock conflict: INFO.
                logger.info(
                    "Payment verify failed: order %s coupon %s no longer "
                    "valid",
                    order.id,
                    coupon.pk,
                )

                return Response(
                    {"error": "The coupon is no longer valid"},
                    status=status.HTTP_409_CONFLICT
                )

        for item in order_items:
            product = locked_products[item.product_id]
            product.stock -= item.quantity
            product.save(update_fields=['stock'])
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
            coupon.save(update_fields=['used_count'])

        # [R-8.16] paid_at is the business-event timestamp of exactly this
        # transition, so it is written beside it inside the same atomic
        # block (rollback together). The already-processed gate above makes
        # a replay unreachable here; the or-guard pins "written exactly
        # once, never mutated" even if a future path re-enters.
        order.paid_at = order.paid_at or timezone.now()
        order.status = 'confirmed'
        order.razorpay_payment_id = razorpay_payment_id
        order.save(update_fields=['status', 'razorpay_payment_id', 'paid_at'])
        # [R-10.1] SPEC-10-01b: the payment dimension is captured by the
        # same confirmed-payment event (the only payment-dimension writer
        # in this batch — COD/failure states are SPEC-10-04). The save
        # above is a byte-frozen region (SPEC-8-04), so the dimension rides
        # this second persistence of the already-locked row in the SAME
        # atomic block: both UPDATEs commit or roll back together, and the
        # already-processed gate keeps this path unreachable on replay.
        order.payment_status = payment_for_status('confirmed')
        order.save(update_fields=['payment_status'])

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
        # hook, inside this same atomic block (rollback-together, like
        # record). dispatch never raises: a send failure is logged on the
        # notifications channel and the captured payment stays confirmed
        # (the SMTP-503 account-still-created behavior, mirrored).
        notifications.dispatch(
            AuditEvent.EventType.ORDER_PAID,
            {"order": order},
        )

    return Response({
        "message": "Payment verified successfully",
        "order_id": order.id,
        "status": order.status,
        "razorpay_payment_id": razorpay_payment_id
    })


# ==================================
# Admin orders JSON seam (SPEC-9-07, spec 9.4 Orders module)
# ==================================

# SPEC-9-07: the fulfilment endpoint drives the order one legal step per
# call along the flow the admin surface's bulk actions encode.
# ADMIN_FULFILMENT_NEXT (the step map) and transition_allowed (the gate)
# live in orders.state — [R-10.1] single source.


@api_view(['GET'])
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
        orders, _history_page_size(
            request.query_params.get(HISTORY_PAGE_SIZE_QUERY_PARAM)
        )
    )
    page = paginator.get_page(request.query_params.get('page', 1))

    serializer = OrderSerializer(page.object_list, many=True)
    return Response({
        'count': paginator.count,
        'total_pages': paginator.num_pages,
        'current_page': page.number,
        'next_page': page.has_next(),
        'previous_page': page.has_previous(),
        'results': serializer.data,
    })


@api_view(['GET'])
@permission_classes([HasOrdersRead])
def admin_order_detail(request, order_id):
    """[R-9.4.9] GET /api/admin/orders/:id/ — one order, staff eyes only.

    Unlike the customer detail endpoint there is no ownership scoping to
    enforce, so an unknown id is a plain uniform 404 (never an existence
    leak — the caller has already passed the orders.read gate)."""
    try:
        order = Order.objects.select_related("coupon").get(id=order_id)
    except Order.DoesNotExist:
        return Response(
            {"error": "Order not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    return Response(OrderSerializer(order).data)


@api_view(['POST'])
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
    record than the admin surface for the same transition."""
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(id=order_id)
        except Order.DoesNotExist:
            return Response(
                {"error": "Order not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        target = ADMIN_FULFILMENT_NEXT.get(order.status)
        if target is None or not transition_allowed(order.status, target):
            allowed = ", ".join(sorted(ALLOWED_TRANSITIONS.get(order.status, set())))
            return Response(
                {
                    "error": f"Order cannot be fulfilled from status "
                             f"'{order.status}'",
                    "allowed": allowed,
                },
                status=status.HTTP_409_CONFLICT
            )

        order.status = target
        # [R-10.1] SPEC-10-01b: the fulfilment dimension rides the same
        # transition. Insertion-only hunk (the 9-07 save below stays
        # byte-identical), so the dimension persists via a second
        # same-transaction write to the row locked above.
        order.fulfilment_status = fulfilment_for_status(target)
        order.save(update_fields=['status'])
        order.save(update_fields=['fulfilment_status'])
        # [6.12.6] API-side staff write: land the privileged-action record
        # the admin surface would have written (audit-log route reads it).
        log_api_action(
            request, order, CHANGE,
            f"Fulfilled via API: status moved to {target}.",
        )

    return Response({
        "message": f"Order status advanced to {target}",
        "order_id": order.id,
        "status": order.status,
    })


@api_view(['POST'])
@permission_classes([HasOrdersCancel])
def admin_order_cancel(request, order_id):
    """[R-9.4.11] POST /api/admin/orders/:id/cancel — cancel an unpaid order.

    The same machine gate the admin uses decides: only ``pending`` carries
    a cancel edge (cancelling a paid order is deliberately impossible until
    refunds exist — the 409 says so, mirroring the admin wording).
    Idempotent: the machine's self-transition makes a re-cancel a no-op
    200 (no second stamp, no duplicate audit row). cancelled_at rides the
    transition exactly like admin ``cancel_pending`` — the is-none guard
    keeps a set event time immutable ([R-8.16])."""
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(id=order_id)
        except Order.DoesNotExist:
            return Response(
                {"error": "Order not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        if order.status == "cancelled":
            # The machine's self-transition: a replay, not a change.
            return Response({
                "message": "Order is already cancelled",
                "order_id": order.id,
                "status": order.status,
            })

        if not transition_allowed(order.status, "cancelled"):
            return Response(
                {
                    "error": f"Order cannot be cancelled from status "
                             f"'{order.status}'. Cancelling a paid order "
                             f"needs a refund — reconcile manually.",
                },
                status=status.HTTP_409_CONFLICT
            )

        order.status = "cancelled"
        order.cancelled_at = order.cancelled_at or timezone.now()
        # [R-10.1] SPEC-10-01b: fulfilment dimension rides the cancel
        # transition (insertion-only; second same-transaction write).
        order.fulfilment_status = fulfilment_for_status("cancelled")
        order.save(update_fields=['status', 'cancelled_at'])
        order.save(update_fields=['fulfilment_status'])
        log_api_action(request, order, CHANGE, "Cancelled via API.")

    return Response({
        "message": "Order cancelled",
        "order_id": order.id,
        "status": order.status,
    })

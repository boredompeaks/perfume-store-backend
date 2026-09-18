from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from rest_framework.decorators import api_view, permission_classes, throttle_scope
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from .models import Order, OrderItem, Coupon
from .serializers import OrderSerializer

from cart.models import Cart
from common.models import AuditEvent
from products.models import StockMovement, products

import logging
import razorpay
from django.conf import settings

logger = logging.getLogger(__name__)
# ==================================
# Order List
# ==================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_list(request):

    orders = Order.objects.filter(
        user=request.user
    ).order_by('-created_at')

    serializer = OrderSerializer(
        orders,
        many=True
    )

    return Response(
        serializer.data
    )


# ==================================
# Create Order / Checkout
# ==================================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_order(request):

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

            discount_amount = (
                subtotal_amount * coupon.discount_value
            ) / Decimal('100')

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
            total_amount=total_amount
        )

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
    try:
        coupon = Coupon.objects.get(
            code__iexact=code
        )

    except Coupon.DoesNotExist:
        return _uniform_coupon_rejection()

    # Check active
    if not coupon.active:
        return _uniform_coupon_rejection()

    # Check dates
    now = timezone.now()

    if now < coupon.valid_from:
        return _uniform_coupon_rejection()

    if now > coupon.valid_until:
        return _uniform_coupon_rejection()

    # Check usage limit
    if (
        coupon.usage_limit is not None
        and coupon.used_count >= coupon.usage_limit
    ):
        return _uniform_coupon_rejection()

    # Calculate cart subtotal
    subtotal = Decimal('0.00')

    for item in cart.items.all():
        subtotal += (
            item.product.price * item.quantity
        )

    # Check minimum order amount
    if subtotal < coupon.minimum_order_amount:
        return _uniform_coupon_rejection()

    # Calculate discount
    if coupon.discount_type == 'percentage':

        discount = (
            subtotal * coupon.discount_value
        ) / Decimal('100')

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
            razorpay_order = client.order.create({
                'amount': amount,
                'currency': 'INR',
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
        "currency": "INR",
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

        order.status = 'confirmed'
        order.razorpay_payment_id = razorpay_payment_id
        order.save(update_fields=['status', 'razorpay_payment_id'])

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

    return Response({
        "message": "Payment verified successfully",
        "order_id": order.id,
        "status": order.status,
        "razorpay_payment_id": razorpay_payment_id
    })

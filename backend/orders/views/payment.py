"""Payment: minting the gateway intent and the verify that turns it into money.
Both views run under ``select_for_update``, and ``verify_payment`` is the
lifecycle's point of no return; the sale itself (stock decrement, ledger rows,
coupon usage) belongs to ``orders.inventory.commit_order_sale``, shared with
the gateway webhook."""

import logging
from decimal import Decimal

import razorpay
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_scope
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from cart.models import Cart
from common import notifications
from common.models import AuditEvent

# verify_payment releases the holds create_order minted; the sale itself is
# ``orders.inventory.commit_order_sale``, shared with the gateway webhook.
from products.models import StockReservation, products

from ..inventory import StockUnavailable, commit_order_sale
from ..models import Coupon, Order, OrderStatusEvent

# Failed-verify audit trigger and payment-dimension transition gate.
from ..state import (
    TRIGGER_PAYMENT_FAILED,
    TRIGGER_PAYMENT_VERIFY,
    payment_for_status,
    payment_transition_allowed,
)

logger = logging.getLogger(__name__)

# The unique constraint on razorpay_order_id is the concurrency authority; the
# bounded loop turns a lost race into reuse of the winner's committed id.
PAYMENT_INTENT_ATTEMPTS = 3


def _object_body(data):
    """The write body as a dict, or ``{}`` when it is not a JSON object. A
    non-object body has no ``.get``, so reading one would 500 both payment
    endpoints; inlined from ``cart.views`` to avoid an import cycle."""
    return data if isinstance(data, dict) else {}


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_scope("payment")
def create_payment(request):

    body = _object_body(request.data)

    order_id = body.get("order_id")

    if not order_id:
        return Response(
            {"error": "order_id is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    try:
        order = Order.objects.get(id=order_id, user=request.user)

    except Order.DoesNotExist:
        return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

    if order.status != "pending":
        return Response(
            {"error": "This order cannot be paid"}, status=status.HTTP_400_BAD_REQUEST
        )

    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )

    # Amount must be in paise
    amount = int(order.total_amount * Decimal("100"))

    if order.razorpay_order_id:
        razorpay_order_id = order.razorpay_order_id
    else:
        try:
            # Charge the currency the order was minted with, read off the row.
            razorpay_order = client.order.create(
                {
                    "amount": amount,
                    "currency": order.currency,
                    "receipt": f"order_{order.id}",
                }
            )
        except Exception:
            # Log with the order reference, then re-raise to preserve the 500
            # semantics exactly.
            logger.exception("Payment intent creation failed for order %s", order.id)
            raise
        razorpay_order_id = razorpay_order["id"]
        # Intent and its trail row commit together. The pre-check is an unlocked
        # read; the row is re-fetched under lock, unique constraint still final.
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
                # The savepoint rolled the violated write back, so the next
                # turn starts from committed state; the bound is a safety net.
                if attempt == PAYMENT_INTENT_ATTEMPTS - 1:
                    raise
                continue

    return Response(
        {
            "order_id": order.id,
            "razorpay_order_id": razorpay_order_id,
            "amount": amount,
            "amount_in_rupees": order.total_amount,
            "currency": order.currency,
            "key_id": settings.RAZORPAY_KEY_ID,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify_payment(request):

    body = _object_body(request.data)

    razorpay_order_id = body.get("razorpay_order_id")

    razorpay_payment_id = body.get("razorpay_payment_id")

    razorpay_signature = body.get("razorpay_signature")

    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature]):
        return Response(
            {"error": "Payment details are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

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
        # No order relationship is proven yet, so the claimed gateway
        # references ride in detail; the single insert is atomic on its own.
        AuditEvent.record(
            AuditEvent.EventType.PAYMENT_SIGNATURE_REJECTED,
            actor=request.user,
            detail={
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
            },
        )

        # The audit row is the structured record; the log line keeps it
        # greppable for an operator.
        logger.warning(
            "Payment signature rejected (gateway order %s, payment %s)",
            razorpay_order_id,
            razorpay_payment_id,
        )

        # Mark the payment dimension failed while the order stays pending, so a
        # later verify can retry; the response is identical either way.
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
                # Order status did not move, so the row records from == to;
                # the trigger names the failure and the actor stays NULL.
                OrderStatusEvent.objects.create(
                    order=failed_order,
                    from_status=failed_order.status,
                    to_status=failed_order.status,
                    actor=None,
                    trigger=TRIGGER_PAYMENT_FAILED,
                )

                # The failed attempt's holds are dead: release them in this same
                # transaction so they never become a sale.
                failed_order.stock_reservations.filter(
                    status=StockReservation.Status.ACTIVE
                ).update(status=StockReservation.Status.RELEASED)

        return Response(
            {"error": "Payment verification failed"}, status=status.HTTP_400_BAD_REQUEST
        )

    order_id = body.get("order_id")
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
            # No FK target (the caller does not own it), so the claimed id
            # rides in detail.
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

            # Benign double-submit retry, so INFO: a WARNING would spam every
            # impatient re-click.
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
        # Ascending id is a total lock order: an unordered ``id__in`` let the
        # plan pick a sequence, so two carts sharing products could deadlock.
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

                # The oversell race's loser: release this attempt's holds now,
                # not at the TTL sweep; the order stays pending and retryable.
                order.stock_reservations.filter(
                    status=StockReservation.Status.ACTIVE
                ).update(status=StockReservation.Status.RELEASED)

                return Response(
                    {
                        "error": "An item is no longer available in the requested quantity"
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        # Lock the coupon on its own row: ``coupon`` is nullable, so a joined
        # FOR UPDATE becomes an outer join PostgreSQL rejects (SQLite hides it).
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

                # Failed checkout precondition: release the holds; the order
                # stays pending and retryable.
                order.stock_reservations.filter(
                    status=StockReservation.Status.ACTIVE
                ).update(status=StockReservation.Status.RELEASED)

                return Response(
                    {"error": "The coupon is no longer valid"},
                    status=status.HTTP_409_CONFLICT,
                )

        # The ONE sale writer (holds -> CONVERTED, stock decrement, ledger),
        # shared with the webhook; its locked re-check prevents overselling.
        try:
            commit_order_sale(order)
        except StockUnavailable as unavailable:
            AuditEvent.record(
                AuditEvent.EventType.PAYMENT_STOCK_CONFLICT,
                actor=request.user,
                order=order,
                detail={
                    "product_id": unavailable.product_id,
                    "requested": unavailable.requested,
                    "available": unavailable.available,
                },
            )

            logger.info(
                "Payment verify failed: order %s stock conflict "
                "(product %s requested %s, available %s)",
                order.id,
                unavailable.product_id,
                unavailable.requested,
                unavailable.available,
            )

            order.stock_reservations.filter(
                status=StockReservation.Status.ACTIVE
            ).update(status=StockReservation.Status.RELEASED)

            return Response(
                {"error": "An item is no longer available in the requested quantity"},
                status=status.HTTP_409_CONFLICT,
            )

        # Coupon usage is not incremented here: ``commit_order_sale`` already
        # consumed it in this transaction under the same idempotent guard.

        # paid_at is this transition's business timestamp, written in the same
        # atomic block; the or-guard keeps it written exactly once.
        previous_status = order.status
        order.paid_at = order.paid_at or timezone.now()
        order.status = "confirmed"
        order.razorpay_payment_id = razorpay_payment_id
        order.save(update_fields=["status", "razorpay_payment_id", "paid_at"])
        # Payment dimension is captured by the same event, in a second
        # persistence of the locked row so both UPDATEs commit or roll back.
        order.payment_status = payment_for_status("confirmed")
        order.save(update_fields=["payment_status"])
        # Transition audit row rides the same block; no admin acts here, so the
        # actor stays NULL.
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

        # The gateway reconciliation and the order timeline each need their
        # event; both land inside the atomic block.
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
        # Notification intent is written as an outbox row here, so no SMTP
        # socket opens; a failed enqueue is swallowed since money is captured.
        try:
            notifications.enqueue(
                AuditEvent.EventType.ORDER_PAID,
                {"order": order},
            )
        except Exception:
            logger.exception(
                "order.paid notification enqueue failed; payment stays "
                "confirmed order=%s",
                order.id,
            )

    return Response(
        {
            "message": "Payment verified successfully",
            "order_id": order.id,
            "status": order.status,
            "razorpay_payment_id": razorpay_payment_id,
        }
    )

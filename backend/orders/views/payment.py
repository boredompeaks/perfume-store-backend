"""Payment: minting the gateway intent and the verify that turns it into money.

One of six modules split out of the former single-file `orders/views.py`. Both
views here run inside `transaction.atomic()` with `select_for_update()`, and
`verify_payment` is the order lifecycle's point of no return: inventory,
coupon usage and cart cleanup happen here and NOWHERE else, deliberately after
the gateway signature has been checked. Every failure branch below releases
the attempt's stock holds rather than leaving phantom pressure on stock.

Nothing in this module was rewritten - the bodies moved verbatim - so the
guarantees documented on each branch are the ones the split inherits.
"""

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

# [SPEC-12-02] StockReservation and StockMovement ride the existing
# products.models import line: verify_payment converts and releases the
# holds create_order minted.
from products.models import StockMovement, StockReservation, products

from ..models import Coupon, Order, OrderStatusEvent

# [R-10.4] SPEC-10-04: the failed-verify audit trigger + the
# payment-dimension transition gate and status mapping.
from ..state import (
    TRIGGER_PAYMENT_FAILED,
    TRIGGER_PAYMENT_VERIFY,
    payment_for_status,
    payment_transition_allowed,
)

logger = logging.getLogger(__name__)

# [R-11.1] SPEC-11-01: the payment-intent persistence carries the same shape
# of safety net. The unique constraint on razorpay_order_id is the
# concurrency authority; the bounded loop only converts a lost race into a
# reuse of the winner's committed id (one retry converges -- the collision
# window is a single write), and exhaustion fails loudly instead of looping.
PAYMENT_INTENT_ATTEMPTS = 3

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

"""The staff refund seam: reversing a captured payment, in full or in part.

One of six modules split out of the former single-file `orders/views.py`. This
is the only writer of the two refund payment values, and it moves the PAYMENT
dimension without ever moving the order status - a refund is not a cancel,
which is why an order whose money is returned stays refundable-eligible rather
than becoming a `cancelled` row.

Nothing in this module was rewritten - the bodies moved verbatim - so the
guarantees documented on the view are the ones the split inherits. The
idempotency-key header names are imported from `checkout.py`, the one module
that defines that contract, so the two surfaces cannot drift apart.
"""

import logging
from decimal import Decimal

from django.contrib.admin.models import ADDITION
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from common.audit import log_api_action
from common.money import quantize_money
from common.permissions import HasRefundsCreate

from ..models import Order, Refund

# [R-1.14] SPEC-1-05: the refund row and the gateway seam the refund writer
# drives. Raising RefundGatewayError out of the atomic block is what discards
# the PENDING row.
from ..refunds import RefundGatewayError, refund_payment

# [R-10.1] Eligibility is asked of the machine, not restated here.
from ..state import payment_transition_allowed

# The header-keyed idempotency contract is defined once, in checkout, and
# reused here rather than restated.
from .checkout import IDEMPOTENCY_KEY_HEADER, IDEMPOTENCY_KEY_MAX_LENGTH

logger = logging.getLogger(__name__)

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

"""The refund seam - the ONE place this store gives money back.

``refund_payment`` is the thin, mockable provider call; ``issue_refund`` is the
single issuance writer shared by the DRF seam and the admin action, so the two
paths cannot drift about what may be refunded or what a retry does."""

import logging
from dataclasses import dataclass
from decimal import Decimal

import razorpay
from django.conf import settings
from django.contrib.admin.models import ADDITION
from django.db import transaction
from django.utils import timezone

from common.audit import log_api_action
from common.money import quantize_money

from .models import Order, Refund
from .state import payment_transition_allowed

logger = logging.getLogger(__name__)


class RefundGatewayError(Exception):
    """The gateway did not refund the payment.

    Callers MUST let this escape their transaction so the attempt rolls back
    whole: a store that recorded a refund the gateway never made would be
    reconciling money it does not have.
    """


def _minor_units(amount):
    """``Decimal('10.00') -> 1000``: the provider counts in the currency's
    smallest unit, quantized once here so the provider never rounds again."""
    return int(quantize_money(amount) * 100)


def refund_payment(*, payment_id, amount):
    """Refund ``amount`` of a captured Razorpay payment; return the refund id.

    ``amount`` is Decimal money, not provider units - the scaling is this
    function's job. Raises :class:`RefundGatewayError` for anything but an
    acknowledged refund.
    """
    if not payment_id:
        raise RefundGatewayError("no captured payment reference to refund")

    # Keyword form, never positional: the SDK's __init__(session, auth,
    # **options) would bind the KEY ID to session and the SECRET to auth, and
    # every refund would die as an AttributeError.
    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
    )

    try:
        # ``timeout`` is the SDK's per-request kwarg, not a Client option: the
        # constructor would discard it, and this call runs inside the caller's
        # transaction holding the order lock, so an unbounded wait would park
        # every refund for that order behind a stalled gateway.
        response = client.refund.create(
            {"payment_id": payment_id, "amount": _minor_units(amount)},
            timeout=settings.RAZORPAY_TIMEOUT,
        )
    except Exception as exc:
        # razorpay's error classes share no common base (and the transport can
        # raise anything), so a raw provider exception must never escape as a
        # 500: the caller only rolls back on a typed error.
        raise RefundGatewayError(f"{type(exc).__name__}: {exc}") from exc

    refund_id = (response or {}).get("id")
    if not refund_id:
        # A success with no id is not a refund: nobody could look it up at the
        # provider.
        raise RefundGatewayError("gateway refund response carried no id")

    return refund_id


class RefundRefused(Exception):
    """A refund the store refuses before any money moves.

    ``message`` is operator-facing and shown verbatim; ``code`` is the
    machine-readable bucket the API maps to 404/400/409.
    """

    def __init__(self, message, code="conflict"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class RefundReceipt:
    """What one issuance attempt produced.

    ``replayed`` means the ``Idempotency-Key`` collapsed this call onto a refund
    that already existed: no new row, no second gateway call.
    """

    refund: Refund
    order: Order
    replayed: bool


def issue_refund(
    request,
    *,
    order_id,
    amount=None,
    reason="",
    idempotency_key=None,
    source="API",
):
    """Issue a refund against a captured payment - the one writer.

    Atomic and serialized: the Order row (and this order's refund rows) is
    locked for the whole attempt, the balance is recomputed under those locks,
    and the gateway call happens inside the same transaction, so two concurrent
    refunds cannot both pass the balance gate and a gateway failure rolls the
    attempt back whole. An ``idempotency_key`` collapses a retry onto the refund
    it already produced; without one the balance gate alone refuses to hand back
    the same money twice.
    """
    reason = (reason or "").strip() if isinstance(reason, str) else ""
    if not reason:
        # The reconciliation trail has to explain a refund later, so the
        # operator's words are required input, not an optional nicety.
        raise RefundRefused("A refund reason is required", code="invalid")

    try:
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(id=order_id)
            except Order.DoesNotExist:
                raise RefundRefused("Order not found", code="not_found")

            # Locked beside the order row and materialized so the lock takes
            # effect; the list serves the replay probe below, while the balance
            # comes from refundable_remaining's aggregate under the Order lock.
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
                    logger.info(
                        "Refund idempotency: refund %s replayed for order %s "
                        "(Idempotency-Key)",
                        replay.id,
                        order.pk,
                    )
                    return RefundReceipt(refund=replay, order=order, replayed=True)

            remaining = order.refundable_remaining
            if remaining <= Decimal("0.00"):
                raise RefundRefused("Order has no refundable balance left")

            # Eligibility is asked of the state machine, not restated here: the
            # current payment must be one with a declared edge to a refund
            # value, so pending/authorized/failed are refused before any write.
            if not any(
                payment_transition_allowed(order.payment_status, target)
                for target in ("refunded", "partially_refunded")
            ):
                raise RefundRefused(
                    f"Order payment is '{order.payment_status}'; "
                    f"only a captured payment can be refunded."
                )

            if not order.razorpay_payment_id:
                # A COD order has no provider payment to reverse: refuse
                # rather than write a refund row no money moved behind.
                raise RefundRefused(
                    "Order has no captured payment to refund at the gateway"
                )

            if amount is None:
                # No amount asked for: the rest of the order's money.
                amount = remaining
            else:
                try:
                    # str() before parsing so a JSON number never becomes
                    # binary-float money; quantization stays inside the guard so
                    # a bad value is refused, never raised.
                    amount = quantize_money(Decimal(str(amount)))
                except (ArithmeticError, TypeError, ValueError):
                    raise RefundRefused(
                        "amount must be a decimal amount", code="invalid"
                    )
                # NaN/Infinity parse as Decimals but are not money, and
                # comparing one raises - hence is_finite() first.
                if not amount.is_finite() or amount <= Decimal("0.00"):
                    raise RefundRefused(
                        "amount must be a positive decimal amount", code="invalid"
                    )
                if amount > remaining:
                    raise RefundRefused(
                        f"Refund of {amount} exceeds the "
                        f"refundable balance of {remaining}"
                    )

            refund = Refund.objects.create(
                order=order,
                amount=amount,
                reason=reason,
                # The order's payment dimension below is the authority on what
                # is left.
                kind=(Refund.Kind.FULL if amount == remaining else Refund.Kind.PARTIAL),
                status=Refund.Status.PENDING,
                actor=request.user,
                idempotency_key=idempotency_key,
            )

            # Raising out of the atomic block discards the PENDING row above, so
            # a refused refund leaves no trace.
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
            # Nothing left to refund -> refunded, else partially_refunded; both
            # are declared edges from every state this writer admits.
            order.payment_status = (
                "refunded"
                if refunded_total >= quantize_money(order.total_amount)
                else "partially_refunded"
            )
            # The is-none guard keeps a set event time immutable.
            order.refunded_at = order.refunded_at or timezone.now()
            order.save(update_fields=["payment_status", "refunded_at"])

            # The privileged-action record the admin surface would leave, so the
            # audit log answers "who refunded what"; it rides this transaction,
            # so a rolled-back refund leaves no record.
            log_api_action(
                request,
                refund,
                ADDITION,
                f"Refund {amount} {order.currency} ({refund.kind}) issued "
                f"via {source} for order #{order.id} "
                f"(gateway refund {gateway_refund_id}).",
            )

        return RefundReceipt(refund=refund, order=order, replayed=False)
    except RefundGatewayError as exc:
        logger.warning(
            "Refund gateway failure for order %s (amount %s): %s",
            order_id,
            amount,
            exc,
        )
        raise

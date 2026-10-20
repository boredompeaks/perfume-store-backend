"""The refund seam - the ONE place this store gives money back.

Two layers, one boundary:

* :func:`refund_payment` translates an internal refund decision into the
  provider's refund call and nothing else. It is thin on purpose and mockable
  at one point, so the issuance path's atomicity can be tested without a
  network.
* :func:`issue_refund` is the issuance core: validation, the row locks, the
  balance gate, the gateway call, the payment-dimension move, the timestamp
  and the privileged-action record. It exists so the DRF seam
  (``orders.views.admin_order_refund``) and the admin action
  (``orders.admin.OrderAdmin.issue_refund``) run the SAME writer rather than
  two copies that can drift about what may be refunded or what a retry does.

Credentials are read from settings, which resolve ``RAZORPAY_KEY_ID`` /
``RAZORPAY_KEY_SECRET`` from the environment - no credential is written here.
``RAZORPAY_TIMEOUT`` bounds the gateway socket for the same reason
``EMAIL_TIMEOUT`` bounds the mail socket: this call is made while the caller's
transaction still holds the order lock, so an unbounded wait is a
checkout-blocking outage rather than a slow request.
The amount is quantized with the shared ``common.money`` helper before it is
scaled to the provider's minor units, the same rounding rule the capture
amount uses in ``create_payment``, so paying and refunding cannot disagree
about one amount.
"""

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

    Raised for a missing payment reference, any provider error, and a 2xx
    response that carries no refund id. Callers MUST let this escape their
    transaction (or re-raise it) so the attempt rolls back whole: a store
    that recorded a refund the gateway never made would be reconciling money
    it does not have. The provider's own message rides the exception text for
    the caller's log lines - never for an API response.
    """


def _minor_units(amount):
    """``Decimal('10.00') -> 1000``: the provider counts in the currency's
    smallest unit. Quantized first, so a half-paise request is rounded once
    here and never again by the provider."""
    return int(quantize_money(amount) * 100)


def refund_payment(*, payment_id, amount):
    """Refund ``amount`` of a captured Razorpay payment; return the refund id.

    ``amount`` is the store's Decimal money, not provider units - the scaling
    is this function's job. Raises :class:`RefundGatewayError` for every
    outcome that is not a refund the provider acknowledged.
    """
    if not payment_id:
        raise RefundGatewayError("no captured payment reference to refund")

    # Keyword form, never positional. The SDK's signature is
    # ``__init__(self, session=None, auth=None, **options)``, so passing the
    # credential pair positionally binds the KEY ID to ``session`` and the
    # SECRET to ``auth``; the client then calls ``.post`` on a string and every
    # refund dies as an AttributeError. ``orders.views.payment`` has always
    # used the keyword form - this seam must match it.
    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
    )

    try:
        # The timeout is the SDK's per-request kwarg, which it forwards
        # untouched to ``requests``. It is NOT a Client option: ``__init__``
        # consumes only base_url and the retry knobs, so a ``timeout=`` passed
        # to the constructor would be silently discarded. Without it the
        # refund socket waits forever, and this call runs inside the caller's
        # ``transaction.atomic()`` holding the order's row lock - a stalled
        # gateway would park every refund for that order behind it. A tripped
        # timeout raises ``requests.Timeout``, which the handler below already
        # turns into a typed RefundGatewayError, so the attempt rolls back
        # whole instead of hanging.
        response = client.refund.create(
            {"payment_id": payment_id, "amount": _minor_units(amount)},
            timeout=settings.RAZORPAY_TIMEOUT,
        )
    except Exception as exc:
        # razorpay's four error classes (BadRequestError, GatewayError,
        # ServerError, SignatureVerificationError) each derive from Exception
        # directly - there is no common base to catch - and the transport can
        # raise anything at all. A refund must never let a raw provider
        # exception escape as a 500: the caller only rolls the attempt back
        # when it recognizes a typed error.
        raise RefundGatewayError(f"{type(exc).__name__}: {exc}") from exc

    refund_id = (response or {}).get("id")
    if not refund_id:
        # A success with no id is not a refund: recording one would put a row
        # in the reconciliation trail that nobody can look up at the provider.
        raise RefundGatewayError("gateway refund response carried no id")

    return refund_id


class RefundRefused(Exception):
    """A refund the store refuses before any money moves.

    ``message`` is the operator-facing text and is shown verbatim by both
    surfaces. ``code`` is the machine-readable bucket the API maps onto an
    HTTP status, so moving the decision here did not move the HTTP contract:
    ``not_found`` -> 404, ``invalid`` -> 400, ``conflict`` -> 409.
    """

    def __init__(self, message, code="conflict"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class RefundReceipt:
    """What one issuance attempt produced.

    ``replayed`` means the ``Idempotency-Key`` collapsed this call onto a
    refund that already existed: no new row, no second gateway call.
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

    ``request`` supplies both the actor (``refund.actor``) and the audit
    actor, so the row and its privileged-action record can never name
    different people. ``amount`` may be ``None`` (refund the remaining
    balance) or any value the API accepted before; it is parsed and
    quantized here so both callers share one money rule.

    Atomic and serialized: the Order row is locked for the whole attempt
    (this order's refund rows beside it, since they are the other half of the
    balance being spent), the balance is recomputed under those locks, and the
    gateway call happens inside the same transaction. Two concurrent refunds
    therefore cannot both pass the balance gate, and a gateway failure rolls
    the attempt back whole - no refund row, no payment-dimension move, no
    timestamp, no audit record.

    Idempotent-safe twice over: an ``idempotency_key`` collapses a retry onto
    the refund it already produced (no second gateway call), and without a key
    the balance gate alone still refuses to hand back the same money twice.

    Raises :class:`RefundRefused` for every domain refusal (no reason, no
    refundable balance, an uncaptured payment, a non-money or over-large
    amount, an unknown order), and :class:`RefundGatewayError` when the
    provider did not acknowledge the refund. Neither leaves a trace.
    """
    reason = (reason or "").strip() if isinstance(reason, str) else ""
    if not reason:
        # A refund is a financial action the reconciliation trail has to
        # explain later, so the operator's words are required input, not an
        # optional nicety (the admin cancel path asks for the same reason).
        raise RefundRefused("A refund reason is required", code="invalid")

    try:
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(id=order_id)
            except Order.DoesNotExist:
                raise RefundRefused("Order not found", code="not_found")

            # This order's refund rows, locked beside the order row, and
            # materialized because that is what makes the lock take effect.
            # What makes the ATTEMPT safe is the Order row lock above: every
            # refund writer for this order takes it first, so no other writer
            # can append a refund between this transaction's balance read and
            # its commit. This list serves the replay probe below; the balance
            # itself comes from refundable_remaining's aggregate, and the
            # Order lock is what keeps that read consistent.
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
                    return RefundReceipt(refund=replay, order=order, replayed=True)

            # The balance gate comes first because it is the reason a fully
            # refunded order cannot be refunded again; the payment-status gate
            # below then covers the orders that were never captured.
            remaining = order.refundable_remaining
            if remaining <= Decimal("0.00"):
                raise RefundRefused("Order has no refundable balance left")

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
                raise RefundRefused(
                    f"Order payment is '{order.payment_status}'; "
                    f"only a captured payment can be refunded."
                )

            if not order.razorpay_payment_id:
                # A cash-on-delivery order carries no provider payment to
                # reverse, and the seam has nothing to call: refuse rather
                # than write a refund row no money moved behind.
                raise RefundRefused(
                    "Order has no captured payment to refund at the gateway"
                )

            if amount is None:
                # No amount asked for: the rest of the order's money.
                amount = remaining
            else:
                try:
                    # The value is stringified before it is parsed, so a
                    # JSON number never becomes binary-float money, and the
                    # quantization happens inside the guard because a value
                    # too large for 2-dp money (or not a number at all) must
                    # be refused, never raise out of the arithmetic.
                    amount = quantize_money(Decimal(str(amount)))
                except (ArithmeticError, TypeError, ValueError):
                    raise RefundRefused(
                        "amount must be a decimal amount", code="invalid"
                    )
                # NaN/Infinity parse as Decimals but are not money, and
                # comparing one raises - so they are refused before the
                # comparisons below (the is_finite() short-circuit is what
                # makes that safe).
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

            # [6.12.6] Staff write: the privileged-action record the admin
            # surface would have left, naming the order and the money so
            # the audit-log route can answer "who refunded what" without
            # joining the refund row. It rides this same transaction, so a
            # rolled-back refund leaves no record of itself.
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
        # The provider's own text goes to the log; the caller gets the fact
        # and nothing about the provider's internals.
        logger.warning(
            "Refund gateway failure for order %s (amount %s): %s",
            order_id,
            amount,
            exc,
        )
        raise

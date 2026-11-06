"""The refund seam - the ONE place this store gives money back.

Two layers, one committed boundary:

* :func:`refund_payment` translates an internal refund decision into the
  provider's refund call and nothing else. It is thin on purpose and mockable
  at one point, so the issuance path's durability can be tested without a
  network.
* :func:`issue_refund` is the issuance core: validation, the reservation, the
  balance gate, the gateway call, the payment-dimension move, the timestamp
  and the privileged-action record. It exists so the DRF seam
  (``orders.views.admin_order_refund``) and the admin actions
  (``orders.admin.OrderAdmin.issue_refund`` and
  ``RefundRequestAdmin.approve_requests``) run the SAME writer rather than
  copies that can drift about what may be refunded or what a retry does.

The boundary is deliberately split across transactions:

1. :func:`reserve_refund` locks the order, computes the reservation-aware
   balance and commits a PENDING ``Refund`` row (with ``reserved_until``)
   BEFORE the provider is called. That commit is what makes the promise
   visible to every other writer.
2. The gateway call happens OUTSIDE any transaction and outside the order
   lock, so a slow provider cannot park the order's row lock.
3. Settlement (PROCESSED + payment dimension + audit) is one short atomic
   block; a gateway refusal flips the SAME reservation to FAILED in its own
   atomic block and never touches the payment dimension.

There is therefore no rollback of the attempt: a DEFINITIVE provider refusal
leaves an honest FAILED row (``failure_code`` says why) and releases its
reservation; an AMBIGUOUS outcome (timeout, transport error, 2xx without an
id) leaves the PENDING row exactly as reserved, because money may have moved
and only a human/reconciliation may release it. A crashed process leaves a
PENDING reservation that stops reserving money when ``reserved_until``
lapses; ``orders.management.commands.sweep_refund_reservations`` records that
lapse as a FAILED ``reservation_expired`` row instead of silently forgetting
it.

Credentials are read from settings, which resolve ``RAZORPAY_KEY_ID`` /
``RAZORPAY_KEY_SECRET`` from the environment - no credential is written here.
``RAZORPAY_TIMEOUT`` bounds the gateway socket for the same reason
``EMAIL_TIMEOUT`` bounds the mail socket: the call is outside the row lock,
but an unbounded wait still parks a worker (and any caller waiting on the
response) for as long as the provider stays silent.
The amount is quantized with the shared ``common.money`` helper before it is
scaled to the provider's minor units, the same rounding rule the capture
amount uses in ``create_payment``, so paying and refunding cannot disagree
about one amount.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
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

# A reservation lapses after this if the gateway call or the settlement never
# completes. A lapsed PENDING row stops reducing the order's refundable
# balance; the sweep command records the lapse as a FAILED row. It is never
# silently deleted or left dangling.
RESERVATION_TTL = timedelta(minutes=10)


class RefundGatewayError(Exception):
    """The gateway OUTCOME IS UNKNOWN: money may or may not have moved.

    Raised for a missing payment reference (defensive only), every
    ambiguous provider/transport failure (timeout, connection error, gateway
    or server error, signature error, anything unexpected), and a 2xx
    response that carries no refund id. The writer deliberately leaves the
    PENDING reservation untouched - the reservation is held until its
    ``reserved_until`` so a possibly-moved refund cannot be issued twice
    meanwhile - and records the unknown outcome in the audit trail before
    raising. Callers MUST surface the failure rather than roll anything back
    or retry the same attempt blindly. The provider's own message rides the
    exception text for the caller's log lines - never for an API response.
    """


class RefundGatewayRefused(RefundGatewayError):
    """The provider DEFINITIVELY refused: no money moved.

    Raised when the provider answers with its own business rejection
    (``razorpay.errors.BadRequestError``): the request was rejected before
    any money moved, so the writer can release the reservation and settle
    the row FAILED with ``failure_code=provider_refused``. This is a
    subclass of :class:`RefundGatewayError`, so every caller that surfaces a
    gateway failure keeps working; the writer catches it first to tell a
    definitive refusal from an ambiguous outcome.
    """


def _minor_units(amount):
    """``Decimal('10.00') -> 1000``: the provider counts in the currency's
    smallest unit. Quantized first, so a half-paise request is rounded once
    here and never again by the provider."""
    return int(quantize_money(amount) * 100)


def refund_payment(*, payment_id, amount, idempotency_key):
    """Refund ``amount`` of a captured Razorpay payment; return the refund id.

    ``amount`` is the store's Decimal money, not provider units - the scaling
    is this function's job. ``idempotency_key`` is required and rides the
    request as the provider's ``Idempotency-Key`` header, so a retry after a
    lost response cannot move the same money twice. A provider business
    rejection raises :class:`RefundGatewayRefused` (definitive, no money
    moved); every other failure and a 2xx response without an id raise
    :class:`RefundGatewayError` (ambiguous, money state unknown).
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
        # The timeout and headers are the SDK's per-request kwargs, which it
        # forwards untouched to ``requests``. The timeout is NOT a Client
        # option: ``__init__`` consumes only base_url and the retry knobs, so
        # a ``timeout=`` passed to the constructor would be silently
        # discarded. Without it the refund socket waits forever, parking a
        # worker for as long as the provider stays silent. A tripped timeout
        # raises ``requests.Timeout``, which the handler below classifies as
        # AMBIGUOUS: the reservation is left held as PENDING (money may have
        # moved) instead of being released by a blind rollback.
        #
        # The header rides THIS call only. The SDK's ``_update_request``
        # preserves an existing headers dict and only adds Content-type, so
        # the idempotency key reaches ``requests`` intact.
        response = client.refund.create(
            {"payment_id": payment_id, "amount": _minor_units(amount)},
            headers={"Idempotency-Key": idempotency_key},
            timeout=settings.RAZORPAY_TIMEOUT,
        )
    except razorpay.errors.BadRequestError as exc:
        # The provider's own business rejection: it explicitly said no and
        # no money moved, so the caller may release the reservation.
        raise RefundGatewayRefused(f"BadRequestError: {exc}") from exc
    except Exception as exc:
        # Everything else - requests.Timeout / ConnectionError, razorpay's
        # GatewayError / ServerError / SignatureVerificationError, and any
        # unexpected transport failure - is AMBIGUOUS: the request may have
        # reached the provider and money may have moved. The writer must not
        # touch the reservation for this branch.
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


@dataclass(frozen=True)
class _Reservation:
    """The committed reservation a gateway attempt acts on."""

    refund: Refund
    order: Order
    remaining: Decimal
    replayed: bool


def _parse_amount(value):
    """Parse one caller-supplied amount into quantized 2-dp money.

    A value that is not money, or not a positive finite amount, is a domain
    refusal (never an arithmetic crash out of the writer).
    """
    try:
        # The value is stringified before it is parsed, so a JSON number never
        # becomes binary-float money, and the quantization happens inside the
        # guard because a value too large for 2-dp money (or not a number at
        # all) must be refused, never raise out of the arithmetic.
        amount = quantize_money(Decimal(str(value)))
    except (ArithmeticError, TypeError, ValueError):
        raise RefundRefused("amount must be a decimal amount", code="invalid")
    # NaN/Infinity parse as Decimals but are not money, and comparing one
    # raises - so they are refused before any comparison (the is_finite()
    # short-circuit is what makes that safe).
    if not amount.is_finite() or amount <= Decimal("0.00"):
        raise RefundRefused("amount must be a positive decimal amount", code="invalid")
    return amount


def reserve_refund(
    request,
    *,
    order_id,
    amount=None,
    reason="",
    idempotency_key=None,
):
    """Reserve refundable balance for one gateway attempt.

    This is the ONE admission path for all three surfaces. It runs inside its
    own ``transaction.atomic()``: the Order row is locked, the balance is
    recomputed (PROCESSED money plus active PENDING reservations), and the
    PENDING reservation is committed before the lock is released. The gateway
    call happens after this returns, outside both the transaction and the
    lock.

    A keyed retry is resolved under the lock:

    * PROCESSED -> replay result, no new row and no gateway call;
    * PENDING -> "already in progress" refusal. A lapsed PENDING row is
      treated the same way on purpose: this writer never resurrects a
      reservation whose provider outcome is unknown, and a dying process
      settles it as FAILED; a lapsed row then stays forensic only;
    * FAILED -> the SAME row is retried in place (the unique index forbids a
      second row with that key). An explicit amount must equal the stored
      amount; otherwise the stored amount is reused. The reset is part of
      this transaction.

    Returns a :class:`_Reservation` carrying the row, the order, the
    pre-reservation remaining balance and the replay flag.
    """
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(id=order_id)
        except Order.DoesNotExist:
            raise RefundRefused("Order not found", code="not_found")

        # This order's refund rows, locked beside the order row, and
        # materialized because that is what makes the lock take effect.
        # What makes the ATTEMPT safe is the Order row lock above: every
        # refund writer for this order takes it first, so no other writer can
        # append or settle a refund between this transaction's balance read
        # and its commit. This list serves the replay probe below; the
        # balance itself comes from refundable_remaining's aggregate.
        order_refunds = list(Refund.objects.select_for_update().filter(order=order))

        reservation = None
        explicit_amount = None
        if idempotency_key is not None:
            reservation = next(
                (
                    row
                    for row in order_refunds
                    if row.idempotency_key == idempotency_key
                ),
                None,
            )
            if reservation is not None:
                if reservation.status == Refund.Status.PROCESSED:
                    # Benign keyed retry, so INFO rather than WARNING: the
                    # same judgement as checkout's replay-collapse log.
                    logger.info(
                        "Refund idempotency: refund %s replayed for order %s "
                        "(Idempotency-Key)",
                        reservation.id,
                        order.pk,
                    )
                    return _Reservation(
                        refund=reservation,
                        order=order,
                        remaining=order.refundable_remaining,
                        replayed=True,
                    )
                if reservation.status == Refund.Status.PENDING:
                    raise RefundRefused(
                        "A refund with this idempotency key is already in progress",
                        code="conflict",
                    )
                if reservation.status == Refund.Status.FAILED:
                    # Retry in place: the unique index owns this key, so a
                    # second row is not an option. An explicit amount that
                    # disagrees with the stored one is a different request
                    # wearing the same key, not a retry.
                    if amount is not None:
                        explicit_amount = _parse_amount(amount)
                        if explicit_amount != quantize_money(reservation.amount):
                            raise RefundRefused(
                                "Idempotency-Key was used with a different amount",
                                code="conflict",
                            )

        # The balance gate comes first because it is the reason a fully
        # refunded order cannot be refunded again; the payment-status gate
        # below then covers the orders that were never captured. A FAILED
        # retry's own row no longer reserves anything, so it cannot
        # double-count against its own retry.
        remaining = order.refundable_remaining
        if remaining <= Decimal("0.00"):
            raise RefundRefused("Order has no refundable balance left")

        # [R-10.1] Eligibility is asked of the machine, not restated here: a
        # refund moves the payment dimension onto one of the two refund
        # values, so the row's current payment must be one the machine
        # declares an edge FROM (payment_transition_allowed over
        # PAYMENT_ALLOWED_TRANSITIONS - captured or partially_refunded
        # today). pending / authorized / failed declare no refund edge, so
        # they are refused before anything is written.
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
            # reverse, and the seam has nothing to call: refuse rather than
            # write a refund row no money moved behind.
            raise RefundRefused(
                "Order has no captured payment to refund at the gateway"
            )

        if reservation is not None:
            # FAILED retry: use the stored amount (never re-derive it from
            # what happens to be left now). An explicit amount was parsed and
            # equality-checked above.
            amount = (
                quantize_money(reservation.amount)
                if amount is None
                else explicit_amount
            )
        elif amount is None:
            # No amount asked for: the rest of the order's money.
            amount = remaining
        else:
            amount = _parse_amount(amount)

        # A retry still has to fit the CURRENT balance: another refund may
        # have consumed money since the failed attempt was written.
        if amount > remaining:
            raise RefundRefused(
                f"Refund of {amount} exceeds the refundable balance of {remaining}"
            )

        now = timezone.now()
        reserved_until = now + RESERVATION_TTL
        if reservation is None:
            reservation = Refund.objects.create(
                order=order,
                amount=amount,
                reason=reason,
                # FULL means "this attempt cleared what was left"; the order's
                # payment dimension below is the authority on what is left.
                kind=(Refund.Kind.FULL if amount == remaining else Refund.Kind.PARTIAL),
                status=Refund.Status.PENDING,
                actor=request.user,
                idempotency_key=idempotency_key,
                reserved_until=reserved_until,
            )
        else:
            # Reset the SAME FAILED row into an active reservation. Its
            # amount is fixed by the idempotency contract; the fresh note
            # and actor describe this retry. The previous failure's code no
            # longer explains a PENDING row, so it is cleared here.
            reservation.status = Refund.Status.PENDING
            reservation.reserved_until = reserved_until
            reservation.failure_code = None
            reservation.reason = reason
            reservation.actor = request.user
            reservation.save(
                update_fields=[
                    "status",
                    "reserved_until",
                    "failure_code",
                    "reason",
                    "actor",
                    "updated_at",
                ]
            )

        return _Reservation(
            refund=reservation,
            order=order,
            remaining=remaining,
            replayed=False,
        )


def _run_on_settled(on_settled, receipt):
    """Run the caller's settle hook inside its own SAVEPOINT.

    The hook runs inside the settlement transaction but under a nested
    ``transaction.atomic()``, so it gets its own SAVEPOINT. A hook failure -
    including a SQL failure that PostgreSQL would otherwise use to abort the
    whole settlement transaction - rolls back only the hook; the PROCESSED
    row and the provider's money move survive. Callers that need their own
    bookkeeping atomic with the settlement get that chance here, and a
    failure is visible in the log rather than in the money trail.
    """
    if on_settled is None:
        return
    try:
        with transaction.atomic():
            on_settled(receipt)
    except Exception:
        logger.exception(
            "Refund settle hook failed for refund %s (order %s); "
            "settlement is kept, hook work is not.",
            receipt.refund.pk,
            receipt.order.pk,
        )


def issue_refund(
    request,
    *,
    order_id,
    amount=None,
    reason="",
    idempotency_key=None,
    source="API",
    on_settled=None,
):
    """Issue a refund against a captured payment - the one writer.

    ``request`` supplies both the actor (``refund.actor``) and the audit
    actor, so the row and its privileged-action record can never name
    different people. ``amount`` may be ``None`` (refund the remaining
    balance) or any value the API accepted before; it is parsed and
    quantized here so both callers share one money rule.

    Durability boundary: :func:`reserve_refund` commits the PENDING
    reservation (and its ``reserved_until``) in its own transaction, the
    gateway call runs outside every transaction, and the outcome is applied
    in a short atomic block of its own. Two concurrent refunds therefore
    cannot both pass the balance gate while one is in flight. A definitive
    gateway refusal releases the reservation and records FAILED; an
    ambiguous outcome neither rolls the attempt away nor releases anything -
    the row stays PENDING exactly as reserved until reconciliation or the
    sweep. In both failure cases the payment dimension, ``refunded_at`` and
    the successful-refund audit record are never reached.

    ``on_settled(receipt)`` is called inside the settlement transaction
    after the PROCESSED write and the order's payment-dimension move, under
    a SAVEPOINT of its own, so a caller (the refund-request admin flow) can
    flip its own record with the money while a hook failure - including a
    SQL failure PostgreSQL would otherwise use to abort the settlement
    transaction - rolls back only the hook.

    Idempotent-safe twice over: an ``idempotency_key`` collapses a PROCESSED
    retry onto the refund it already produced (no second gateway call),
    refuses a PENDING retry as "in progress", and retries a FAILED row in
    place with a stable derived gateway key. The key is REQUIRED, because
    every production surface names one operator action that must never move
    the same money twice.

    Raises :class:`RefundRefused` for every domain refusal (no reason, no
    refundable balance, an uncaptured payment, a non-money or over-large
    amount, an unknown order, a key conflict), :class:`RefundGatewayRefused`
    when the provider explicitly refused (row FAILED, reservation released),
    and :class:`RefundGatewayError` when the provider's outcome is unknown
    (row left PENDING with its reservation held so a possibly-moved refund
    is not issued twice).
    """
    # A programming error, not a domain refusal: a production caller without
    # an operator action id cannot promise "never twice", so it is refused
    # before any database work.
    idempotency_key = (
        idempotency_key.strip() if isinstance(idempotency_key, str) else ""
    )
    if not idempotency_key:
        raise ValueError(
            "issue_refund requires an operator action id (idempotency_key)"
        )

    reason = (reason or "").strip() if isinstance(reason, str) else ""
    if not reason:
        # A refund is a financial action the reconciliation trail has to
        # explain later, so the operator's words are required input, not an
        # optional nicety (the admin cancel path asks for the same reason).
        raise RefundRefused("A refund reason is required", code="invalid")

    reservation = reserve_refund(
        request,
        order_id=order_id,
        amount=amount,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    if reservation.replayed:
        return RefundReceipt(
            refund=reservation.refund,
            order=reservation.order,
            replayed=True,
        )

    order = reservation.order
    refund = reservation.refund
    amount = refund.amount

    try:
        # Outside every transaction and outside the order lock. The gateway
        # key is derived from the reservation row, so a retry of the SAME row
        # presents the same key even if the response to the first call was
        # lost, and the provider can collapse it rather than pay twice.
        gateway_refund_id = refund_payment(
            payment_id=order.razorpay_payment_id,
            amount=amount,
            idempotency_key=f"refund-{refund.pk}",
        )
    except RefundGatewayRefused as exc:
        # Definitive provider refusal: no money moved, so release the
        # reservation in its own committed transaction (never a rollback of
        # the attempt) and record why. The subclass is caught first so this
        # branch cannot be swallowed by the ambiguous handler below.
        with transaction.atomic():
            updated = Refund.objects.filter(
                pk=refund.pk,
                status=Refund.Status.PENDING,
            ).update(
                status=Refund.Status.FAILED,
                failure_code=Refund.FailureCode.PROVIDER_REFUSED,
                reserved_until=None,
                updated_at=timezone.now(),
            )
            if updated != 1:
                # Settlement/state divergence: this writer owns the only path
                # that should be moving a PENDING row here. Money did not
                # move, so the RESERVATION is still released above only if it
                # matched; make the divergence loud rather than silent.
                logger.critical(
                    "Refund %s was refused at the gateway but its reservation "
                    "was not PENDING when settling FAILED (matched %s rows).",
                    refund.pk,
                    updated,
                )
            try:
                # savepoint: audit failure must not roll back the money move
                with transaction.atomic():
                    log_api_action(
                        request,
                        refund,
                        ADDITION,
                        f"Refund {amount} {order.currency} for order #{order.pk} "
                        f"failed at the gateway; the provider refused; "
                        f"no money moved.",
                    )
            except Exception:
                # The FAILED row is the money invariant; losing the audit
                # insert must not roll that back into a live reservation.
                logger.exception(
                    "Failed to record the audit trail for refused refund %s.",
                    refund.pk,
                )
        # The provider's own text goes to the log; the caller gets the fact
        # and nothing about the provider's internals.
        logger.warning(
            "Refund gateway refusal for order %s (amount %s): %s",
            order_id,
            amount,
            exc,
        )
        raise
    except RefundGatewayError as exc:
        # AMBIGUOUS: the call may have reached the provider and money may have
        # moved. Do NOT touch the row: it stays PENDING with the exact
        # reserved_until it was committed with, so the reservation continues
        # to block a second refund of the same money until it lapses. The
        # sweep command records a lapse later; only then may the balance be
        # reused. The audit trail records that the outcome is unknown.
        held_until = refund.reserved_until
        logger.warning(
            "Refund gateway outcome UNKNOWN for refund %s (order %s, "
            "amount %s): %s. Reservation is held until %s and the row stays "
            "PENDING for reconciliation.",
            refund.pk,
            order_id,
            amount,
            exc,
            held_until,
        )
        try:
            # savepoint: audit failure must not disturb the held reservation
            with transaction.atomic():
                log_api_action(
                    request,
                    refund,
                    ADDITION,
                    f"Refund {amount} {order.currency} for order #{order.pk} "
                    f"has an unknown gateway outcome; reservation held until "
                    f"{held_until} and the row stays PENDING.",
                )
        except Exception:
            logger.exception(
                "Failed to record the audit trail for the unknown-outcome refund %s.",
                refund.pk,
            )
        raise

    # One short settlement transaction: the money already moved at the
    # provider, so the row becomes PROCESSED and the order's payment
    # dimension/timestamp move with it. The hook (when supplied) and the
    # audit record run inside it, each under its own SAVEPOINT.
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=reservation.order.pk)
        now = timezone.now()
        updated = Refund.objects.filter(
            pk=refund.pk,
            status=Refund.Status.PENDING,
        ).update(
            status=Refund.Status.PROCESSED,
            gateway_refund_id=gateway_refund_id,
            reserved_until=None,
            updated_at=now,
        )
        if updated != 1:
            # The gateway moved the money; never drop the settlement on the
            # floor. Overwrite whatever the row says now and make the
            # divergence loud for an operator.
            logger.critical(
                "Refund %s settled at the gateway but its reservation was not "
                "PENDING (matched %s rows); forcing PROCESSED with gateway id %s.",
                refund.pk,
                updated,
                gateway_refund_id,
            )
            Refund.objects.filter(pk=refund.pk).update(
                status=Refund.Status.PROCESSED,
                gateway_refund_id=gateway_refund_id,
                reserved_until=None,
                updated_at=now,
            )

        refund = Refund.objects.get(pk=refund.pk)
        refunded_total = Refund.refunded_total(order)
        # [R-10.1] The balance gate is what chooses between the two refund
        # values the eligibility gate above proved reachable: nothing left to
        # refund -> refunded, some money still refundable ->
        # partially_refunded. Both are declared edges in
        # PAYMENT_ALLOWED_TRANSITIONS from every state this writer admits,
        # and the gate above is what guarantees the order can never overshoot
        # the captured amount.
        order.payment_status = (
            "refunded"
            if refunded_total >= quantize_money(order.total_amount)
            else "partially_refunded"
        )
        # [R-8.16] The business-event stamp the refund section was named for;
        # the is-none guard keeps a set event time immutable.
        order.refunded_at = order.refunded_at or now
        order.save(update_fields=["payment_status", "refunded_at"])

        receipt = RefundReceipt(refund=refund, order=order, replayed=False)

        # Contract: the hook may fail, but it must never roll the settlement
        # back after the provider has moved money; _run_on_settled wraps it
        # in a SAVEPOINT and logs a failure.
        _run_on_settled(on_settled, receipt)

        # [6.12.6] Staff write: the privileged-action record the admin surface
        # would have left, naming the order and the money so the audit-log
        # route can answer "who refunded what" without joining the refund
        # row. It rides this same transaction. A failed audit INSERT is logged
        # loudly but must not roll back a settlement the provider already
        # made - the money invariant outranks the trail row.
        try:
            # savepoint: audit failure must not roll back the money move
            with transaction.atomic():
                log_api_action(
                    request,
                    refund,
                    ADDITION,
                    f"Refund {amount} {order.currency} ({refund.kind}) issued "
                    f"via {source} for order #{order.id} "
                    f"(gateway refund {gateway_refund_id}).",
                )
        except Exception:
            logger.exception(
                "Refund %s settled (gateway %s) but its audit record could "
                "not be written.",
                refund.pk,
                gateway_refund_id,
            )

    return RefundReceipt(refund=refund, order=order, replayed=False)

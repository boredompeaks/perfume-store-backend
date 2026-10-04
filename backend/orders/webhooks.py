"""[R-1.15] SPEC-1-06: the payment webhooks family — the server-to-server
endpoint Razorpay calls, so payment truth stops depending on the customer's
browser coming back.

Until this endpoint existed the ONLY statement this store had about a payment
was the client callback (``verify_payment``): a customer who closed the tab
after paying left a paid order pending forever, and a client-side verify is
only as trustworthy as the machine that sent it. Spec section 1 lists webhooks
in the payments row of the overview, spec 11.2 makes "verify webhook
signatures" and "handle duplicate webhook deliveries" payment requirements in
their own right, and the threat table answers webhook spoofing with "signature
verification and replay/idempotency controls". So the three properties below are
the endpoint, in order of how badly they are needed:

1. **Signature verification is mandatory.** An unverified webhook that can move
   money is worse than no webhook at all: it hands anyone who learns an order id
   a "mark this paid" button. The HMAC-SHA256 of the RAW request body is
   compared against an env-driven secret with ``hmac.compare_digest``, BEFORE
   the body is parsed - a signature over a re-serialized payload is not a
   signature over what was sent. An unconfigured secret fails CLOSED (503,
   nothing recorded, nothing moved), so a deployment that forgot the key cannot
   accidentally accept unsigned money.
2. **Every delivery is recorded, once.** ``PaymentEvent.event_id`` is unique, so
   a replayed delivery collides in the database instead of applying twice; the
   IntegrityError-retry loop is the same shape ``create_payment`` uses for the
   payment-intent uniqueness (conventions.md:17). The insert runs BEFORE the
   handler, inside one transaction, so "recorded" and "acted on" are the same
   commit or neither is.
3. **Only the machine writes payment state.** A capture drives the order through
   ``orders.state`` — the same transition table, the same dimension mapping and
   the same audit-trigger vocabulary the customer-callback writer uses — inside
   ``transaction.atomic()`` with the Order row locked. Nothing here writes a
   status literal or invents a payment amount.

**Inventory is deliberately NOT duplicated here.** The stock decrement and its
``StockMovement`` ledger rows are SPEC-12-02's confirm step, under its own
product locks with its own oversell re-check; a second copy here would risk the
double decrement SPEC-1-B01 just fixed for refunds. What this endpoint does do
is convert the order's live stock holds to CONVERTED (one idempotent
``update``, the same statement ``verify_payment`` writes), so a paid order can
never have its holds swept away as expired by the TTL reconciler. Finishing a
webhook-confirmed order's inventory commit is that reconciler's job.

**Refund events are recorded, not replayed.** A ``payment.refunded`` /
``refund.processed`` delivery means money went back at the gateway — possibly
from the refund this store itself just issued through the SPEC-1-05 seam,
possibly from the provider dashboard, possibly by an operator at Razorpay.
Turning that into a ``Refund`` row here would double-spend the refundable
balance against the row the refund writer already recorded, and it would have to
invent the operator ``reason`` and ``actor`` a financial trail requires
(``Refund.reason`` is NOT NULL by design). So the delivery is recorded and
logged, and the finance operator who reconciles payments and refunds ([1.31])
matches it against the Refund rows — the seam stays the ONE place this store
asks the gateway to move money back.

Every refusal and every unhandled event type is RECORDED as a row: "the provider
says this happened and we did nothing" is precisely what a reconciliation needs
to read.
"""

import hashlib
import hmac
import json
import logging
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_scope,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from common import notifications
from common.models import AuditEvent
from common.money import quantize_money
from products.models import StockReservation

from .models import Order, OrderStatusEvent, PaymentEvent
from .state import (
    PAYMENT_CAPTURED,
    TRIGGER_PAYMENT_WEBHOOK,
    WEBHOOK_EVENT_CAPTURED,
    WEBHOOK_REFUND_EVENTS,
    fulfilment_for_status,
    payment_transition_allowed,
    status_for_payment,
    transition_allowed,
)

logger = logging.getLogger(__name__)

# Provider protocol constants, not deployment config: these are the header
# names Razorpay sends on every webhook delivery (its dashboard writes them
# into the delivery URL's secret), and the endpoint id they ride on.
WEBHOOK_EVENT_ID_HEADER = "X-Razorpay-Event-Id"
WEBHOOK_SIGNATURE_HEADER = "X-Razorpay-Signature"

# [R-9.3.14]-shaped retry bound for the replay insert, matching
# create_payment's PAYMENT_INTENT_ATTEMPTS: one turn is the expected path
# (no collision), and the bound is a safety net against a pathological race,
# never a substitute for the constraint.
PAYMENT_EVENT_INSERT_ATTEMPTS = 3

# The provider counts money in the currency's smallest unit; this store's money
# is the 2-dp Decimal every other amount in the repo is (common.money). Decimal
# arithmetic, never float: a provider count must not become binary-float money.
MINOR_UNITS_PER_MAJOR = Decimal(100)

# The provider's signature is a hex-encoded SHA-256 digest, so its length is
# fixed by the digest (not by a chosen constant): anything else is not a
# signature and is refused before the comparison, which keeps the check total.
WEBHOOK_SIGNATURE_HEX_LENGTH = hashlib.sha256().digest_size * 2

# A payment dimension that already means "the money is in". A second capture
# event for one of these is a duplicate delivery of a fact we already hold, and
# re-applying it would append a second transition to the audit trail.
CAPTURED_PAYMENT_STATES = ("captured", "partially_refunded", "refunded")


def _store_money(amount_minor_units):
    """Provider minor units -> the store's Decimal money, or ``None``.

    Deliberately strict: only a non-negative integer count of minor units is
    money. A float, a string, a bool or a missing value returns ``None`` — the
    delivery is still recorded, just without an amount — because guessing a
    Decimal out of a malformed provider value is how float money starts. This
    is the same conversion ``orders.refunds`` scales with, so paying and
    reconciling cannot disagree about one amount.
    """
    if isinstance(amount_minor_units, bool) or not isinstance(amount_minor_units, int):
        return None
    if amount_minor_units < 0:
        return None
    return quantize_money(Decimal(amount_minor_units) / MINOR_UNITS_PER_MAJOR)


def _entity(body, name):
    """The event's named entity object, or ``{}`` when absent/not an object."""
    entity = body.get("payload", {})
    entity = entity.get(name) if isinstance(entity, dict) else None
    return entity if isinstance(entity, dict) else {}


def _signature_matches(raw_body, provided):
    """True when ``provided`` is the HMAC-SHA256 hex digest of the raw body.

    ``hmac.compare_digest`` is constant-time, so a caller cannot learn the
    expected digest one character at a time from response timing. The secret is
    a guaranteed non-empty deployment setting here: the view refuses an
    unconfigured deployment before reaching this function.

    The candidate is normalized and length-checked BEFORE the comparison
    because the comparison is total only over matching types: a non-ASCII
    signature header makes ``compare_digest`` raise ``TypeError``, which on an
    unauthenticated endpoint is an attacker-reachable 500 (and a 500 tells the
    provider to retry). ``provided`` is compared VERBATIM - the provider signs
    and compares a hex digest, so a value with surrounding whitespace is not
    what it sent and must not be quietly accepted.
    """
    try:
        candidate = provided.encode("ascii")
    except UnicodeEncodeError:
        return False
    if len(candidate) != WEBHOOK_SIGNATURE_HEX_LENGTH:
        return False
    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), candidate)


def _fits(value, field_name):
    """True when ``value`` fits the ``PaymentEvent`` column it is written to.

    SQLite ignores a VARCHAR width and Postgres enforces it, so an unbounded
    provider string is a silent success in development and a ``DataError`` 500
    in production - on an unauthenticated money endpoint, which is exactly
    where it surfaces. The bound is read off the column itself, so the check
    cannot drift from the schema it protects.
    """
    return len(value) <= PaymentEvent._meta.get_field(field_name).max_length


def _stored_reference(value):
    """The provider payment reference as this store can store it.

    A reference that does not fit the column is stored as the empty reference
    (which the model already means by "the event named no payment") rather than
    truncated: a truncated id is a plausible-looking reference that reconciles
    against nothing.
    """
    return value if _fits(value, "gateway_payment_id") else ""


def _apply_captured(event, body):
    """Reconcile a captured payment onto its order; return the outcome.

    Every refusal still records the event (the caller commits it): an event
    naming an order this store does not know, a mismatched payment reference, a
    wrong amount or an edge the machine does not declare are all facts
    reconciliation needs, and none of them may move money.
    """
    payment = _entity(body, "payment")
    gateway_payment_id = str(payment.get("id") or "")
    gateway_order_id = str(payment.get("order_id") or "")
    amount = _store_money(payment.get("amount"))
    target_status = status_for_payment(PAYMENT_CAPTURED)
    # An oversized reference is refused like any other unbindable one, and the
    # row keeps no reference rather than a truncated one (see _stored_reference).
    oversized_reference = not _fits(gateway_payment_id, "gateway_payment_id")

    event.gateway_payment_id = _stored_reference(gateway_payment_id)
    event.amount = amount

    if (
        not gateway_order_id
        or not gateway_payment_id
        or amount is None
        or oversized_reference
    ):
        logger.warning(
            "Payment webhook %s: malformed capture payload (order %r, "
            "payment %r); nothing applied",
            event.event_id,
            gateway_order_id,
            gateway_payment_id[:16],
        )
        AuditEvent.record(
            AuditEvent.EventType.PAYMENT_REFERENCE_MISMATCH,
            detail={
                "event_id": event.event_id,
                "gateway_order_id": gateway_order_id,
                "gateway_payment_id": gateway_payment_id,
                "reason": "malformed capture payload",
            },
        )
        return PaymentEvent.Outcome.REFUSED

    with transaction.atomic():
        # The Order row lock is the concurrency authority: this delivery and a
        # simultaneous customer-callback verify (or a second capture event) both
        # serialize on it, so the "is this payment already captured" question
        # below is answered against committed state, not a stale read.
        order = (
            Order.objects.select_for_update()
            .filter(razorpay_order_id=gateway_order_id)
            .first()
        )
        event.order = order

        if order is None:
            logger.warning(
                "Payment webhook %s: no order is bound to gateway order %s; "
                "nothing applied",
                event.event_id,
                gateway_order_id,
            )
            AuditEvent.record(
                AuditEvent.EventType.PAYMENT_ORDER_NOT_FOUND,
                detail={
                    "event_id": event.event_id,
                    "gateway_order_id": gateway_order_id,
                    "gateway_payment_id": gateway_payment_id,
                },
            )
            return PaymentEvent.Outcome.REFUSED

        if (
            order.razorpay_payment_id
            and order.razorpay_payment_id != gateway_payment_id
        ):
            # The order is already bound to a DIFFERENT gateway payment. This
            # is either a late event for a replaced payment or a provider
            # mix-up; either way the payload does not get to say what this
            # order's money was.
            logger.warning(
                "Payment webhook %s: order %s is bound to gateway payment %s, "
                "not %s; nothing applied",
                event.event_id,
                order.id,
                order.razorpay_payment_id,
                gateway_payment_id,
            )
            AuditEvent.record(
                AuditEvent.EventType.PAYMENT_REFERENCE_MISMATCH,
                order=order,
                detail={
                    "event_id": event.event_id,
                    "claimed_razorpay_payment_id": gateway_payment_id,
                    "razorpay_payment_id": order.razorpay_payment_id,
                },
            )
            return PaymentEvent.Outcome.REFUSED

        if amount != quantize_money(order.total_amount):
            # Captured money that is not this order's money is the one number
            # this endpoint must never reconcile: the amount is the tie between
            # the gateway's record and the row.
            logger.warning(
                "Payment webhook %s: captured %s does not match order %s "
                "total %s; nothing applied",
                event.event_id,
                amount,
                order.id,
                quantize_money(order.total_amount),
            )
            AuditEvent.record(
                AuditEvent.EventType.PAYMENT_REFERENCE_MISMATCH,
                order=order,
                detail={
                    "event_id": event.event_id,
                    "captured_amount": str(amount),
                    "order_total": str(quantize_money(order.total_amount)),
                },
            )
            return PaymentEvent.Outcome.REFUSED

        if order.payment_status in CAPTURED_PAYMENT_STATES:
            # Idempotent no-op: the money is already recorded against this
            # order (the customer callback, or an earlier delivery of this
            # same capture under a different event id). The unique event_id
            # handles the true replay; this handles the duplicate FACT.
            logger.info(
                "Payment webhook %s: order %s is already %s; nothing applied",
                event.event_id,
                order.id,
                order.payment_status,
            )
            AuditEvent.record(
                AuditEvent.EventType.PAYMENT_ALREADY_PROCESSED,
                order=order,
                detail={
                    "event_id": event.event_id,
                    "razorpay_payment_id": gateway_payment_id,
                    "payment_status": order.payment_status,
                },
            )
            return PaymentEvent.Outcome.REFUSED

        if not transition_allowed(order.status, target_status) or (
            not payment_transition_allowed(order.payment_status, PAYMENT_CAPTURED)
        ):
            # Both machine gates, asked rather than restated: the lifecycle
            # edge (pending -> confirmed) and the payment-dimension edge
            # (pending/authorized/failed -> captured). A cancelled or already
            # advanced order is out of scope for a capture.
            logger.warning(
                "Payment webhook %s: order %s cannot be captured from "
                "status %s / payment %s; nothing applied",
                event.event_id,
                order.id,
                order.status,
                order.payment_status,
            )
            return PaymentEvent.Outcome.REFUSED

        previous_status = order.status
        order.status = target_status
        order.fulfilment_status = fulfilment_for_status(target_status)
        order.payment_status = PAYMENT_CAPTURED
        # Binding the provider reference is what makes the next delivery (and
        # the customer-callback verify) reconcile against the same payment.
        order.razorpay_payment_id = gateway_payment_id
        order.paid_at = order.paid_at or timezone.now()
        order.save(
            update_fields=[
                "status",
                "fulfilment_status",
                "payment_status",
                "razorpay_payment_id",
                "paid_at",
            ]
        )

        # The order's holds become a real sale's holds: filtered on ACTIVE this
        # is idempotent, and a paid order can no longer have its holds swept
        # away as expired by the TTL reconciler.
        order.stock_reservations.filter(status=StockReservation.Status.ACTIVE).update(
            status=StockReservation.Status.CONVERTED
        )

        OrderStatusEvent.objects.create(
            order=order,
            from_status=previous_status,
            to_status=order.status,
            actor=None,
            trigger=TRIGGER_PAYMENT_WEBHOOK,
        )
        AuditEvent.record(
            AuditEvent.EventType.PAYMENT_VERIFIED,
            order=order,
            detail={
                "event_id": event.event_id,
                "razorpay_order_id": gateway_order_id,
                "razorpay_payment_id": gateway_payment_id,
            },
        )
        AuditEvent.record(
            AuditEvent.EventType.ORDER_PAID,
            order=order,
            detail={
                "event_id": event.event_id,
                "total_amount": str(order.total_amount),
            },
        )
        # Same customer notification the customer-callback writer sends: a
        # payment that arrived without a browser callback must still reach the
        # customer. dispatch never raises.
        notifications.dispatch(
            AuditEvent.EventType.ORDER_PAID,
            {"order": order},
        )

    return PaymentEvent.Outcome.APPLIED


def _apply_refunded(event, body):
    """Record a settled refund; return the outcome. Moves nothing.

    See the module docstring: the SPEC-1-05 seam owns refunds, and a delivery
    arriving here is either its echo, an out-of-band refund, or an operator's
    action at the provider. Reconciling it against the Refund rows is the
    finance operator's read ([1.31]), so the row is recorded (with the payment
    it names, and the order when we can prove which one) and the log names the
    order for a human.
    """
    refund = _entity(body, "refund")
    gateway_payment_id = str(refund.get("payment_id") or "")

    event.gateway_payment_id = _stored_reference(gateway_payment_id)
    event.amount = _store_money(refund.get("amount"))

    if not event.gateway_payment_id:
        # Nothing to bind the refund to: either the event named no payment or
        # the reference does not fit the column (an empty stored reference is
        # this model's "the event named no payment"). Recorded, moved nothing.
        return PaymentEvent.Outcome.RECORDED

    with transaction.atomic():
        order = (
            Order.objects.select_for_update()
            .filter(razorpay_payment_id=gateway_payment_id)
            .first()
        )
        event.order = order
        if order is not None:
            logger.warning(
                "Refund webhook %s: gateway reports %s returned against "
                "payment %s (order %s). Recorded only — no refund row is "
                "written from a webhook; reconcile against the refund seam.",
                event.event_id,
                event.amount,
                gateway_payment_id,
                order.id,
            )

    return PaymentEvent.Outcome.RECORDED


def _apply_event(event, body):
    """Dispatch a delivery to its handler; return the recorded outcome."""
    if event.event_type == WEBHOOK_EVENT_CAPTURED:
        return _apply_captured(event, body)
    if event.event_type in WEBHOOK_REFUND_EVENTS:
        return _apply_refunded(event, body)

    # Genuine, recorded, unhandled: the provider may add event types at any
    # time, and discarding a delivery it considers real would make this table
    # an incomplete record of what actually happened to a payment. INFO: the
    # refusals above are the ones an operator must read.
    logger.info(
        "Payment webhook %s: event type %r has no handler; recorded only",
        event.event_id,
        event.event_type,
    )
    return PaymentEvent.Outcome.RECORDED


@api_view(["POST"])
@permission_classes([AllowAny])
# Server-to-server: the gateway sends no session cookie and no bearer token, so
# this endpoint opts out of the credential chain entirely. That is what makes
# it exempt from the SPEC-17-03 CSRF gate (which only fires for requests that
# ride the session cookie) and it is deliberately NOT exempt from signature
# verification, which is the credential this endpoint accepts instead.
@authentication_classes([])
@throttle_scope("webhook")
def razorpay_webhook(request):
    """POST /api/v1/webhooks/razorpay/ — a signed payment event delivery.

    Answers 200 for every delivery it recorded (applied, refused or recorded
    only) and for a replay, because the provider must stop retrying a delivery
    this store has already heard; 400 for a request it will never accept
    (missing/invalid signature, unparseable body, no event id to deduplicate
    on); 503 while no webhook secret is configured.
    """
    # The RAW body, read before anything parses it: DRF marks request.data
    # access as having consumed the stream, so this is the only point at which
    # the exact bytes the provider signed are available. The signature covers
    # those bytes, so verification must come first and JSON parsing second.
    raw_body = request.body

    secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        # Fail closed: with no secret configured no signature could be valid,
        # so accepting the delivery would be an unverified webhook that can
        # move money. A deployment error, not a client error — 503, so the
        # provider's dashboard shows a failing endpoint rather than a quiet
        # one that looks healthy and loses money.
        logger.error(
            "Payment webhook received but RAZORPAY_WEBHOOK_SECRET is not "
            "configured; refusing event %s",
            request.headers.get(WEBHOOK_EVENT_ID_HEADER),
        )
        return Response(
            {"error": "Webhook signature verification is not configured"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    provided_signature = request.headers.get(WEBHOOK_SIGNATURE_HEADER)
    if not provided_signature or not _signature_matches(raw_body, provided_signature):
        # One uniform answer for "missing" and "wrong": an unsigned or
        # badly-signed delivery is refused identically, and nothing is
        # recorded for it (an unauthenticated caller must not be able to write
        # rows into the financial trail).
        logger.warning(
            "Payment webhook rejected: %s header missing or invalid (event %s)",
            WEBHOOK_SIGNATURE_HEADER,
            request.headers.get(WEBHOOK_EVENT_ID_HEADER),
        )
        return Response(
            {"error": "Invalid webhook signature"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    event_id = (request.headers.get(WEBHOOK_EVENT_ID_HEADER) or "").strip()
    if not event_id:
        # Without the provider's delivery id there is nothing to deduplicate
        # on, and the unique constraint is the whole replay defence.
        logger.warning(
            "Payment webhook rejected: %s header missing", WEBHOOK_EVENT_ID_HEADER
        )
        return Response(
            {"error": "Invalid webhook event"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # The signature was valid over bytes that are not a JSON document:
        # nothing here can be reconciled, and 400 tells the provider not to
        # retry a delivery this store can never read.
        logger.warning("Payment webhook %s: body is not valid JSON", event_id)
        return Response(
            {"error": "Invalid webhook payload"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not isinstance(body, dict):
        # A JSON document that is not an object carries no event type, no
        # entity and nothing to record beyond the string itself.
        logger.warning(
            "Payment webhook %s: body is JSON but not an event object", event_id
        )
        return Response(
            {"error": "Invalid webhook payload"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    event_type = str(body.get("event") or "")

    if not _fits(event_id, "event_id") or not _fits(event_type, "event_type"):
        # A delivery whose identifying columns cannot be stored is refused
        # rather than truncated: `event_id` is the replay key (a truncated one
        # would dedupe two different deliveries onto one row) and a truncated
        # event_type names an event this store never received. Bounding here -
        # against the column widths, not a hardcoded number - is what keeps the
        # answer identical on SQLite and Postgres (see _fits).
        logger.warning(
            "Payment webhook rejected: event id or event type exceeds the "
            "stored width (event id %s chars, event type %s chars)",
            len(event_id),
            len(event_type),
        )
        return Response(
            {"error": "Invalid webhook event"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    for attempt in range(PAYMENT_EVENT_INSERT_ATTEMPTS):
        try:
            # The insert IS the replay defence, and it runs before the handler
            # inside the same transaction as the effect: a second delivery of
            # this event id violates the unique constraint, the whole block
            # rolls back, and the loop's except reports a replay instead of
            # applying anything twice. Same IntegrityError-retry shape as
            # create_payment's payment-intent write (conventions.md:17).
            with transaction.atomic():
                event = PaymentEvent.objects.create(
                    event_id=event_id,
                    event_type=event_type,
                    payload=body,
                )
                event.outcome = _apply_event(event, body)
                event.save(
                    update_fields=["outcome", "order", "amount", "gateway_payment_id"]
                )
                return Response(
                    {
                        "status": "ok",
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "outcome": event.outcome,
                    },
                    status=status.HTTP_200_OK,
                )
        except IntegrityError:
            if PaymentEvent.objects.filter(event_id=event_id).exists():
                # A replay of an event already recorded: idempotent by
                # construction, so acknowledge it and let the provider stop
                # retrying. No second order write, no second audit row.
                logger.info(
                    "Payment webhook %s: replay of an already-recorded event",
                    event_id,
                )
                return Response(
                    {"status": "duplicate", "event_id": event_id},
                    status=status.HTTP_200_OK,
                )
            if attempt == PAYMENT_EVENT_INSERT_ATTEMPTS - 1:
                # A collision on some OTHER constraint (a malformed event id,
                # a locking conflict): not a replay, and not something this
                # handler can classify. Let it surface as a 500 so the
                # provider retries. This is also the loop's last turn, so the
                # loop cannot fall through to an implicit None.
                logger.exception(
                    "Payment webhook %s: could not be recorded",
                    event_id,
                )
                raise
    # Every turn of the loop above either returns a response or re-raises, so
    # control never reaches this point.

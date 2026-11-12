"""The payment webhooks family: the server-to-server endpoint Razorpay calls,
so payment truth stops depending on the customer's browser returning.

Signature verification is mandatory and fails closed; every delivery is recorded
exactly once (unique ``event_id``) before the handler runs in the same commit;
only the state machine writes payment state, with inventory committed through
the shared ``commit_order_sale``; and refund events are recorded, not replayed."""

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

from .inventory import StockUnavailable, commit_order_sale
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

# Provider protocol constants, not deployment config: the header names Razorpay
# sends on every webhook delivery.
WEBHOOK_EVENT_ID_HEADER = "X-Razorpay-Event-Id"
WEBHOOK_SIGNATURE_HEADER = "X-Razorpay-Signature"

# Retry bound for the replay insert, matching create_payment's
# PAYMENT_INTENT_ATTEMPTS: a safety net around the constraint, never a
# substitute for it.
PAYMENT_EVENT_INSERT_ATTEMPTS = 3

# The provider counts money in the currency's smallest unit; Decimal
# arithmetic, never float.
MINOR_UNITS_PER_MAJOR = Decimal(100)

# A hex-encoded SHA-256 digest has a fixed length; anything else is refused
# before the comparison.
WEBHOOK_SIGNATURE_HEX_LENGTH = hashlib.sha256().digest_size * 2

# The money is already in for these; a second capture event is a duplicate
# delivery of a fact already held.
CAPTURED_PAYMENT_STATES = ("captured", "partially_refunded", "refunded")


def _store_money(amount_minor_units):
    """Provider minor units -> the store's Decimal money, or ``None``.

    Deliberately strict: only a non-negative int is money, because guessing a
    Decimal out of a malformed provider value is how float money starts; a
    refused value still leaves the delivery recorded, just without an amount.
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

    ``hmac.compare_digest`` is constant-time; the candidate is length-checked
    first because it raises ``TypeError`` on non-ASCII input, which on this
    unauthenticated endpoint would be an attacker-reachable 500. The value is
    compared verbatim, never stripped.
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
    provider string is a silent success in development and a 500 on this
    unauthenticated money endpoint in production. The bound is read off the
    column, so it cannot drift from the schema.
    """
    return len(value) <= PaymentEvent._meta.get_field(field_name).max_length


def _stored_reference(value):
    """The provider payment reference as this store can store it.

    A reference that does not fit the column becomes the empty reference (the
    model's "named no payment") rather than truncated: a truncated id is a
    plausible-looking reference that reconciles against nothing.
    """
    return value if _fits(value, "gateway_payment_id") else ""


def _apply_captured(event, body):
    """Reconcile a captured payment onto its order; return the outcome.

    Every refusal still records the event (the caller commits it): an unknown
    order, a mismatched reference, a wrong amount or an undeclared edge are all
    facts reconciliation needs, and none may move money.
    """
    payment = _entity(body, "payment")
    gateway_payment_id = str(payment.get("id") or "")
    gateway_order_id = str(payment.get("order_id") or "")
    amount = _store_money(payment.get("amount"))
    target_status = status_for_payment(PAYMENT_CAPTURED)
    # An oversized reference is refused like any other unbindable one, keeping
    # no reference rather than a truncated one.
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
        # The Order lock is the concurrency authority: this delivery and a
        # simultaneous callback verify serialize on it, so the "already
        # captured" question below is answered against committed state.
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
            # The order is already bound to a DIFFERENT gateway payment, so the
            # payload does not get to say what this order's money was.
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
            # this endpoint must never reconcile: the amount is the tie.
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
            # Idempotent no-op: the unique event_id handles the true replay,
            # this handles the duplicate FACT (callback, or another event id).
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
            # Both machine gates, asked rather than restated; a cancelled or
            # already advanced order is out of scope for a capture.
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
        # The sale belongs to orders.inventory, shared with the customer
        # callback, and runs BEFORE the capture state is written: the service's
        # idempotency guard IS the payment dimension, so writing "captured"
        # first would make this call skip its own sale.
        try:
            commit_order_sale(order)
        except StockUnavailable as unavailable:
            # The gateway holds this money whatever this store decides, so the
            # capture cannot be un-applied: refusing to move stock is the only
            # honest answer, and the recorded event plus this audit row are what
            # the finance operator reconciles the paid-but-unshipped order from.
            logger.info(
                "Payment webhook %s: order %s has no stock for product %s "
                "(requested %s, available %s); the capture is recorded but "
                "nothing was sold",
                event.event_id,
                order.id,
                unavailable.product_id,
                unavailable.requested,
                unavailable.available,
            )
            AuditEvent.record(
                AuditEvent.EventType.PAYMENT_STOCK_CONFLICT,
                order=order,
                detail={
                    "event_id": event.event_id,
                    "product_id": unavailable.product_id,
                    "requested": unavailable.requested,
                    "available": unavailable.available,
                },
            )
            order.stock_reservations.filter(
                status=StockReservation.Status.ACTIVE
            ).update(status=StockReservation.Status.RELEASED)
            return PaymentEvent.Outcome.REFUSED

        order.status = target_status
        order.fulfilment_status = fulfilment_for_status(target_status)
        order.payment_status = PAYMENT_CAPTURED
        # Binding the reference makes the next delivery and callback verify
        # reconcile against this same payment.
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
        # Same notification the customer-callback writer queues: the outbox row
        # is written inside this transaction, the drain worker sends it, and the
        # dedup_key collapses the second enqueue for one payment. A failed
        # enqueue is logged and swallowed so it never breaks the capture.
        try:
            notifications.enqueue(
                AuditEvent.EventType.ORDER_PAID,
                {"order": order},
            )
        except Exception:
            logger.exception(
                "order.paid notification enqueue failed; capture stays "
                "applied order=%s",
                order.id,
            )

    return PaymentEvent.Outcome.APPLIED


def _apply_refunded(event, body):
    """Record a settled refund; return the outcome. Moves nothing.

    The refund seam owns refunds, so a delivery here is its echo, an out-of-band
    refund or an operator action at the provider: the row (and the order when we
    can prove which one) is recorded for the finance operator to reconcile.
    """
    refund = _entity(body, "refund")
    gateway_payment_id = str(refund.get("payment_id") or "")

    event.gateway_payment_id = _stored_reference(gateway_payment_id)
    event.amount = _store_money(refund.get("amount"))

    if not event.gateway_payment_id:
        # Nothing to bind the refund to; recorded, moved nothing.
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

    # Genuine, recorded, unhandled: discarding a delivery the provider considers
    # real would make this table an incomplete record of what happened.
    logger.info(
        "Payment webhook %s: event type %r has no handler; recorded only",
        event.event_id,
        event.event_type,
    )
    return PaymentEvent.Outcome.RECORDED


@api_view(["POST"])
@permission_classes([AllowAny])
# Server-to-server: the gateway sends no session cookie or bearer token, so this
# opts out of the credential chain (and the session-only CSRF gate) but is
# deliberately NOT exempt from signature verification, its credential instead.
@authentication_classes([])
@throttle_scope("webhook")
def razorpay_webhook(request):
    """POST /api/v1/webhooks/razorpay/ - a signed payment event delivery.

    Answers 200 for every delivery recorded (applied, refused, recorded only or
    a replay) so the provider stops retrying; 400 for one it will never accept;
    503 while no webhook secret is configured.
    """
    # The RAW body, before anything parses it: DRF marks request.data access as
    # having consumed the stream, and the signature covers the exact bytes.
    raw_body = request.body

    secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        # Fail closed: with no secret no signature could be valid, so 503 (a
        # deployment error) shows a failing endpoint rather than a quiet one
        # that loses money.
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
        # One uniform answer for "missing" and "wrong", and nothing recorded:
        # an unauthenticated caller must not write rows into the financial
        # trail.
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
        # Without the provider's delivery id there is nothing to deduplicate on;
        # the unique constraint is the replay defence.
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
        # A valid signature over bytes that are not JSON: 400 tells the provider
        # not to retry a delivery this store can never read.
        logger.warning("Payment webhook %s: body is not valid JSON", event_id)
        return Response(
            {"error": "Invalid webhook payload"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not isinstance(body, dict):
        logger.warning(
            "Payment webhook %s: body is JSON but not an event object", event_id
        )
        return Response(
            {"error": "Invalid webhook payload"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    event_type = str(body.get("event") or "")

    if not _fits(event_id, "event_id") or not _fits(event_type, "event_type"):
        # Refused rather than truncated: event_id is the replay key and a
        # truncated event_type names an event never received; bounding against
        # the column widths keeps SQLite and Postgres identical.
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
            # The insert IS the replay defence, inside the same transaction as
            # the effect: a second delivery violates the unique constraint and
            # the block rolls back.
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
                # A replay of an event already recorded: acknowledge it so the
                # provider stops retrying, with no second order write or audit
                # row.
                logger.info(
                    "Payment webhook %s: replay of an already-recorded event",
                    event_id,
                )
                return Response(
                    {"status": "duplicate", "event_id": event_id},
                    status=status.HTTP_200_OK,
                )
            if attempt == PAYMENT_EVENT_INSERT_ATTEMPTS - 1:
                # A collision on some OTHER constraint is not a replay: let it
                # surface as a 500 so the provider retries. Also the loop's last
                # turn, so it cannot fall through to an implicit None.
                logger.exception(
                    "Payment webhook %s: could not be recorded",
                    event_id,
                )
                raise

"""In-process and queued notification service.

``send_email`` is the single send path; ``dispatch``/``dispatch_on_commit`` send
inline or post-commit, and ``enqueue`` records the intent in the outbox for the
drain loop to deliver at-least-once.
"""

import json
import logging
from collections import namedtuple
from datetime import timedelta
from enum import Enum

from django.apps import apps
from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, models, transaction
from django.db.models import Count
from django.template.loader import render_to_string
from django.utils import timezone

from common.models import AuditEvent, NotificationOutbox

# Dedicated channel so deployments can route notification output in log tooling.
logger = logging.getLogger("common.notifications")


def send_email(template_name, context, subject, recipient):
    """Render ``emails/<template_name>.txt`` and send it to one recipient.

    The single send path, with the sender always from settings. Raises on failure;
    ``dispatch`` wraps it for sends that must never break a transaction.
    """
    message = render_to_string(f"emails/{template_name}.txt", context)
    send_mail(
        subject,
        message,
        settings.DEFAULT_FROM_EMAIL,
        [recipient],
        fail_silently=False,
    )
    logger.info("email sent template=%s to=%s", template_name, recipient)


def _notify_order_paid(context):
    """order.paid -> order-confirmation email."""
    order = context.get("order")
    if order is None:
        # A call-site bug, not a send failure: warn and keep the caller alive.
        logger.warning("dispatch order.paid: missing order in context")
        return
    send_email(
        "order_confirmation",
        {
            "order": order,
            "frontend_url": settings.FRONTEND_URL,
        },
        f"Your Perfume Store order #{order.id} is confirmed",
        # The account email or the guest address captured at checkout; reading
        # ``order.user.email`` would raise on guest orders.
        order.recipient,
    )


# One handler per event that carries a customer notification.
_EVENT_HANDLERS = {
    AuditEvent.EventType.ORDER_PAID: _notify_order_paid,
}

# The vocabulary in declaration order: ``__members__`` because mypy does not
# model Django's Choices metaclass ``__iter__``.
_EVENT_TYPE_MEMBERS = tuple(AuditEvent.EventType.__members__.values())

# The same vocabulary as a set, built from the tuple above; members are str
# subclasses and compare equal to their values.
_EVENT_TYPE_VALUES = frozenset(_EVENT_TYPE_MEMBERS)


def events_without_handler():
    """Vocabulary members that dispatch to nothing, in declaration order.

    Makes the gap between a recordable event and a delivered notification
    addressable, so a missing handler can be asserted against.
    """
    return tuple(
        member for member in _EVENT_TYPE_MEMBERS if _EVENT_HANDLERS.get(member) is None
    )


def dispatch(event_type, context=None):
    """Send the notification bound to a business event. Never raises.

    Send failures are logged and swallowed so a mail outage cannot break the
    transaction. This sends inline with locks held; prefer ``enqueue`` or
    ``dispatch_on_commit`` where that matters.
    """
    handler = _EVENT_HANDLERS.get(event_type)
    if handler is None:
        # Two absences: a known member with no handler is a content gap (DEBUG);
        # an unknown name is a dead dispatch that can never match (WARNING).
        known = event_type in _EVENT_TYPE_VALUES
        logger.log(
            logging.DEBUG if known else logging.WARNING,
            "dispatch %s: no notification registered%s",
            event_type,
            ""
            if known
            else " (not an AuditEvent.EventType member, so no handler can "
            "ever match it)",
        )
        return
    try:
        handler(context or {})
    except Exception:
        logger.exception("Notification dispatch failed for event %s", event_type)


def dispatch_on_commit(event_type, context=None):
    """Register ``dispatch`` to run once the caller's transaction commits.

    Releases ``select_for_update`` locks before the SMTP round trip; a rollback
    discards the callback. Still on the response path, so latency is unchanged.
    """
    transaction.on_commit(lambda: dispatch(event_type, context))


# A stored context value is an identifier, never prose. Over-long values RAISE
# rather than truncate: a cut URL still looks like a link but fails at the
# customer, so the caller must find out at the call site.
PAYLOAD_VALUE_MAX_LENGTH = 200


class NotificationPayloadError(ValueError):
    """A context value that must not be queued: over-long and not a reference."""


class UnresolvableNotification(LookupError):
    """A stored reference cannot be turned back into a live row.

    A defined outcome, not a crash: every failure mode maps here so a drain loop
    can catch one class and close the row out.
    """


def _describe(value):
    """One context value in a form a table row can hold.

    A model becomes a label/pk reference and containers recurse. Decimals become
    strings (money stays exact), lazy strings are forced here, and sets are sorted
    so the dedup key is stable across workers.
    """
    if isinstance(value, models.Model):
        return {"label": value._meta.label_lower, "pk": value.pk}
    if isinstance(value, dict):
        return {str(key): _describe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_describe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        # Sorted by canonical JSON form so the order is process-independent.
        return sorted((_describe(item) for item in value), key=_canonical)
    if value is None or isinstance(value, (bool, int)):
        return value
    text = str(value)
    if len(text) > PAYLOAD_VALUE_MAX_LENGTH:
        raise NotificationPayloadError(
            f"context value is {len(text)} characters; a queued notification "
            f"value is an identifier and must be at most "
            f"{PAYLOAD_VALUE_MAX_LENGTH} characters. Refusing rather than "
            f"truncating: a cut credential renders as a plausible-looking "
            f"broken link."
        )
    return text


def serialize_context(context):
    """The drainable form of a context: references and scalars.

    Deliberately not run through the audit scrubber: notification payloads
    legitimately hold one-time tokens, so deletion on a clock (``expires_at`` plus
    the purge command) is the compensating control.
    """
    if not isinstance(context, dict):
        return {}
    return {str(key): _describe(value) for key, value in context.items()}


def _canonical(value):
    """Canonical JSON text of a value: independent of key order and
    ``PYTHONHASHSEED``."""
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _is_reference(value):
    """True for the exact shape :func:`_describe` writes for a model.

    A dotted label is required so a caller's own two-key mapping is not mistaken
    for one; malformed labels reach ``apps.get_model`` and raise there.
    """
    return (
        isinstance(value, dict)
        and set(value) == {"label", "pk"}
        and isinstance(value["label"], str)
        and "." in value["label"]
    )


def _dedup_key(event_type, payload, occurrence=None):
    """The canonical identity of one queued notification, used to collapse
    duplicate enqueues.

    It carries no send state (at-least-once comes from the locked claim, not this
    key); ``occurrence`` is the escape hatch for a deliberate repeat.
    """
    identity = {
        "event_type": str(event_type),
        "payload": payload,
        "occurrence": occurrence,
    }
    return _canonical(identity)


def resolve_context(payload):
    """Rebuild a live context from a stored payload, re-reading every reference
    at drain time so the notification reflects current state.

    Raises :class:`UnresolvableNotification` when a row is gone, so the loop can
    dead-letter it instead of retrying forever.
    """
    resolved = {}
    for key, value in (payload or {}).items():
        resolved[key] = _resolve_value(key, value)
    return resolved


def _resolve_value(key, value):
    # The reference check comes first: a reference is itself a two-key mapping.
    if _is_reference(value):
        return _load_reference(key, value["label"], value["pk"])
    if isinstance(value, dict):
        return {
            name: _resolve_value(f"{key}.{name}", item) for name, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_value(key, item) for item in value]
    return value


def _load_reference(key, label, pk):
    """Re-read one stored reference, or raise ``UnresolvableNotification``.

    ``ValueError`` is caught alongside ``LookupError`` because ``apps.get_model``
    raises it for a malformed label, and a drain loop must catch one class.
    """
    try:
        model = apps.get_model(label)
    except (LookupError, ValueError):
        raise UnresolvableNotification(
            f"no model registered for {label!r} (context key {key!r})"
        ) from None
    try:
        return model._default_manager.get(pk=pk)
    except (model.DoesNotExist, ValueError, TypeError):
        raise UnresolvableNotification(
            f"{label} pk={pk!r} referenced by context key {key!r} is gone"
        ) from None


def enqueue(event_type, context=None, occurrence=None):
    """Record the intent to notify, inside the caller's transaction.

    Returns the row, or ``None`` when there is no handler or it is already queued.
    The write is deliberately transactional (not ``on_commit``) and a failure
    propagates: rolling back beats silently losing the customer's mail.
    """
    if _EVENT_HANDLERS.get(event_type) is None:
        # Same two absences as dispatch: a known member is a content gap, an
        # unknown name is a dead dispatch.
        known = event_type in _EVENT_TYPE_VALUES
        logger.log(
            logging.DEBUG if known else logging.WARNING,
            "enqueue %s: no notification registered%s",
            event_type,
            ""
            if known
            else " (not an AuditEvent.EventType member, so no handler can "
            "ever match it)",
        )
        return None
    payload = serialize_context(context or {})
    dedup_key = _dedup_key(event_type, payload, occurrence)
    try:
        with transaction.atomic():
            return NotificationOutbox.objects.create(
                event_type=str(event_type),
                dedup_key=dedup_key,
                payload=payload,
            )
    except IntegrityError:
        # Already queued: a retried hook or the second of two paths for one
        # event. The existing row stands; this says nothing about whether it was
        # ever SENT.
        logger.info(
            "notification already queued event=%s occurrence=%s dedup_key=%s",
            event_type,
            occurrence,
            dedup_key,
        )
        return None


# FAILED is claimable and DEAD is not: that one line IS the dead-letter, so a
# poison row stops consuming batches without triage.
CLAIMABLE_STATUSES = (
    NotificationOutbox.Status.PENDING,
    NotificationOutbox.Status.FAILED,
)

# Fallbacks so a settings module predating a key cannot break the worker.
DEFAULT_DRAIN_BATCH_SIZE = 100
DEFAULT_CLAIM_LEASE_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 30
DEFAULT_RETRY_MAX_SECONDS = 3600

# Clamp before shifting: a hand-edited row could carry a huge attempts value and
# hang the worker computing the backoff.
_BACKOFF_EXPONENT_CAP = 16

# Truncated, not refused: refusing would discard the failure reason, which is
# what a dead-letter exists to say. The full traceback goes to the log.
LAST_ERROR_MAX_LENGTH = 500


class DrainOutcome(Enum):
    """What one row's turn in the loop produced; named values so a typo cannot
    create a sixth bucket."""

    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"
    VANISHED = "vanished"


# Written out because mypy rejects a computed namedtuple field list; a test pins
# the fields to DrainOutcome.
DrainResult = namedtuple(
    "DrainResult", ["sent", "failed", "dead", "vanished", "examined"]
)


def _setting(name, default):
    """One worker knob, with the documented fallback so a missing key cannot stop
    delivery."""
    return getattr(settings, name, default)


def claimable_notifications(now=None):
    """The rows a worker may take right now, oldest first; the one definition of
    the claim predicate. A read-only peek: no lock and no join, so the nullable
    ``FOR UPDATE`` failure cannot arise."""
    now = now or timezone.now()
    return NotificationOutbox.objects.filter(
        status__in=CLAIMABLE_STATUSES,
        next_attempt_at__lte=now,
        expires_at__gt=now,
    ).order_by("created_at", "pk")


def claim_next_notification(now=None, batch_size=None):
    """Take exclusive ownership of one claimable row (its pk), or return None.

    The claim is one short transaction that does not span the send. Locking alone
    would not exclude a second worker, because it re-reads the committed row, so
    the lease write makes the predicate false from the instant the claim commits.
    """
    now = now or timezone.now()
    limit = batch_size or _setting(
        "NOTIFICATION_OUTBOX_DRAIN_BATCH_SIZE", DEFAULT_DRAIN_BATCH_SIZE
    )
    # Must outlast EMAIL_TIMEOUT but not the scheduler interval, or a crashed
    # worker's row sits idle needlessly.
    lease = now + timedelta(
        seconds=_setting(
            "NOTIFICATION_OUTBOX_LEASE_SECONDS", DEFAULT_CLAIM_LEASE_SECONDS
        )
    )
    for pk in claimable_notifications(now).values_list("pk", flat=True)[:limit]:
        claimed = _claim(pk, now, lease)
        if claimed is not None:
            return claimed
    return None


def _claim(pk, now, lease):
    """Lock one candidate, re-check it under the lock, and take it; None means
    another worker won the race."""
    with transaction.atomic():
        row = NotificationOutbox.objects.select_for_update().filter(pk=pk).first()
        if (
            row is None
            or row.status not in CLAIMABLE_STATUSES
            or row.next_attempt_at > now
            or row.expires_at <= now
        ):
            return None
        # Incremented by the CLAIM, before the send, so a worker that dies here
        # still spends an attempt.
        row.attempts += 1
        row.next_attempt_at = lease
        row.save(update_fields=["attempts", "next_attempt_at"])
        return row.pk


def retry_delay_seconds(attempts):
    """How long to wait before the attempt after number ``attempts``.

    Exponential and capped; both ends are settings. No jitter, deliberately, so an
    operator can predict ``next_attempt_at`` from ``attempts``.
    """
    base = _setting(
        "NOTIFICATION_OUTBOX_RETRY_BASE_SECONDS", DEFAULT_RETRY_BASE_SECONDS
    )
    cap = _setting("NOTIFICATION_OUTBOX_RETRY_MAX_SECONDS", DEFAULT_RETRY_MAX_SECONDS)
    exponent = max(0, min(attempts - 1, _BACKOFF_EXPONENT_CAP))
    return min(cap, base * (2**exponent))


def _record_success(pk):
    """Stamp a row whose send completed.

    Conditional, so a racing purge cannot make a lost row look delivered. UPDATE,
    never ``save()``, because save() would re-INSERT a purged row.
    """
    updated = NotificationOutbox.objects.filter(pk=pk).update(
        status=NotificationOutbox.Status.SENT,
        sent_at=timezone.now(),
        last_error="",
    )
    if not updated:
        logger.info("notification outbox row vanished before its stamp pk=%s", pk)
        return DrainOutcome.VANISHED
    logger.info("notification outbox row sent pk=%s", pk)
    return DrainOutcome.SENT


def _alert_dead_letter(pk, event_type, attempts, reason):
    """Raise the admin alert for one row that has just been dead-lettered.

    The import is function-local because ``ops.alerts`` imports this module, and
    this never raises into the caller.
    """
    from ops import alerts

    alerts.notify_background_job_failure(
        f"notification outbox row {pk} (event {event_type!r}) was "
        f"dead-lettered after {attempts} attempt(s): {_error_text(reason)}"
    )


def _record_failure(pk, event_type, attempts, error):
    """Count one failed attempt: retry later, or dead-letter it now.

    The decision is ``attempts >= max`` and nothing else; error-type triage by
    message text is how a retry loop retries a permanent refusal forever.
    """
    exhausted = attempts >= _setting(
        "NOTIFICATION_OUTBOX_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS
    )
    updated = NotificationOutbox.objects.filter(pk=pk).update(
        status=(
            NotificationOutbox.Status.DEAD
            if exhausted
            else NotificationOutbox.Status.FAILED
        ),
        next_attempt_at=timezone.now()
        + timedelta(seconds=retry_delay_seconds(attempts)),
        last_error=_error_text(error),
    )
    if not updated:
        logger.info(
            "notification outbox row vanished before its failure was recorded pk=%s",
            pk,
        )
        return DrainOutcome.VANISHED
    if exhausted:
        logger.error(
            "notification outbox row dead-lettered pk=%s attempts=%d error=%s",
            pk,
            attempts,
            _error_text(error),
        )
        _alert_dead_letter(pk, event_type, attempts, error)
        return DrainOutcome.DEAD
    logger.warning(
        "notification outbox row failed, will retry pk=%s attempt=%d error=%s",
        pk,
        attempts,
        _error_text(error),
    )
    return DrainOutcome.FAILED


def _dead_letter(pk, event_type, attempts, reason):
    """Close out a row no attempt could fix: no reschedule, because an
    unresolvable reference fails identically forever."""
    updated = NotificationOutbox.objects.filter(pk=pk).update(
        status=NotificationOutbox.Status.DEAD,
        last_error=_error_text(reason),
    )
    if not updated:
        logger.info(
            "notification outbox row vanished before it was closed out pk=%s", pk
        )
        return DrainOutcome.VANISHED
    logger.error(
        "notification outbox row dead-lettered pk=%s reason=%s",
        pk,
        _error_text(reason),
    )
    _alert_dead_letter(pk, event_type, attempts, reason)
    return DrainOutcome.DEAD


def _error_text(error):
    """A bounded, typed description; the class name tells operator problems
    apart even when backend formatting varies."""
    text = f"{type(error).__name__}: {error}"
    if len(text) <= LAST_ERROR_MAX_LENGTH:
        return text
    return text[: LAST_ERROR_MAX_LENGTH - 3] + "..."


def _deliver(pk):
    """Send one claimed row and record the outcome; raises only on a DB fault.

    The handler is called directly, not through ``dispatch``: a worker that cannot
    see a failure cannot retry it. A database error is not swallowed, because the
    pass must not report successes it never recorded.
    """
    row = NotificationOutbox.objects.filter(pk=pk).first()
    if row is None:
        return DrainOutcome.VANISHED
    try:
        context = resolve_context(row.payload)
    except UnresolvableNotification as exc:
        return _dead_letter(pk, row.event_type, row.attempts, exc)
    handler = _EVENT_HANDLERS.get(row.event_type)
    if handler is None:
        # A handler existed at enqueue and not now: no retry can produce one.
        return _dead_letter(
            pk,
            row.event_type,
            row.attempts,
            f"no notification registered for event {row.event_type!r}",
        )
    try:
        handler(context)
    except Exception as exc:
        logger.exception(
            "notification outbox send failed pk=%s event=%s attempt=%d",
            pk,
            row.event_type,
            row.attempts,
        )
        return _record_failure(pk, row.event_type, row.attempts, exc)
    return _record_success(pk)


def drain_notifications(batch_size=None, now=None):
    """One pass of the drain loop: claim and send until the batch is done.

    Each row is delivered independently, so one unfixable row does not end the
    pass. The pass stops at the first empty claim (one indexed query when idle)
    and never sleeps.
    """
    limit = batch_size or _setting(
        "NOTIFICATION_OUTBOX_DRAIN_BATCH_SIZE", DEFAULT_DRAIN_BATCH_SIZE
    )
    counts = {outcome.value: 0 for outcome in DrainOutcome}
    examined = 0
    while examined < limit:
        pk = claim_next_notification(now=now, batch_size=limit)
        if pk is None:
            break
        examined += 1
        counts[_deliver(pk).value] += 1
    return DrainResult(examined=examined, **counts)


def outbox_status_counts():
    """How many rows sit in each status, grouped in one query; every status is
    present with a zero so "none" cannot look like "not counted"."""
    counts = {status: 0 for status in NotificationOutbox.Status.values}
    for row in NotificationOutbox.objects.values("status").annotate(total=Count("pk")):
        counts[row["status"]] = row["total"]
    return counts


DeadRetryResult = namedtuple("DeadRetryResult", ["requeued", "expired", "expired_pks"])

# The count is always exact; only the pk list is bounded, because dead-lettering
# a whole batch would otherwise be its own log incident.
DEAD_RETRY_PK_LOG_LIMIT = 20


def retry_dead_notifications(event_type=None):
    """Re-open dead-lettered rows for another attempt (operator retry).

    A row past ``expires_at`` is REFUSED and left DEAD: its payload holds one-time
    token material that the retention window bounds, and an expired link is worse
    than no mail. ``event_type=None`` means every event, not rows whose type is
    NULL.
    """
    now = timezone.now()
    doomed = NotificationOutbox.objects.filter(status=NotificationOutbox.Status.DEAD)
    if event_type is not None:
        doomed = doomed.filter(event_type=event_type)
    # Two queries because they are opposites; ``expires_at`` is NOT NULL, so the
    # buckets are exact complements.
    expired_pks = tuple(doomed.filter(expires_at__lte=now).values_list("pk", flat=True))
    requeued = doomed.filter(expires_at__gt=now).update(
        status=NotificationOutbox.Status.PENDING,
        next_attempt_at=now,
    )
    shown = list(expired_pks[:DEAD_RETRY_PK_LOG_LIMIT])
    listed = ", ".join(str(pk) for pk in shown)
    if len(expired_pks) > len(shown):
        # Its own comma-separated element: "20 (+3 more)" would read as though
        # 20 were annotated.
        listed += f", (+{len(expired_pks) - len(shown)} more)"
    logger.info(
        "notification outbox manual retry re-opened %d dead row(s) event_type=%s; "
        "%d left dead past their retention window, which this retry will not "
        "extend: pks=[%s]",
        requeued,
        event_type or "all",
        len(expired_pks),
        listed,
    )
    return DeadRetryResult(
        requeued=requeued, expired=len(expired_pks), expired_pks=expired_pks
    )

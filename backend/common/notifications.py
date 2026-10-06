"""In-process notification service ([R-19.0], spec 19.1).

One send path for transactional email: ``send_email`` is the only place
that loads a template and hands mail to Django's email backend, so sender
configuration (``settings.DEFAULT_FROM_EMAIL``) and the template home
(``templates/emails/``) have a single address. ``dispatch`` is the
event-driven entry point: views call it explicitly beside their
``AuditEvent.record`` hook inside the same ``transaction.atomic()`` block,
so notifications follow business events instead of scattered manual sends
in controllers.

Deliberately out of scope here (SPEC-2-03): no queue, outbox table,
retries or backoff. ``dispatch`` therefore logs and continues on any send
failure — an SMTP outage must never roll back the business transaction it
notifies about, mirroring the accounts flows where SMTP 503 still leaves
the account created.

``dispatch_on_commit`` is the post-commit variant, added in ASYNC-2b1. A
synchronous SMTP send inside a money-path ``transaction.atomic()`` holds
that block's ``select_for_update`` rows — Order, products, Coupon — for the
whole provider round trip, so one bad minute from a mail provider becomes a
checkout-blocking outage. Deferring the send to ``transaction.on_commit``
releases those locks at commit and opens the socket afterwards.

The honest limit of that fix: ``on_commit`` runs after commit but still
inside the request/response cycle, so a stalled provider still makes *this*
response slow. What it stops is *other* requests blocking behind our locks.

``enqueue`` (ASYNC-2c1) is the substrate for taking the send off the response
path altogether: it writes the INTENT to send into ``NotificationOutbox``,
inside the caller's transaction, and returns. ASYNC-2c2 adds the other half,
``drain_notifications``, which claims and sends those rows with a bounded
retry and a dead-letter — so every property below is now behaviour a caller
can observe, and the tests pin it rather than assert storage invariants.

**No call site is converted by either task, deliberately.** The sites still
call ``dispatch``/``dispatch_on_commit`` and still send, so nothing is
silently undelivered and the table is still empty and inert as shipped. That
is the ordering constraint the whole pair is built around: converting a site
(ASYNC-2c3) before anything drains it would mean a notification that exists
nowhere and is never sent — a silent, permanent loss. The drain loop landing
first is what makes that conversion safe.

What the queue's delivery guarantee is, precisely, because it was previously
overstated here: ``dedup_key`` stops the same notification being QUEUED twice,
and ``status``/``sent_at`` on a row-locked claim are what make it at-least-
once. Only the first used to exist; :func:`claim_next_notification` is the
second.
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

# A dedicated channel name (not __name__) so deployments can route
# notification output in log tooling independently; records reach the
# root handlers of the existing LOGGING baseline (SPEC-7-02).
logger = logging.getLogger("common.notifications")


def send_email(template_name, context, subject, recipient):
    """Render ``emails/<template_name>.txt`` and send it to one recipient.

    The single send path — every transactional email in the project goes
    through here, with the sender always taken from
    ``settings.DEFAULT_FROM_EMAIL`` (never a hardcoded address). Raises on
    failure so request-scoped flows keep their documented SMTP-503 error
    behavior (the accounts recovery views); the event-driven ``dispatch``
    wraps this for sends that must never break their transaction.
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
    """order.paid -> order-confirmation email (the SPEC-19-1 proof event).

    Minimal and factual (order number, total, frontend URL); the full
    order-email content set is SPEC-1-12/SPEC-19-2's scope.
    """
    order = context.get("order")
    if order is None:
        # A hook site passed no order: a programming error at the call
        # site, not a send failure — warn and keep the caller alive.
        logger.warning("dispatch order.paid: missing order in context")
        return
    send_email(
        "order_confirmation",
        {
            "order": order,
            "frontend_url": settings.FRONTEND_URL,
        },
        f"Your Perfume Store order #{order.id} is confirmed",
        # [R-1.13] `recipient` is the account's email or, for a guest order
        # (SPEC-1-B04, no account), the guest address captured at checkout.
        # Reading ``order.user.email`` directly would raise on the guest rows
        # this store can now hold, and dispatch would swallow it into the log
        # while the customer got no confirmation at all.
        order.recipient,
    )


# Event registry: one handler per business event that carries a customer
# notification (spec 19.1). Events without an entry have no notification
# wired yet — they are added explicitly as their content lands.
_EVENT_HANDLERS = {
    AuditEvent.EventType.ORDER_PAID: _notify_order_paid,
}


def dispatch(event_type, context=None):
    """Send the notification bound to a business event. Never raises.

    Call explicitly beside the ``AuditEvent.record`` hook, inside the
    caller's ``transaction.atomic()`` block (rollback-together, same as
    record). Send failures are logged with their traceback and swallowed,
    so a notification outage can never break the business transaction.

    This sends immediately, locks held. Sites that must not sit on their
    ``select_for_update`` rows for an SMTP round trip register the send
    with ``dispatch_on_commit`` instead.
    """
    handler = _EVENT_HANDLERS.get(event_type)
    if handler is None:
        # Normal for the spec-19.1 events that have no notification yet;
        # the DEBUG line keeps a mis-typed event name at a hook site
        # findable without spamming the INFO baseline.
        logger.debug("dispatch %s: no notification registered", event_type)
        return
    try:
        handler(context or {})
    except Exception:
        logger.exception("Notification dispatch failed for event %s", event_type)


def dispatch_on_commit(event_type, context=None):
    """Register ``dispatch`` to run once the caller's transaction commits.

    ASYNC-2b1. For hook sites inside a ``transaction.atomic()`` block that
    holds ``select_for_update`` rows: sending inline holds those locks for
    the length of the SMTP round trip, so a slow provider blocks every other
    checkout touching the same order, SKU or coupon. Registering the send
    instead of performing it means the locks are released at commit and the
    socket opens after.

    Two consequences, both intentional:

    - A rollback discards the callback, so a rolled-back order no longer
      emails anyone. The old inline send could not be recalled.
    - ``dispatch`` still swallows a send failure and logs it at ERROR
      with a traceback, and callbacks run post-commit where a raise cannot
      roll anything back anyway. That keeps a failing send out of an
      already-decided response.

    Still on the response path: ``on_commit`` fires before the response is
    returned, so the SMTP latency is unchanged. Only the lock hold is gone.

    Deliberately opt-in rather than folded into ``dispatch``: making the
    registry itself defer would silently convert the other registry hook
    sites - shipped/delivered (``orders/events.py``) and the webhook
    callback (``orders/webhooks.py``) - and remove any caller's ability to
    send inside its own transaction. The back-in-stock notice is NOT one of
    them: ``products/models.py`` calls ``send_email`` directly and never
    reaches this registry, so no change here could convert it and ASYNC-2b2
    has to move it explicitly. All three are converted one at a time in
    ASYNC-2b2/2b3.
    """
    transaction.on_commit(lambda: dispatch(event_type, context))


# ---------------------------------------------------------------------------
# ASYNC-2c1: the durable outbox. Storage + enqueue only — no drain, and no
# call site converted. See the module docstring for why that is safe today.
# ---------------------------------------------------------------------------

# A stored context value is an identifier, not prose. A notification context
# names rows and settings; nothing in this project puts a paragraph in one.
#
# This bound is a REFUSAL, not a truncation. The audit trail truncates over-long
# values and marks the cut, because an audit record must survive its source
# row's later correction. A queued notification has the opposite obligation:
# a value cut at an arbitrary boundary renders as a plausible-looking but
# broken email — a verification URL missing the last third of its token still
# starts with the right scheme and still reads as a link, and fails at the
# customer, which is the worst place to discover it. So an over-long
# non-reference value raises instead, and the caller finds out at the call site
# rather than in a customer's inbox.
#
# Note this marker is deliberately NOT the audit trail's (that one is a literal
# "." appended by common/audit.py), because the two mechanisms are opposites:
# one records that it cut, this one refuses to cut at all.
PAYLOAD_VALUE_MAX_LENGTH = 200


class NotificationPayloadError(ValueError):
    """A dispatch context carries something that must not be queued.

    Raised by :func:`serialize_context` for a value that is not a model
    reference and is longer than ``PAYLOAD_VALUE_MAX_LENGTH``. Two things
    land here and both are call-site bugs: prose nobody meant to put in a
    notification, and credential material (a password-reset URL, a one-time
    token) that a silent cut would turn into a broken link that still looks
    like a link.
    """


class UnresolvableNotification(LookupError):
    """A stored reference cannot be turned back into a live row.

    Raised by :func:`resolve_context` when the referenced model is not in the
    registry or the referenced row no longer exists (or cannot be addressed
    by the primary key that was stored). This is a DEFINED outcome, not a
    crash: a notification about an order that has since been deleted has no
    recipient and nothing to render, so a drain loop must be able to catch
    this and close the row out rather than die mid-batch on it.

    Every failure mode is mapped onto this ONE exception. That is not
    decoration: ``apps.get_model`` signals a label it cannot parse with
    ``ValueError``, not ``LookupError``, so a hand-edited or malformed label
    such as ``"a.b.c"`` used to escape as an uncaught ``ValueError`` — which
    is the one thing a drain loop cannot catch by looking for this class, and
    so it died mid-batch on exactly the row its docstring said it would
    survive.
    """


def _describe(value):
    """One dispatch-context value in a form a table row can hold.

    A model instance becomes a reference — its model label and primary key,
    never the object. Nested mappings and sequences recurse. Everything else
    is reduced to JSON-native data, and any value that is not already
    ``None``/``int``/``bool`` becomes text first, so three deliberate
    consequences follow:

    - A ``Decimal`` becomes a **string**, never a float: conventions.md holds
      money exact, and a payload round-tripped through JSON must not introduce
      the rounding a float would.
    - A lazy translation string is forced here. Leaving it lazy would store a
      callable-shaped object that resolves against whichever language the
      *draining* process happens to run with, which is not the language the
      recipient's request was in. Resolving at enqueue pins the text to the
      request that asked for the notification; anything that must be re-derived
      against live state belongs behind a reference instead, not in a payload.
    - A ``set``/``frozenset`` is described and then SORTED, not rendered by
      ``str()``. Python's ``str()`` of a set walks it in hash order, which
      varies with ``PYTHONHASHSEED`` — so the same context enqueued by two
      different gunicorn workers produced two different ``dedup_key`` values
      and the duplicate suppression silently did not happen across workers.
    """
    if isinstance(value, models.Model):
        return {"label": value._meta.label_lower, "pk": value.pk}
    if isinstance(value, dict):
        return {str(key): _describe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_describe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        # Sorted by its canonical JSON form, so the order does not depend on
        # which process is describing it.
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
    """The drainable form of a dispatch context: references and scalars.

    A non-mapping is ``{}``, matching ``AuditEvent.record``'s treatment of a
    detail: a context is always a mapping, and silently coercing something
    else into one would invent data.

    This is deliberately NOT run through ``common.audit.clean_audit_payload``.
    The audit trail must never hold a one-time code, but a notification
    legitimately does — the accounts verification and reset emails are built
    out of exactly that material, and dropping it would break the mail this
    queue exists to deliver. So the compensating control is NOT redaction at
    write time; it is deletion on a clock, and the mechanism exists now rather
    than being deferred: every row carries ``expires_at``, written from
    ``settings.NOTIFICATION_OUTBOX_TTL_SECONDS``, and the deletion owner is
    the in-tree ``purge_notification_outbox`` management command. What does
    not exist yet is anything that RUNS that command on a schedule — no cron
    and no beat in this repo — so ``drain_notification_outbox`` offers
    ``--purge-expired`` as an opt-in, **off by default**. Off by default
    because that purge deletes every row past its expiry whatever its status,
    and a dead-lettered row is exactly the one an operator has not retried
    yet: running it on every drain tick would delete failures before anyone
    could read why they failed or ask for a manual retry. Scheduling the
    sweep stays the operator's, exactly as for ``expire_reservations``.
    Two bounds therefore protect the token, and only one of them is this
    repo's: ``PASSWORD_RESET_TIMEOUT`` caps its validity, and ``expires_at``
    caps how long the copy in the queue outlives the reason it was written.
    """
    if not isinstance(context, dict):
        return {}
    return {str(key): _describe(value) for key, value in context.items()}


def _canonical(value):
    """The canonical JSON text of a described value, for keying and ordering.

    ``sort_keys=True`` is what makes the encoding independent of the order the
    caller happened to build the context in, and the default stringifier is
    what makes it independent of ``PYTHONHASHSEED``. Two contexts describing
    the same notification now produce byte-identical text whatever order they
    were assembled in.
    """
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _is_reference(value):
    """True for the exact shape :func:`_describe` writes for a model.

    Both keys must be present and the label must be a dotted model label, so
    a caller's own two-key mapping is never mistaken for a reference and
    re-read out of the registry by accident.

    The label is NOT shape-validated beyond that. A stricter check would be
    worse: a multi-dot or truncated label would then fail this test and be
    handed back to the caller as an ordinary mapping, which is silent. Every
    malformed label instead reaches ``apps.get_model`` and comes back as
    :class:`UnresolvableNotification`.
    """
    return (
        isinstance(value, dict)
        and set(value) == {"label", "pk"}
        and isinstance(value["label"], str)
        and "." in value["label"]
    )


def _dedup_key(event_type, payload, occurrence=None):
    """The identity of one queued notification, encoded canonically.

    **What this is for: stopping the same notification being QUEUED twice.**
    Two paths in this project fire for one payment — the browser callback in
    the order views and the gateway webhook — and a retried hook is the same
    case again. Both would otherwise queue two rows for one customer's
    confirmation, so ``enqueue`` writes the first and refuses the second.

    **What this is NOT for: stopping it being SENT twice.** A previous version
    of this docstring claimed the key was the drain loop's "already handled"
    guard, and that was false. The key is derived only from the event type and
    the payload, is byte-identical before and after any send, and is
    ``unique=True`` — so it can never match a second row and carries no send
    state whatsoever. What makes the queue at-least-once is the row's own
    ``status``/``sent_at`` under a claim that locks it: a worker that dies
    between sending and stamping re-sends *that* row. That claim is
    :func:`claim_next_notification`, and it was still missing when this
    paragraph was first written — which is exactly why the key cannot stand in
    for it.

    **Stability, which is what makes the enqueue-side use safe.** The two
    halves are the event type and the identity of the rows it concerns —
    ``order.paid`` about ``orders.order:7`` names the same notification
    forever, because neither half can change while the event is being queued.
    Mutable state is excluded on purpose: a key built from the order's total
    or its rendered subject would change the moment anything was corrected,
    and the duplicate would sail through.

    **Canonical, not concatenated.** Earlier this was a ``":".join`` of
    rendered fragments, which was injective only in the author's imagination:
    an unescaped separator meant ``{"a": "1", "b": "2"}`` collided with
    ``{"a": "1:b=2"}``, and a caller's insertion order changed the result, so
    the same context built by two different sites produced two keys. It is now
    a length-free structured encoding of the whole payload with sorted keys,
    which is order-independent, collision-free for the value types
    :func:`_describe` can produce, and stable across processes.

    **At-least-once, and a repeat has to be asked for.** ``occurrence`` is the
    deliberate escape hatch. Left ``None``, the key says "this business event
    owes exactly one notification" and a second enqueue collapses — which is
    right for a confirmation and wrong for a resend or a second reminder. A
    caller that genuinely means to notify again passes a discriminator it
    already holds: the reminder's own sequence number, or the id of the
    resend request. That value goes into the key, so the repeat is a NEW row
    with its own send state rather than a mutation of the first — which is
    what "this notification is still owed" actually means.
    """
    identity = {
        "event_type": str(event_type),
        "payload": payload,
        "occurrence": occurrence,
    }
    return _canonical(identity)


def resolve_context(payload):
    """Rebuild a live dispatch context from a stored payload, at drain time.

    Every reference is re-read from the database HERE rather than restored
    from the row, so a notification reflects the state of the order when it
    is sent, not the state it was in when the request enqueued it. That is
    why nothing pre-rendered is stored: no subject, no template name, no
    ``settings.FRONTEND_URL`` snapshot. The handler builds all of that at
    send time from the live instance and current settings, exactly as it does
    on the inline path — so queueing a notification cannot quietly change
    what a customer reads.

    Raises :class:`UnresolvableNotification` when a referenced row is gone or
    unaddressable. A deleted order has no recipient and nothing to render, so
    the drain loop closes the row out rather than treating it as a send
    failure to retry forever: :func:`drain_notifications` catches this class,
    dead-letters the row and carries on with the rest of the batch, which is
    the outcome this docstring has always promised. Not retrying is the point
    — the row it names is gone, so a second attempt would fail identically
    forever and the batch would be consumed by it.
    """
    resolved = {}
    for key, value in (payload or {}).items():
        resolved[key] = _resolve_value(key, value)
    return resolved


def _resolve_value(key, value):
    # The reference check comes FIRST: a reference is itself a two-key
    # mapping, so recursing into mappings ahead of it would walk straight
    # through one and hand it back unresolved.
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
    """Re-read one stored reference, or say clearly why it cannot be.

    Every failure mode is a defined :class:`UnresolvableNotification`: a label
    no longer in the registry, a row since deleted, and a primary key that no
    longer addresses the column it was stored for (a hand-edited or migrated
    row). The last two are ``DoesNotExist``/``ValueError`` from the ORM, which
    a drain loop must not have to know the difference between.

    ``ValueError`` is caught from ``apps.get_model`` for the same reason and
    it is not hypothetical: a stored label of ``"a.b.c"`` unpacks into three
    parts there and raises ``ValueError: too many values to unpack``, which is
    not a ``LookupError``. Catching only ``LookupError`` let that one escape
    as an uncaught ``ValueError`` — the single failure a drain loop filtering
    on this exception cannot survive.
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
    """Record the intent to notify, in the caller's transaction. ASYNC-2c1.

    Returns the ``NotificationOutbox`` row it wrote, or ``None`` when there is
    nothing to record: an event with no registered handler (the same no-op
    ``dispatch`` makes, so converting a call site cannot turn a working
    no-op into a silently-undelivered row), or a notification that is already
    queued under the same ``dedup_key``.

    ``occurrence`` is how a caller asks for the same notification TWICE. Left
    ``None``, the key says one business event owes one notification and a
    repeat collapses to ``None`` — correct for a confirmation, and wrong for a
    resend after a mis-send or a second reminder, which were structurally
    impossible before this parameter existed. Pass a discriminator the caller
    already holds (a reminder's sequence number, a resend request id) and the
    repeat becomes its own row with its own send state. See
    :func:`_dedup_key` for why the key cannot serve as the send-side guard.

    Every row gets ``expires_at`` from the model's own field default, which
    reads ``settings.NOTIFICATION_OUTBOX_TTL_SECONDS``. The payload is
    deliberately not scrubbed of one-time tokens (see
    :func:`serialize_context`), so that bound plus the in-tree
    ``purge_notification_outbox`` command are what stop credential material
    outliving the queue.

    **Inside the transaction, deliberately — not via ``transaction.on_commit``,
    which is what ``dispatch_on_commit`` does.** The two operations have
    opposite failure modes and need opposite placement:

    - ``dispatch_on_commit`` performs a send, which is external and cannot be
      undone. Sending before commit emails a transaction that may still roll
      back; deferring it to on_commit fixes that and buys nothing
      transactionally, because there is no transaction to be in.
    - ``enqueue`` only writes a row. Writing it inside the caller's block is
      what makes the notification part of the business transaction: either the
      order and its notification both commit or neither does. Registering the
      write on ``on_commit`` would reintroduce exactly the window this task
      exists to remove — commit succeeds, the process dies before the callback
      runs, and the row is never created, so the customer is owed a
      confirmation that exists nowhere and no failure was ever raised. The
      insert costs nothing worth deferring: it holds no SMTP socket and adds
      no lock wait beyond the row's own.

    **A failed enqueue propagates; it is not logged and swallowed.** That is a
    deliberate divergence from ``dispatch``, and the reasoning is the same:
    ``dispatch`` swallows because an SMTP failure is an external outage that
    cannot be rolled back with the transaction, whereas a failure to write
    this row means the transaction cannot record what it owes, and swallowing
    it would produce precisely the dual-write this substrate removes — a
    confirmed order with a silently missing notification. Rolling the
    checkout back is the recoverable outcome; losing the email is not. The
    operational consequence is honest and worth stating: a missing or
    unapplied ``common_notificationoutbox`` table turns every converted call
    site into a 500 until migrations run. That is visible on the first
    request, not silent, which is the trade this task is making.

    The duplicate branch uses a savepoint, so an IntegrityError from the
    ``dedup_key`` unique constraint rolls back only the insert and leaves the
    caller's transaction usable (conventions.md: unique generation is
    IntegrityError-handled, never check-then-act).
    """
    if _EVENT_HANDLERS.get(event_type) is None:
        # Same contract as dispatch: nothing is wired for this event yet, and
        # a DEBUG line keeps it findable without implying a row exists.
        logger.debug("enqueue %s: no notification registered", event_type)
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
        # Already queued under this key: either a retried hook or the second
        # of two paths that both fire for one event (the browser callback and
        # the webhook). The customer should receive one email, so the existing
        # row stands and nothing new is written. This says nothing about
        # whether that row was ever SENT — see _dedup_key.
        logger.info(
            "notification already queued event=%s occurrence=%s dedup_key=%s",
            event_type,
            occurrence,
            dedup_key,
        )
        return None


# ---------------------------------------------------------------------------
# ASYNC-2c2: the drain loop — claim, send, bounded retry, dead-letter.
# See the module docstring for the at-least-once guarantee and for why the
# claim, not the dedup key, is what enforces it.
# ---------------------------------------------------------------------------

# The statuses a worker may take a row from. FAILED is here and DEAD is not,
# and that one line IS the dead-letter: a row that failed with retries left is
# still owed a send, and a row that has exhausted them or can never resolve is
# not claimed by anybody again. A poison row therefore stops consuming batches
# without the loop being told which rows are poison.
CLAIMABLE_STATUSES = (
    NotificationOutbox.Status.PENDING,
    NotificationOutbox.Status.FAILED,
)

# Defaults for settings that do not define the knob. They are written here
# rather than only in settings.py so the value a reader finds next to the code
# that uses it is the value in force, and so a settings module predating a key
# cannot make the worker's arithmetic fail.
DEFAULT_DRAIN_BATCH_SIZE = 100
DEFAULT_CLAIM_LEASE_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 30
DEFAULT_RETRY_MAX_SECONDS = 3600

# The exponent is clamped before shifting. attempts is bounded by
# DEFAULT_MAX_ATTEMPTS on the claim path, but a hand-edited or migrated row can
# carry any value at all, and 2 ** attempts on one of those would build an
# integer large enough to be slow on its own — a worker that hangs computing a
# backoff holds no locks and still delivers nothing.
_BACKOFF_EXPONENT_CAP = 16

# An error string kept for an operator, not for a forensic record. It is
# TRUNCATED rather than refused, which is the opposite of
# PAYLOAD_VALUE_MAX_LENGTH's rule and for the opposite reason: refusing here
# would mean refusing to record why a notification failed, which is the one
# thing a dead-letter exists to say. The full traceback goes to the log.
LAST_ERROR_MAX_LENGTH = 500


class DrainOutcome(Enum):
    """What one row's turn in the loop produced.

    Named values rather than bare strings because the loop counts them and an
    operator reads the summary, and a typo in a string would silently create a
    sixth bucket that nothing ever increments.
    """

    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"
    VANISHED = "vanished"


DrainResult = namedtuple(
    "DrainResult", [outcome.value for outcome in DrainOutcome] + ["examined"]
)


def _setting(name, default):
    """One worker knob, read from settings with the documented fallback.

    ``getattr`` rather than a direct attribute so a settings module predating
    a key degrades to the documented default instead of raising
    AttributeError inside a drain pass — a missing tuning knob must never be
    the reason notifications stop going out.
    """
    return getattr(settings, name, default)


def claimable_notifications(now=None):
    """The rows a worker may take right now, oldest first. Read-only.

    The claim predicate, in one place so the loop and its tests cannot
    disagree about it. Three things must hold for a row to be claimable: a
    claimable status, a ``next_attempt_at`` that has arrived (which is both
    the backoff and the in-flight lease), and an ``expires_at`` that has not.

    No lock and no join: this is the PEEK. ``NotificationOutbox`` has no
    foreign keys at all, so there is nothing here that could become the
    ``LEFT OUTER JOIN ... FOR UPDATE`` which once 500'd every payment
    verification on PostgreSQL.
    """
    now = now or timezone.now()
    return NotificationOutbox.objects.filter(
        status__in=CLAIMABLE_STATUSES,
        next_attempt_at__lte=now,
        expires_at__gt=now,
    ).order_by("created_at", "pk")


def claim_next_notification(now=None, batch_size=None):
    """Take exclusive ownership of one claimable row, or return ``None``.

    Returns the row's pk, never the instance: the claim's transaction commits
    before anything is sent, and an object read inside it would be a snapshot
    of a row another worker may be about to change.

    **The claim is one short transaction and it does not span the send.** That
    is the point of moving sends off the request path: a worker that opened its
    mail socket while holding a row lock would reintroduce, on the outbox,
    exactly the lock-hold-across-SMTP problem ASYNC-2b1 removed from the
    checkout.

    Two things together make two workers incapable of taking one row:

    1. ``select_for_update()``, so the re-check below happens against a locked
       row. This queryset joins nothing (see :func:`claimable_notifications`),
       so the nullable-outer-join failure cannot arise in it.
    2. The lease write. The predicate is ``next_attempt_at <= now`` and the
       claim pushes ``next_attempt_at`` out by the lease, so the predicate is
       FALSE for every other worker from the instant this claim commits. The
       lock ALONE would not do it: a second worker blocked on the lock
       re-reads the committed row, still sees a claimable status, and would
       send the same row a second time.

    The re-check inside the lock is not redundant with the peek — it covers the
    window between them — and its failure is a normal outcome, not an
    exceptional one: it is how a worker is told it lost a race. The loop then
    tries the next candidate, so losing one row does not end the pass.
    """
    now = now or timezone.now()
    limit = batch_size or _setting(
        "NOTIFICATION_OUTBOX_DRAIN_BATCH_SIZE", DEFAULT_DRAIN_BATCH_SIZE
    )
    # The lease is env-driven because it is a deployment property: it has to
    # outlast EMAIL_TIMEOUT, or a slow provider would let a second worker
    # begin the same send while this one is still inside it, and it must not
    # outlast the scheduler's interval or a crashed worker's row sits idle for
    # no reason.
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
    """Lock one candidate, re-check it under the lock, and take it.

    Returns the pk when this worker now owns the row, ``None`` when another
    worker got there first (or the row went away between the peek and here).
    """
    with transaction.atomic():
        row = NotificationOutbox.objects.select_for_update().filter(pk=pk).first()
        if (
            row is None
            or row.status not in CLAIMABLE_STATUSES
            or row.next_attempt_at > now
            or row.expires_at <= now
        ):
            return None
        # attempts is incremented by the CLAIM, before the send, so a worker
        # that dies here has still spent an attempt. Counting only completed
        # attempts is how a poison row becomes an unbounded retry loop.
        row.attempts += 1
        row.next_attempt_at = lease
        row.save(update_fields=["attempts", "next_attempt_at"])
        return row.pk


def retry_delay_seconds(attempts):
    """How long to wait before the attempt after number ``attempts``.

    Exponential in ``attempts``, capped: a provider down for a minute should
    be retried in seconds, and one down for an hour should not be retried every
    second of that hour. Both ends of that sentence are settings, so a
    deployment tunes the shape without a code change and no magic threshold
    lives here. The invariant that matters is the one the tests pin: a row
    whose next attempt is in the future is not claimed.

    No jitter, deliberately. Jitter spreads a herd of rows that all failed at
    the same instant, which is worth having at scale, and it is not worth
    making a worker's schedule unobservable in a test or a ``next_attempt_at``
    an operator cannot predict from ``attempts``.
    """
    base = _setting(
        "NOTIFICATION_OUTBOX_RETRY_BASE_SECONDS", DEFAULT_RETRY_BASE_SECONDS
    )
    cap = _setting("NOTIFICATION_OUTBOX_RETRY_MAX_SECONDS", DEFAULT_RETRY_MAX_SECONDS)
    exponent = max(0, min(attempts - 1, _BACKOFF_EXPONENT_CAP))
    return min(cap, base * (2**exponent))


def _record_success(pk):
    """Stamp a row whose send completed.

    Conditional, so the purge command racing this step cannot make a lost row
    look delivered: the statement matches no row, and the pass says so rather
    than counting a notification it cannot prove was recorded. It is an UPDATE
    and never a ``save()`` on an instance — Django's ``save()`` on a row that
    has been deleted re-INSERTs it, which would resurrect a payload the purge
    command deleted precisely because it should not still exist.
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


def _record_failure(pk, attempts, error):
    """Count one failed attempt: retry later, or dead-letter it now.

    ``attempts`` is passed in rather than read here. The row was already read
    by :func:`_deliver` and the claim that reserved it pushed its lease out, so
    no other worker can be writing it; reading it again would be a second
    answer to a question that has one.

    The terminal decision is ``attempts >= max`` and nothing else. A row that
    cannot be delivered is one whose every attempt fails, and a row that has
    failed ``max`` times has by definition reached the bound the deployment
    set. There is no second opinion and no error-type triage: a permanently
    refused recipient and a temporarily unavailable server arrive as the same
    Python exception here, and telling them apart by matching text is how a
    retry loop ends up retrying a refusal forever.
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
        return DrainOutcome.DEAD
    logger.warning(
        "notification outbox row failed, will retry pk=%s attempt=%d error=%s",
        pk,
        attempts,
        _error_text(error),
    )
    return DrainOutcome.FAILED


def _dead_letter(pk, reason):
    """Close out a row that no attempt could ever fix.

    Distinct from :func:`_record_failure` in that the row is not rescheduled:
    an unresolvable reference is not a transient fault, so spending attempts
    waiting to fail identically would only keep it consuming batches. The
    attempt its claim already spent is still recorded, which is what keeps the
    count an honest record of what the worker did.
    """
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
    return DrainOutcome.DEAD


def _error_text(error):
    """A bounded, typed description of one failure.

    The class name is kept because "SMTPRecipientsRefused" and
    "TemplateDoesNotExist" are different operator problems, and inside this
    project that name is the only part of the text that reliably survives an
    email backend's own message formatting.
    """
    text = f"{type(error).__name__}: {error}"
    if len(text) <= LAST_ERROR_MAX_LENGTH:
        return text
    return text[: LAST_ERROR_MAX_LENGTH - 3] + "..."


def _deliver(pk):
    """Send one claimed row and record the outcome. Raises only on a DB fault.

    The registry handler is called DIRECTLY rather than through ``dispatch``,
    and that is the one deliberate difference from the request path:
    ``dispatch`` swallows every exception, which is right when a notification
    must not break the checkout that triggered it and wrong here, because a
    worker that cannot see a failure cannot retry it. Calling the handler still
    goes through ``send_email``, so exactly one function hands mail to a
    backend.

    A database error is deliberately NOT swallowed. Infrastructure faults are
    not this row's fault, and a pass that kept going would report successes for
    rows it never actually recorded; dying and being restarted is the honest
    response, and the lease bounds how long the table waits for it.
    """
    row = NotificationOutbox.objects.filter(pk=pk).first()
    if row is None:
        return DrainOutcome.VANISHED
    try:
        context = resolve_context(row.payload)
    except UnresolvableNotification as exc:
        return _dead_letter(pk, exc)
    handler = _EVENT_HANDLERS.get(row.event_type)
    if handler is None:
        # The event had a handler when the row was queued and has none now.
        # Nothing can render it and no retry will ever produce a handler, so
        # this is the same shape as an unresolvable reference: close it out.
        return _dead_letter(
            pk, f"no notification registered for event {row.event_type!r}"
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
        return _record_failure(pk, row.attempts, exc)
    return _record_success(pk)


def drain_notifications(batch_size=None, now=None):
    """One pass of the drain loop: claim and send until the batch is done.

    Returns a :class:`DrainResult`. Each row is delivered independently: one
    that dead-letters, or whose send fails and is rescheduled, ends its own
    turn and the pass moves on. That is the property the substrate's own
    docstrings promise — ``resolve_context`` raises a defined error precisely
    so the loop can survive the row it names — so it is the first thing the
    tests pin, with the unfixable row in the MIDDLE of a batch rather than at
    an end where passing would be free.

    ``batch_size`` bounds one pass, and the pass stops the moment a claim finds
    nothing, so an idle table costs a single indexed query. Nothing sleeps:
    like ``expire_reservations`` and ``purge_notification_outbox`` this is a
    sweep a scheduler runs, and a pass handed a batch it cannot fill does not
    sit in a retry sleep pretending to be a long-running worker.
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
    """How many rows sit in each status. The observability half of §19.3.

    Grouped in the database rather than by counting each status separately, so
    the report an operator reads is one query and cannot disagree with itself.
    Every status the vocabulary admits is present in the result with a zero,
    because "no rows are dead-lettered" and "this report cannot count
    dead-lettered rows" must not look the same.
    """
    counts = {status: 0 for status in NotificationOutbox.Status.values}
    for row in NotificationOutbox.objects.values("status").annotate(total=Count("pk")):
        counts[row["status"]] = row["total"]
    return counts


def retry_dead_notifications(event_type=None):
    """Re-open dead-lettered rows for another attempt (operator, ASYNC-2c2).

    The manual-retry capability §19.3 asks for. Its authorization is the same
    one every other operator sweep in this repository has — control of the
    process that runs ``manage.py`` — because this is a management command with
    no request, no user, and therefore no capability to check. That is this
    project's existing mechanism rather than a way around one: an API
    endpoint for the same action would need a new capability in
    ``common/roles.py``, which is not this task's file, and substituting an
    inline ``is_staff`` check is what conventions.md forbids.

    ``attempts`` is DELIBERATELY preserved. The row has failed that many times
    and forgetting it would make the count a lie; if the send fails again the
    row goes straight back to ``DEAD``, so this cannot be used to build an
    unbounded loop. If the send succeeds the row is ``SENT`` and its history
    stays visible as a count of how many times this notification had not gone
    out.

    ``event_type=None`` means EVERY event, not "rows whose event type is null",
    which is what a plain ``filter(event_type=event_type)`` would have done —
    a manual retry that silently matched nothing because it compared a column
    against NULL.

    Returns the number of rows re-opened.
    """
    doomed = NotificationOutbox.objects.filter(status=NotificationOutbox.Status.DEAD)
    if event_type is not None:
        doomed = doomed.filter(event_type=event_type)
    requeued = doomed.update(
        status=NotificationOutbox.Status.PENDING,
        next_attempt_at=timezone.now(),
    )
    logger.info(
        "notification outbox manual retry re-opened %d dead row(s) event_type=%s",
        requeued,
        event_type or "all",
    )
    return requeued

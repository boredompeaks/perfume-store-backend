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
inside the caller's transaction, and returns. Nothing drains that table yet
— the worker is ASYNC-2c2 — so every property below is stated as an invariant
the storage guarantees and the tests pin, not as behaviour a caller can
observe today. Deliberately, **no live call site was converted**: the sites
still call ``dispatch``/``dispatch_on_commit`` and still send, so no
notification is silently undelivered by this change. The table is therefore
empty and inert as shipped, and the two later tasks are what make it move:
converting a site is ASYNC-2c3, and a row written by a converted site sits
undelivered until ASYNC-2c2 lands the loop that drains it. Converting before
that would be a silent, permanent loss.

What the queue's delivery guarantee is, precisely, because it was previously
overstated here: ``dedup_key`` stops the same notification being QUEUED twice,
and ``status``/``sent_at`` on a row-locked claim are what make it at-least-
once. Only the first exists today.
"""

import json
import logging

from django.apps import apps
from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, models, transaction
from django.template.loader import render_to_string

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
    not exist yet is anything that RUNS that command on a schedule — no cron,
    no beat, no worker call — so scheduling it is ASYNC-2c2's, the same way
    ``expire_reservations`` is documented as a command an operator schedules.
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
    between sending and stamping re-sends *that row*. That is ASYNC-2c2's work
    and it does not exist yet.

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
    the drain loop is expected to catch this and close the row out (or
    dead-letter it, in ASYNC-2d) rather than treat it as a send failure to
    retry forever.
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

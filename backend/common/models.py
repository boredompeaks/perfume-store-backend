"""Append-only business-event audit ledger (spec 7.1 Audit module, [R-7.20]).

The privileged-action trail (``common/audit.py``) records what staff did
through the admin and the gated API as Django ``LogEntry`` rows; this model
records what the business did — order lifecycle (created, paid), payment
verification outcomes (intent, success, and every verify failure branch),
and authentication events (registration, login attempts, email
verification, password reset) — the audit-trail half of the section-1 gap
SPEC-1-11 [1.22]. The two trails are complementary: LogEntry answers "which
staff member changed which row", AuditEvent answers "which business events
happened".

Events are written by ``AuditEvent.record`` inside the same
``transaction.atomic()`` block as the side effect they record (the
``StockMovement`` ledger pattern from SPEC-6-02), so trail and effect
commit or roll back together and can never disagree.

``NotificationOutbox`` joins it in ASYNC-2c1 on the same principle, for the
other half of a business event: the notification a customer is owed. The
audit trail records what the system did; the outbox records what it still
owes a person, and — for the same reason — commits or rolls back with the
write that earned it.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.audit import clean_audit_payload
from common.middleware import (
    NO_REQUEST_ID,
    REQUEST_ID_MAX_LENGTH,
    current_request_id,
)

# A dedicated channel name (not this module's __name__) so deployments can
# route or filter the business trail in log tooling independently of model
# noise; settings.LOGGING pins it at INFO (SPEC-7-02).
audit_logger = logging.getLogger("common.audit")

# A queued notification's expiry is a property of writing one, not something
# each caller should be able to forget, so it is a field default rather than a
# required argument: ``NotificationOutbox.objects.create()`` cannot produce a
# row that never expires. The bound is deployment-shaped (it decides how long a
# copy of a one-time token may sit in this table), so it is env-driven through
# settings rather than written here.
NOTIFICATION_OUTBOX_DEFAULT_TTL_SECONDS = 3 * 24 * 60 * 60


def default_notification_outbox_expiry():
    """When a freshly written outbox row stops being worth keeping."""
    return timezone.now() + timedelta(
        seconds=getattr(
            settings,
            "NOTIFICATION_OUTBOX_TTL_SECONDS",
            NOTIFICATION_OUTBOX_DEFAULT_TTL_SECONDS,
        )
    )


class AuditEvent(models.Model):
    """One immutable business event: what happened, to what, by whom, when.

    ``detail`` carries the JSON payload (gateway identifiers, amounts,
    attempted usernames) and keeps the trail meaningful after the
    referenced rows are gone — both foreign keys use ``SET_NULL`` so the
    event outlives the order or user it describes, with the identifying
    values preserved in ``detail``.
    """

    class Category(models.TextChoices):
        ORDER = "order", "Order"
        PAYMENT = "payment", "Payment"
        AUTH = "auth", "Authentication"
        CATALOGUE = "catalogue", "Catalogue"
        STAFF = "staff", "Staff"

    # SPEC-20-4 [R-20.29]: which surface the mutation arrived through. A real
    # field, not prose in a change message, so "did staff do this in the admin
    # or through the API" is a filter rather than a text search. ``SYSTEM`` is
    # the honest label for a business event whose writer does not attribute
    # itself to a staff surface (checkout, payment verification, auth) — it
    # says "not attributable", never "admin".
    class Source(models.TextChoices):
        ADMIN = "admin", "Admin"
        API = "api", "API"
        SYSTEM = "system", "System"

    class EventType(models.TextChoices):
        # Order lifecycle. Further per-transition events ride section 10
        # (the from->to status machine), not here.
        ORDER_CREATED = "order.created", "Order created"
        ORDER_PAID = "order.paid", "Order paid"
        # Payment intent + verify outcomes/failures.
        PAYMENT_INITIATED = "payment.initiated", "Payment intent created"
        PAYMENT_SIGNATURE_REJECTED = (
            "payment.signature_rejected",
            "Payment signature rejected",
        )
        PAYMENT_ORDER_NOT_FOUND = (
            "payment.order_not_found",
            "Payment order not found",
        )
        PAYMENT_ALREADY_PROCESSED = (
            "payment.already_processed",
            "Payment already processed",
        )
        PAYMENT_REFERENCE_MISMATCH = (
            "payment.reference_mismatch",
            "Payment reference mismatch",
        )
        PAYMENT_STOCK_CONFLICT = (
            "payment.stock_conflict",
            "Payment stock conflict",
        )
        PAYMENT_COUPON_INVALID = (
            "payment.coupon_invalid",
            "Payment coupon invalid",
        )
        PAYMENT_VERIFIED = "payment.verified", "Payment verified"
        # Authentication lifecycle.
        AUTH_REGISTERED = "auth.registered", "Account registered"
        AUTH_LOGIN = "auth.login", "Login succeeded"
        AUTH_LOGIN_FAILED = "auth.login_failed", "Login failed"
        AUTH_EMAIL_VERIFIED = "auth.email_verified", "Email verified"
        AUTH_PASSWORD_RESET = "auth.password_reset", "Password reset"
        # Catalogue mutations with their structured before -> after values
        # (SPEC-20-4 [R-20.27]) — the API surface, which could previously
        # only say "Updated via API.".
        CATALOGUE_CREATED = "catalogue.created", "Catalogue item created"
        CATALOGUE_UPDATED = "catalogue.updated", "Catalogue item updated"
        CATALOGUE_DELETED = "catalogue.deleted", "Catalogue item deleted"
        # A staff-role grant/revoke: the most consequential mutation the admin
        # performs, and previously recorded only as per-direction prose.
        STAFF_ROLES_UPDATED = "staff.roles_updated", "Staff roles updated"

    category = models.CharField(max_length=20, choices=Category.choices, db_index=True)
    event_type = models.CharField(
        max_length=50, choices=EventType.choices, db_index=True
    )
    actor = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    detail = models.JSONField(default=dict, blank=True)
    # SPEC-20-4 [R-20.29]: the surface the mutation arrived through — a field
    # so an audit query can separate admin work from API work instead of
    # pattern-matching change messages. Defaults to SYSTEM, meaning the writer
    # makes no staff-surface claim (see ``Source``).
    source = models.CharField(
        max_length=10,
        choices=Source.choices,
        default=Source.SYSTEM,
        db_index=True,
    )
    # SPEC-20-3 [R-20.26]: the correlation id of the request that produced
    # the event, so an audit row can be joined to the request's log lines and
    # to the id echoed on the response. Blank (never NULL) for events written
    # outside a request — management commands, the test fixtures' direct
    # calls — where there is no request to correlate with.
    request_id = models.CharField(
        max_length=REQUEST_ID_MAX_LENGTH, blank=True, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Audit event"
        verbose_name_plural = "Audit events"

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.event_type}"

    @classmethod
    def record(cls, event_type, actor=None, order=None, detail=None, source=None):
        """Append one event; the single sanctioned write path.

        Call inside the same ``transaction.atomic()`` block as the side
        effect being recorded. ``category`` derives from the event-type's
        dotted prefix (``order.``/``payment.``/``auth.``/``catalogue.``/
        ``staff.``), so an unknown or unprefixed identifier fails loudly
        instead of writing a row that no category query will ever find.
        Anonymous/system actors store NULL, mirroring
        ``StockMovement.created_by``.

        ``source`` defaults to :attr:`Source.SYSTEM` — a writer that does not
        name its surface makes no claim about one. The guarded staff
        mutations state it explicitly (``Source.ADMIN`` / ``Source.API``).

        The detail is passed through ``common.audit.clean_audit_payload``
        here rather than at each call site: since SPEC-20-4 began storing
        real before/after values, this is the one place that decides what may
        be written, so no caller can bypass it.
        """
        event = cls.objects.create(
            category=cls.Category(event_type.split(".", 1)[0]),
            event_type=event_type,
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            order=order,
            detail=clean_audit_payload(detail or {}),
            source=source or cls.Source.SYSTEM,
            request_id=current_request_id(),
        )
        # [SPEC-7-02] Observability baseline: the trail is DB-only
        # otherwise, so a log reader has no surface for it. Emitted after
        # the insert with the stored identity; the call writes no rows, so
        # the transaction placement above is untouched.
        #
        # [SPEC-17-09] [R-17.32] "Avoid putting personal information in
        # logs" — decision: usernames are RETAINED here (pseudonymizing to
        # the actor pk would keep only an unreadable id in the abuse/
        # dispute forensics stream), because a username is the login
        # credential, not sensitive PII, and dies with the account row's
        # association. Email/phone/address/full name are FORBIDDEN in
        # this output; the full rationale lives in docs/retention.md.
        audit_logger.info(
            "audit %s id=%s actor=%s order=%s request_id=%s detail=%s",
            event.event_type,
            event.pk,
            event.actor.username if event.actor else None,
            event.order_id,
            event.request_id or NO_REQUEST_ID,
            event.detail,
        )
        return event

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError(
                "AuditEvent rows are append-only: updating an existing "
                "event is forbidden."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            "AuditEvent rows are append-only: deleting an event is forbidden."
        )


class NotificationOutbox(models.Model):
    """One durable, not-yet-sent customer notification (ASYNC-2c1).

    The substrate the post-commit sends in this project still lack. Rows are
    written by ``common.notifications.enqueue`` INSIDE the transaction of the
    business write they describe, so an order that commits leaves its
    notification behind and an order that rolls back leaves nothing — the
    dual-write window (business effect committed, notification lost) that
    ``dispatch_on_commit`` still has is closed by storing the intent rather
    than performing the send.

    What a row holds is deliberately thin. ``payload`` carries identifiers
    and scalars only — a model reference is its label plus primary key, never
    the instance — because the request that enqueued the row is gone by the
    time anything reads it, and an instance or a lazily-evaluated string
    would hold that request's values rather than the row's. The worker
    re-reads each reference at drain time through
    ``common.notifications.resolve_context``, which is also why the row
    stores no template name and no subject: the handler builds those, from
    live state, when it runs.

    ``dedup_key`` is the **enqueue-time** idempotence guard, discussed at
    ``notifications._dedup_key``: it collapses two paths that fire for one
    business event, so the same notification is queued once. It is NOT the
    send-side at-least-once guard and cannot be - it is derived only from the
    event type and the payload, is byte-identical before and after any send,
    and is unique, so it carries no send state and can never match a second
    row. What actually makes the queue at-least-once is this row's own
    ``status``/``sent_at`` under a claim that locks the row: a worker that
    dies between sending and stamping re-sends *this* row. That is ASYNC-2c2's
    work and it does not exist yet.

    ``expires_at`` bounds how long the row — and any token material in its
    payload — may sit, and is what makes the table safe to fill with
    notifications built from one-time codes: the payload is deliberately not
    scrubbed, so deletion on a clock is the compensating control. The deletion
    owner is named and in-tree (``manage.py purge_notification_outbox``);
    nothing schedules it yet, which is stated in that command's docstring.

    State is only what storage needs — ``PENDING`` until some later task
    claims, sends and stamps the row. There is deliberately no claim lease,
    attempt counter, backoff or dead-letter here: those are ASYNC-2c2 (the
    worker) and ASYNC-2d (retry/dead-letter), and until ASYNC-2c2 lands
    **nothing drains this table, so rows accumulate**.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    # An AuditEvent.EventType value (``order.paid``) or the bare transition
    # names the registry keys on. db_indexed because the drain query filters
    # and groups on it.
    event_type = models.CharField(max_length=50, db_index=True)
    # TextField, not a bounded CharField: the key is DERIVED (a canonical
    # encoding of the event type, the payload and any caller-supplied
    # occurrence discriminator), so a length cap would either truncate a key
    # into a collision — silently swallowing a distinct notification — or
    # demand a bound on payload scalars that has no honest justification.
    # Still unique, so the database enforces idempotence rather than a
    # convention.
    dedup_key = models.TextField(unique=True)
    payload = models.JSONField(default=dict, blank=True)
    # db_indexed: the worker's claim query is "oldest PENDING row".
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Stamped by the worker when it sends. NULL means "never confirmed sent",
    # which is not the same as PENDING — a row can fail and be retried — so it
    # is a nullable stamp rather than a status value.
    sent_at = models.DateTimeField(null=True, blank=True)
    # Written at enqueue from settings.NOTIFICATION_OUTBOX_TTL_SECONDS (via
    # the field default, so it cannot be omitted) and db_indexed because the
    # purge command's only query is "everything past this instant". A row that
    # outlives it has either been drained or stranded; either way its payload is
    # no longer needed and may hold credential material that should not still
    # be sitting in a table.
    expires_at = models.DateTimeField(
        db_index=True, default=default_notification_outbox_expiry
    )

    class Meta:
        # FIFO drain order, with the pk as the tiebreak so two rows written
        # in the same clock tick still have a total order (a worker's LIMIT
        # claim needs one).
        ordering = ("created_at", "pk")
        verbose_name = "Notification outbox row"
        verbose_name_plural = "Notification outbox rows"

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.event_type} ({self.status})"


class SavedFilter(models.Model):
    """One staff user's named changelist filter (SPEC-20-6 [R-20.11]).

    Spec 20.1 lists "Saved filters/views where useful" among the data-table
    capabilities and Django ships no equivalent, so the two highest-traffic
    changelists get one. It is a *preference*, not business data: a small
    set of changelist query parameters (``{"status__exact": "pending"}``)
    the user named, scoped to one account and one model.

    Three deliberate properties, all of which the test suite pins:

    - scoped by ``(user, content_type)`` so a saved view is private and is
      deleted with the account, never shared between staff;
    - ``params`` holds a validated subset of what the changelist itself
      accepts — the same gate the changelist applies on replay, so a stored
      spec can only narrow a listing, never widen one;
    - the table holds no domain data of its own. The parameters can name a
      customer's phone or email as a search term, which is why the row
      belongs to the staff user rather than to a shared "team view": it
      lives and dies with the account that typed it.
    """

    NAME_MAX_LENGTH = 60

    user = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="saved_admin_filters",
    )
    # A ContentType rather than two char columns: the model identity is then
    # a real reference (a row cannot outlive the model it points at, and the
    # uniqueness scope is enforced by the database, not by convention).
    content_type = models.ForeignKey(
        "contenttypes.ContentType",
        on_delete=models.CASCADE,
        related_name="saved_admin_filters",
    )
    name = models.CharField(max_length=NAME_MAX_LENGTH)
    params = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)
        # One filter per name per user per model: re-saving a name replaces
        # the selection (it is an update, not a duplicate), and the
        # constraint is what says so.
        constraints = [
            models.UniqueConstraint(
                fields=("user", "content_type", "name"),
                name="unique_saved_filter_per_user_model",
            )
        ]
        verbose_name = "Saved filter"
        verbose_name_plural = "Saved filters"

    def __str__(self):
        return self.name

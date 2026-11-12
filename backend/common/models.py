"""Append-only business-event audit ledger and the notification outbox.

Each is written inside the same transaction as the side effect it records, so trail
and effect commit or roll back together.
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

# Dedicated channel (not __name__) so log tooling can route the trail; pinned
# at INFO in settings.LOGGING.
audit_logger = logging.getLogger("common.audit")

# A field default, not a required argument, so a created row can never lack an
# expiry; the bound itself is env-driven.
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

    Both foreign keys use ``SET_NULL`` so the event outlives the rows it names,
    with identifying values preserved in ``detail``.
    """

    class Category(models.TextChoices):
        ORDER = "order", "Order"
        PAYMENT = "payment", "Payment"
        AUTH = "auth", "Authentication"
        CATALOGUE = "catalogue", "Catalogue"
        STAFF = "staff", "Staff"

    # The surface the mutation arrived through, as a filterable field. ``SYSTEM``
    # means "not attributable", never "admin".
    class Source(models.TextChoices):
        ADMIN = "admin", "Admin"
        API = "api", "API"
        SYSTEM = "system", "System"

    class EventType(models.TextChoices):
        # Order lifecycle transitions. Membership here is NOT delivery:
        # _EVENT_HANDLERS in common.notifications decides what has a handler.
        ORDER_CREATED = "order.created", "Order created"
        ORDER_PAID = "order.paid", "Order paid"
        ORDER_SHIPPED = "order.shipped", "Order shipped"
        ORDER_DELIVERED = "order.delivered", "Order delivered"
        ORDER_CANCELLED = "order.cancelled", "Order cancelled"
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
        # A confirmed sale whose coupon increment passed usage_limit: a RECORD,
        # never a gate (see orders.inventory.commit_order_sale).
        PAYMENT_COUPON_OVER_LIMIT = (
            "payment.coupon_over_limit",
            "Payment coupon over limit",
        )
        PAYMENT_VERIFIED = "payment.verified", "Payment verified"
        AUTH_REGISTERED = "auth.registered", "Account registered"
        AUTH_LOGIN = "auth.login", "Login succeeded"
        AUTH_LOGIN_FAILED = "auth.login_failed", "Login failed"
        AUTH_EMAIL_VERIFIED = "auth.email_verified", "Email verified"
        AUTH_PASSWORD_RESET = "auth.password_reset", "Password reset"
        CATALOGUE_CREATED = "catalogue.created", "Catalogue item created"
        CATALOGUE_UPDATED = "catalogue.updated", "Catalogue item updated"
        CATALOGUE_DELETED = "catalogue.deleted", "Catalogue item deleted"
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
    # Defaults to SYSTEM: the writer makes no staff-surface claim (see
    # ``Source``).
    source = models.CharField(
        max_length=10,
        choices=Source.choices,
        default=Source.SYSTEM,
        db_index=True,
    )
    # Correlation id of the request, blank for events written outside one.
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

        Call inside the same transaction as the side effect. ``category`` derives
        from the dotted prefix, and the detail is sanitized here so no caller can
        bypass the scrubber.
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
        # Emit after the insert with the stored identity, so a log reader has a
        # surface for the DB-only trail. Usernames are retained; email, phone,
        # address and full name are FORBIDDEN (docs/retention.md).
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
    """One durable, not-yet-sent customer notification, written inside the
    business transaction that owes it.

    ``dedup_key`` prevents double-queuing, the row-locked claim makes delivery
    at-least-once and bounded by ``attempts``, and ``expires_at`` bounds how long
    any token material in ``payload`` may sit.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        DEAD = "dead", "Dead-lettered"

    # An AuditEvent.EventType value; indexed because the drain query filters on it.
    event_type = models.CharField(max_length=50, db_index=True)
    # Derived and unique, so the database enforces idempotence. TextField because
    # a length cap would truncate keys into collisions.
    dedup_key = models.TextField(unique=True)
    payload = models.JSONField(default=dict, blank=True)
    # Indexed: the claim query and the status report both filter on it.
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Stamped on send; NULL means never confirmed, which is not the same as PENDING.
    sent_at = models.DateTimeField(null=True, blank=True)
    # Set by the field default (NOTIFICATION_OUTBOX_TTL_SECONDS) and indexed for
    # the purge query; payload token material must not outlive it.
    expires_at = models.DateTimeField(
        db_index=True, default=default_notification_outbox_expiry
    )
    # Counts CLAIMS, not completed sends: a worker that dies after claiming still
    # consumes an attempt, which is what bounds retries. Only the claim writes it.
    attempts = models.PositiveIntegerField(default=0)
    # Earliest claimable instant: the claim pushes it out by the lease (so two
    # workers cannot take one row) and a failure by that attempt's backoff.
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    # Short failure text for the operator surface; cleared on success.
    last_error = models.TextField(blank=True, default="")

    class Meta:
        # FIFO drain order; pk tiebreaks rows written in the same clock tick.
        ordering = ("created_at", "pk")
        verbose_name = "Notification outbox row"
        verbose_name_plural = "Notification outbox rows"

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.event_type} ({self.status})"


class SavedFilter(models.Model):
    """One staff user's named changelist filter, private to the account and model.

    ``params`` is a validated subset of what the changelist accepts, so replaying
    a stored spec can only narrow a listing, never widen one.
    """

    NAME_MAX_LENGTH = 60

    user = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="saved_admin_filters",
    )
    # A real reference, so uniqueness is DB-enforced and the row dies with the
    # model.
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
        # Re-saving a name replaces its selection: one filter per name per user
        # per model.
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

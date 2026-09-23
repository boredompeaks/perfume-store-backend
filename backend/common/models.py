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
"""

import logging

from django.db import models

# A dedicated channel name (not this module's __name__) so deployments can
# route or filter the business trail in log tooling independently of model
# noise; settings.LOGGING pins it at INFO (SPEC-7-02).
audit_logger = logging.getLogger("common.audit")


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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Audit event"
        verbose_name_plural = "Audit events"

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.event_type}"

    @classmethod
    def record(cls, event_type, actor=None, order=None, detail=None):
        """Append one event; the single sanctioned write path.

        Call inside the same ``transaction.atomic()`` block as the side
        effect being recorded. ``category`` derives from the event-type's
        dotted prefix (``order.``/``payment.``/``auth.``), so an unknown or
        unprefixed identifier fails loudly instead of writing a row that no
        category query will ever find. Anonymous/system actors store NULL,
        mirroring ``StockMovement.created_by``.
        """
        event = cls.objects.create(
            category=cls.Category(event_type.split(".", 1)[0]),
            event_type=event_type,
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            order=order,
            detail=detail or {},
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
            "audit %s id=%s actor=%s order=%s detail=%s",
            event.event_type,
            event.pk,
            event.actor.username if event.actor else None,
            event.order_id,
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

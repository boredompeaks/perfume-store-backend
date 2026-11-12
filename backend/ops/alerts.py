"""Admin alert dispatch: plain-transactional email to the configured staff recipients through `common.notifications.send_email`.

Dedup is a per-alert-type cooldown on the dedicated ``alerts`` cache alias; a send failure is logged and swallowed, so an SMTP outage never fails the operation that tripped the alert.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import caches
from django.utils import timezone

from common import notifications

# Dedicated channel so deployments can route alert output independently.
logger = logging.getLogger("ops.alerts")

# Each key names both templates/emails/<key>.txt and the cache-cooldown entry.
LOW_STOCK = "low_stock"
OUT_OF_STOCK = "out_of_stock"
PAYMENT_FAILURE_SPIKE = "payment_failure_spike"
SECURITY_ALERT = "security_alert"
INTEGRATION_OUTAGE = "integration_outage"
BACKGROUND_JOB_FAILURE = "background_job_failure"


def _recipients():
    """Parse ALERT_RECIPIENTS into an ordered, de-duplicated list."""
    seen = []
    for raw in settings.ALERT_RECIPIENTS.split(","):
        address = raw.strip()
        if address and address not in seen:
            seen.append(address)
    return seen


def _in_cooldown(alert_key):
    """True when an alert of this type was sent within the cooldown window.

    The get-then-set is deliberately not atomic (a race costs one duplicate
    mail) and fails open, so a cache outage never kills the alert.
    """
    try:
        if caches["alerts"].get(f"alerts:cooldown:{alert_key}") is not None:
            return True
        caches["alerts"].set(
            f"alerts:cooldown:{alert_key}",
            True,
            timeout=settings.ALERT_COOLDOWN_SECONDS,
        )
    except Exception:
        logger.exception("alert %s cooldown check failed; failing open", alert_key)
    return False


def _send(alert_key, context, subject):
    """One cooldown-checked send through the single email path; True when at least one mail actually left."""
    sent = False
    try:
        if _in_cooldown(alert_key):
            logger.info("alert %s suppressed (cooldown)", alert_key)
            return False
        for recipient in _recipients():
            # Per-recipient isolation: one failing mailbox must not abandon
            # the rest of the staff list.
            try:
                notifications.send_email(
                    f"alert_{alert_key}", context, subject, recipient
                )
                sent = True
            except Exception:
                logger.exception("alert %s send to %s failed", alert_key, recipient)
    except Exception:
        # Log-only: the alert must never break the flow that detected the problem.
        logger.exception("alert %s dispatch failed", alert_key)
    return sent


def _product_lines(rows):
    lines = []
    for row in rows:
        lines.append(f"- {row['name']} (id {row['id']}): {row['stock']} in stock")
    return lines


def _notify_stock_breach(alert_key, subject, rows):
    if not rows or not _recipients():
        return False
    return _send(
        alert_key,
        {
            "products": rows,
            "product_lines": _product_lines(rows),
            "threshold": settings.LOW_STOCK_THRESHOLD,
        },
        subject,
    )


def notify_low_stock(rows):
    """Low-stock breach: any product at 0 < stock <= LOW_STOCK_THRESHOLD, from the id/name/stock dicts the caller already computed."""
    return _notify_stock_breach(
        LOW_STOCK,
        "Low stock alert",
        rows,
    )


def notify_out_of_stock(rows):
    """Out-of-stock breach: products at exactly 0 units."""
    return _notify_stock_breach(
        OUT_OF_STOCK,
        "Out of stock alert",
        rows,
    )


def _failure_rows(recent):
    """Render the failed-payment AuditEvent instances for the alert body."""
    return [
        "- {event_type} order {order_id} at {created_at:%Y-%m-%d %H:%M:%S}".format(
            event_type=row.event_type,
            order_id=row.order_id,
            created_at=row.created_at,
        )
        for row in recent
    ]


def notify_payment_failure_spike(recent):
    """Payment-failure spike: at least PAYMENT_FAILURE_SPIKE_COUNT failed attempts within the trailing window.

    ``recent`` is the caller-filtered AuditEvent queryset; the alert counts and renders whatever crossed the threshold.
    """
    if len(recent) < settings.PAYMENT_FAILURE_SPIKE_COUNT or not _recipients():
        return False
    return _send(
        PAYMENT_FAILURE_SPIKE,
        {
            "count": len(recent),
            "window_seconds": settings.PAYMENT_FAILURE_SPIKE_WINDOW_SECONDS,
            "failure_lines": _failure_rows(recent),
        },
        "Payment failure spike alert",
    )


def notify_security_change(detail_text):
    """Security-sensitive account change: a password reset succeeded; ``detail_text`` is the audit-safe identity line."""
    if not _recipients():
        return False
    return _send(
        SECURITY_ALERT,
        {"detail": detail_text},
        "Security-sensitive account change",
    )


# Every verify_payment failure branch records one of these, so the spike is
# counted from the audit trail.
FAILURE_EVENT_TYPES = (
    "payment.signature_rejected",
    "payment.order_not_found",
    "payment.reference_mismatch",
    "payment.stock_conflict",
    "payment.coupon_invalid",
)


def check_payment_failure_spike(now=None):
    """Declared spike rule: at least PAYMENT_FAILURE_SPIKE_COUNT failed attempts in the trailing window fire one alert, cooldown-deduped.

    Reads the AuditEvent trail and never raises; returns True when an alert mail left.
    """
    from common.models import AuditEvent

    if not _recipients():
        return False
    now = now or timezone.now()
    window_start = now - timedelta(
        seconds=settings.PAYMENT_FAILURE_SPIKE_WINDOW_SECONDS
    )
    recent = AuditEvent.objects.filter(
        event_type__in=FAILURE_EVENT_TYPES,
        created_at__gte=window_start,
        created_at__lte=now,
    ).order_by("-created_at")
    return notify_payment_failure_spike(recent)


def notify_integration_outage(detail_text):
    """Integration outage: a /health/ check failed; the polled probe's cooldown protects the inbox, and recovery is not notified."""
    if not _recipients():
        return False
    return _send(
        INTEGRATION_OUTAGE,
        {"detail": detail_text},
        "Integration outage alert",
    )


def notify_background_job_failure(detail_text):
    """Background job failure: a queued notification was dead-lettered; the row's ``last_error`` is the diagnosis.

    Raised beside the dead-letter transition and log-only, so a drain pass cannot abort on the alert.
    """
    if not _recipients():
        return False
    return _send(
        BACKGROUND_JOB_FAILURE,
        {"detail": detail_text},
        "Background job failure alert",
    )

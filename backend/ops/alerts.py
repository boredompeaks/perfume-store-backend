"""Admin alert dispatch (SPEC-19-2, spec 19.2, alerting halves of
[R-19.15]/[R-19.20]/[R-19.21]/[R-19.27]).

Every alert is a plain-transactional email to the configured staff
recipients, sent through the SPEC-19-1 single send path
(``common.notifications.send_email`` — never a new send_mail call; the
whole point of R-19.0). The module owns the four alert types that have an
existing detection site; the order-lifecycle admin alerts (new order,
reconciliation, fulfilment) are deliberately absent — their content halves
are SPEC-1-12's (section-19.md owner attributions).

Recipients: ``settings.ALERT_RECIPIENTS`` (comma-separated emails from the
env, documented in .env.example). Empty/unset disables admin alerts
entirely — the store boots with no alert budget configured rather than
guessing a mailbox; the ``staff@…`` fallback an earlier design sketched was
rejected as a hardcoded address (conventions.md: no hardcoded emails).

Dedup: per alert-type cooldown (``settings.ALERT_COOLDOWN_SECONDS``,
default 300). The low-stock and out-of-stock triggers re-fire on every
staff dashboard load and every stock edit — without a cooldown an admin
with 40 near-stockout SKUs gets 40 identical mails per dashboard visit and
a payment-failure burst emails once per failed verify. The cache (default
LocMemCache; Redis once SPEC-2-03 wires it) records the last-sent time per
alert type; inside the window the alert is logged (INFO) instead of sent,
so suppression is observable and testable.

Send contract: identical to ``dispatch`` — a send failure is logged with
its traceback and swallowed (log-only). An SMTP outage must never fail the
business operation that tripped the alert (a checkout verifying, a staff
member loading the dashboard).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from common import notifications

# Dedicated channel name (mirrors common.notifications) so deployments can
# route alert output in log tooling independently.
logger = logging.getLogger("ops.alerts")

# Every alert template lives at templates/emails/<key>.txt; the key is also
# the cache-cooldown identity, so one name space serves both.
LOW_STOCK = "low_stock"
OUT_OF_STOCK = "out_of_stock"
PAYMENT_FAILURE_SPIKE = "payment_failure_spike"
SECURITY_ALERT = "security_alert"
INTEGRATION_OUTAGE = "integration_outage"


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

    Cache-based on purpose: per-process atomicity is enough for a
    mail-bomb bound, and a race that double-sends costs one duplicate mail,
    never a business outcome.

    Fail-open: a raisable cache backend must not kill the alert, so a
    cache outage degrades to "not suppressed" (the send is attempted,
    worst case un-deduped) with the failure logged — never a 500 in the
    dashboard/health flow that tripped the alert.
    """
    try:
        if cache.get(f"alerts:cooldown:{alert_key}") is not None:
            return True
        cache.set(
            f"alerts:cooldown:{alert_key}",
            True,
            timeout=settings.ALERT_COOLDOWN_SECONDS,
        )
    except Exception:
        logger.exception(
            "alert %s cooldown check failed; failing open", alert_key
        )
    return False


def _send(alert_key, context, subject):
    """One cooldown-checked send through the single email path.

    Returns True when a mail actually left (test/observability seam);
    suppressed and failed sends both return False.
    """
    sent = False
    try:
        # Fail-open cooldown: the suppression check runs inside the same
        # try as the send, so a raisable cache backend degrades to an
        # attempted (worst case un-deduped) mail, never a 500 in the
        # dashboard/health flow that tripped the alert.
        if _in_cooldown(alert_key):
            logger.info("alert %s suppressed (cooldown)", alert_key)
            return False
        for recipient in _recipients():
            notifications.send_email(
                f"alert_{alert_key}", context, subject, recipient
            )
            sent = True
    except Exception:
        # Log-only, like dispatch: the alert must never break the flow
        # that detected the problem.
        logger.exception("alert %s dispatch failed", alert_key)
    return sent


def _product_lines(rows):
    lines = []
    for row in rows:
        lines.append(
            f"- {row['name']} (id {row['id']}): {row['stock']} in stock"
        )
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
    """Low-stock breach: any product at 0 < stock <= LOW_STOCK_THRESHOLD.

    Trigger site: ops.services.get_health / the dashboard inventory scan —
    whichever notices the breach first. ``rows`` are the id/name/stock
    dicts already computed there; the alert adds nothing new to detect.
    """
    return _notify_stock_breach(
        LOW_STOCK,
        "Low stock alert",
        rows,
    )


def notify_out_of_stock(rows):
    """Out-of-stock breach: products at exactly 0 units — the harder edge
    of the same [R-19.15] inventory-attention half; every sale on an
    out-of-stock SKU is already lost revenue."""
    return _notify_stock_breach(
        OUT_OF_STOCK,
        "Out of stock alert",
        rows,
    )


def _failure_rows(recent):
    """Render the failed-payment AuditEvent rows for the alert body.

    The caller passes model instances (check_payment_failure_spike owns
    the queryset); attribute access is the one contract, so no dual
    dict/instance shape is supported.
    """
    return [
        "- {event_type} order {order_id} at {created_at:%Y-%m-%d %H:%M:%S}".format(
            event_type=row.event_type,
            order_id=row.order_id,
            created_at=row.created_at,
        )
        for row in recent
    ]


def notify_payment_failure_spike(recent):
    """Payment-failure spike: >= PAYMENT_FAILURE_SPIKE_COUNT failed payment
    attempts within the trailing PAYMENT_FAILURE_SPIKE_WINDOW_SECONDS
    (defaults: 3 in 300). Spec 19.2 names the alert class but not the
    number, so the rule is declared here and both knobs are env-driven.

    ``recent`` is the already-filtered AuditEvent queryset (the caller owns
    the window query — the trigger site is the verify-failure branch beside
    the payment.* AuditEvent hook); the alert counts and renders whatever
    crossed the threshold.
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
    """Security-sensitive account change: a password reset succeeded.

    Trigger site: beside the AUTH_PASSWORD_RESET AuditEvent hook (17-01's
    revocation flow) — the same population the reset email already reaches,
    catching the case where the owner's mailbox was the thing that was
    compromised. ``detail_text`` is the audit-safe identity line (the
    username — common/models.py's [R-17.32] decision already governs what
    may appear in audit/log output).
    """
    if not _recipients():
        return False
    return _send(
        SECURITY_ALERT,
        {"detail": detail_text},
        "Security-sensitive account change",
    )


# Failed-payment AuditEvent event types: every verify_payment failure
# branch records one of these beside its warning log (orders/views.py, the
# [R-7.20] trail). A signature rejection or a stock conflict is exactly the
# "repeated payment failures" signal spec 19.2 names — the audit trail is
# the detection site, so the spike is counted from it and orders/views.py
# stays untouched.
FAILURE_EVENT_TYPES = (
    "payment.signature_rejected",
    "payment.order_not_found",
    "payment.reference_mismatch",
    "payment.stock_conflict",
    "payment.coupon_invalid",
)


def check_payment_failure_spike(now=None):
    """Declared spike rule ([R-19.21] alerting half): at least
    PAYMENT_FAILURE_SPIKE_COUNT failed payment attempts within the trailing
    PAYMENT_FAILURE_SPIKE_WINDOW_SECONDS (defaults: 3 in 300) fire one
    spike alert; the per-type cooldown then silences repeats for the
    window — the spike condition stays true until the burst ages out, so
    without the cooldown one bad gateway hour would email every verify.

    Reads the AuditEvent trail (the single sanctioned detection surface);
    never raises. Returns True when an alert mail left.
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
    """Integration outage: a /health/ check failed (database unwritable,
    media store unusable) — the store is degraded until it recovers.

    Trigger site: ops.views.health when get_health() degrades. The
    /health/ probe is polled, so the same cooldown rule protects the
    operator's inbox; recovery notifications are not attempted (the poller
    that notices recovery is the next successful probe — alerting only on
    failure avoids a second dedup state machine).
    """
    if not _recipients():
        return False
    return _send(
        INTEGRATION_OUTAGE,
        {"detail": detail_text},
        "Integration outage alert",
    )

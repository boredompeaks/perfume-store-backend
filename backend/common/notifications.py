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
the account created. The flip side of the in-process substrate: the send
happens before the caller's transaction commits, so a later rollback in
the same block cannot recall an already-handed-off email — exactly the
window the SPEC-2-03 outbox closes.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from common.models import AuditEvent

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
        order.user.email,
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

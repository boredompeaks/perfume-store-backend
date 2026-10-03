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
Getting the send off the response path entirely needs a real worker, which is
ASYNC-2c, not here.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
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
    registry itself defer would silently convert every other hook site
    (back-in-stock, shipped/delivered, webhooks) and remove any caller's
    ability to send inside its own transaction. Those sites are converted
    one at a time in ASYNC-2b2/2b3.
    """
    transaction.on_commit(lambda: dispatch(event_type, context))

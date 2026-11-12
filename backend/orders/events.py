"""Per-transition side-effect contract: lifecycle transitions enqueue their
notification through the shared outbox seam inside the transition's own
transaction, so a rolled-back transition leaves no notification behind.

The map is keyed on ``AuditEvent.EventType`` members; no handler is
registered yet, so these enqueues write no outbox row and no customer
notification is actually delivered."""

import logging

from common import notifications
from common.models import AuditEvent

# A dedicated channel name so deployment log tooling can route side-effect
# failures independently (mirrors common.notifications).
logger = logging.getLogger("orders.events")

# Transition → notification event, keyed on AuditEvent.EventType members: a
# bare non-member string would match nothing and fail silently. "confirmed"
# is deliberately absent — verify_payment's order.paid enqueue owns it.
TRANSITION_EVENTS = {
    "shipped": AuditEvent.EventType.ORDER_SHIPPED,
    "delivered": AuditEvent.EventType.ORDER_DELIVERED,
    "cancelled": AuditEvent.EventType.ORDER_CANCELLED,
}


def notify_transition(order, from_status, to_status):
    """Queue the transition's notification inside the caller's transaction;
    never sends on this thread and never raises, so a failed outbox write
    cannot fail or roll back the transition."""
    event_type = TRANSITION_EVENTS.get(to_status)
    if event_type is None:
        return
    try:
        notifications.enqueue(event_type, {"order": order})
    except Exception:
        logger.exception(
            "Lifecycle notification enqueue failed: order %s %s->%s (%s)",
            order.pk,
            from_status,
            to_status,
            event_type,
        )

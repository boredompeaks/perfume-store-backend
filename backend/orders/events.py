"""[R-10.16] SPEC-10-05: the per-transition side-effect contract.

Spec 10.3 requires every transition to declare its side effects. The
minimal contract: the lifecycle transitions that carry a customer
notification (shipped, delivered, cancelled) fire ONE call into the shared
notifications seam — ``common.notifications.dispatch``, the same dispatch
verify_payment already uses for order.paid ([R-19.0]) — never a second
send path. The notifications subsystem itself (handlers, templates,
outbox) is SPEC-1-12/S19: until its handlers register against these
events, dispatch resolves no handler and returns, so the hook sites below
are the contract and the content arrives later.

Event names follow the AuditEvent.EventType dotted convention; when
SPEC-1-12/S19 adds the matching EventType members and registers handlers,
these hook sites start delivering unchanged.

Placement: every status writer calls ``notify_transition`` AFTER the
transition's save and its OrderStatusEvent append, INSIDE the same
``transaction.atomic()`` block. The in-transaction call is safe by
construction: ``dispatch`` never raises (handler failures are logged and
swallowed in common.notifications) and the wrapper below swallows anyway,
so a notification failure can never fail or roll back the transition it
follows. The known cost of the in-process substrate — a send happens
before commit, so a later rollback in the same block cannot recall it —
is documented and accepted in common/notifications.py (the SPEC-2-03
outbox closes that window). The hook fires exactly once per real
transition: skips and idempotent replays return before the writer's
notify site, the same cardinality as the audit trail.
"""

import logging

from common import notifications

# A dedicated channel name so deployment log tooling can route side-effect
# failures independently (mirrors common.notifications).
logger = logging.getLogger("orders.events")

# Transition → notification event. Only lifecycle moves that carry a
# customer notification are listed here; confirmed has deliberately no
# entry (verify_payment's own order.paid dispatch owns that notification).
TRANSITION_EVENTS = {
    "shipped": "order.shipped",
    "delivered": "order.delivered",
    "cancelled": "order.cancelled",
}


def notify_transition(order, from_status, to_status):
    """Fire the lifecycle notification for a just-written transition.

    Never raises — a notification failure is log-only and can never fail
    or roll back the transition it follows (pinned in tests by mocking
    dispatch to raise)."""
    event_type = TRANSITION_EVENTS.get(to_status)
    if event_type is None:
        return
    try:
        notifications.dispatch(event_type, {"order": order})
    except Exception:
        logger.exception(
            "Lifecycle notification failed: order %s %s->%s (%s)",
            order.pk,
            from_status,
            to_status,
            event_type,
        )

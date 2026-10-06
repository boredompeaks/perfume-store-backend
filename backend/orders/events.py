"""[R-10.16] SPEC-10-05: the per-transition side-effect contract.

Spec 10.3 requires every transition to declare its side effects. The
minimal contract: the lifecycle transitions that carry a customer
notification (shipped, delivered, cancelled) fire ONE call into the shared
notifications seam — ``common.notifications.dispatch``, the same dispatch
verify_payment already uses for order.paid ([R-19.0]) — never a second
send path.

**These three names are members of ``AuditEvent.EventType`` (ASYNC-2e).**
They used to be bare strings here, which is the defect class this hook
site now names deliberately: ``_EVENT_HANDLERS`` is keyed on the enum, so a
name outside it resolves no handler, is not a value the audit trail can
record, and was absorbed by the same DEBUG no-op as a benign
"no notification wired yet" — so three missing notifications reported
nothing. The values below are the enum members, and both facts that made
them dead (the key type and the log level that hid it) are pinned by
tests.

What is deliberately still absent: **no handler is registered for any of
these three.** Dispatching them therefore still sends no email, and
``dispatch`` logs it at DEBUG as the content gap it honestly is. The order
email content set is SPEC-1-12/SPEC-19-2's scope, not this file's, and
``common.notifications.events_without_handler()`` names the gap so it can
be closed deliberately rather than discovered. A reader of this file may
say the hook sites exist and fire once per transition; a reader may NOT say
a customer receives a shipped, delivered or cancelled notification.

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
from common.models import AuditEvent

# A dedicated channel name so deployment log tooling can route side-effect
# failures independently (mirrors common.notifications).
logger = logging.getLogger("orders.events")

# Transition → notification event. Only lifecycle moves that carry a
# customer notification are listed here; confirmed has deliberately no
# entry (verify_payment's own order.paid dispatch owns that notification).
#
# The values are AuditEvent.EventType MEMBERS, not bare strings, and that is
# the load-bearing choice: the registry is keyed on the enum, so a bare
# string that is not a member matches nothing and fails silently. Keying this
# map on the enum makes a name that is not a vocabulary member a test failure
# instead of a production no-op.
TRANSITION_EVENTS = {
    "shipped": AuditEvent.EventType.ORDER_SHIPPED,
    "delivered": AuditEvent.EventType.ORDER_DELIVERED,
    "cancelled": AuditEvent.EventType.ORDER_CANCELLED,
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

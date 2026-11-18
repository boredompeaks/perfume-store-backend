"""Order lifecycle state machine - the single source of truth.

Holds the status choices, transition tables and helpers for both the legacy
single status and the explicit payment/fulfilment dimensions, plus the
audit-trigger and webhook-event vocabularies. It must stay
dependency-free (no Django or model imports): the 0012 data migration imports
the legacy->dimensions mapping, and this module is imported by models, admin
and views alike."""

# ``Callable`` is a runtime import, not typing-only: it appears in the
# module-level annotation below, evaluated at import. ``TYPE_CHECKING`` gates
# the model reference so this module stays dependency-free.
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orders.models import Order

STATUS_CHOICES = [
    ("pending", "Pending"),
    ("confirmed", "Confirmed"),
    ("shipped", "Shipped"),
    ("delivered", "Delivered"),
    ("cancelled", "Cancelled"),
]

# Cancelling a *paid* order is deliberately impossible: the machine declares no
# edge into "cancelled" from confirmed/shipped/delivered. Its money comes back
# through the refund seam, which moves the payment dimension, not the status.
ALLOWED_TRANSITIONS = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"shipped"},
    "shipped": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}


def transition_allowed(old_status: str, new_status: str) -> bool:
    return new_status == old_status or new_status in ALLOWED_TRANSITIONS.get(
        old_status, set()
    )


# Only NAMES the candidate edge; transition_allowed is still the single gate, so
# a revoked machine edge 409s instead of silently widening the machine.
ADMIN_FULFILMENT_NEXT = {
    "pending": "confirmed",
    "confirmed": "shipped",
    "shipped": "delivered",
}

# The statuses a packing/shipping step can still be taken FROM. Derived from
# ADMIN_FULFILMENT_NEXT so the queue can never list a status the machine will
# not advance.
FULFILMENT_QUEUE_STATUSES = tuple(ADMIN_FULFILMENT_NEXT)

# Transition preconditions: what must be TRUE about the row before a legal
# move. Each entry is a callable receiving the Order and returning failure
# reasons (empty = met); the ORM-backed checks live in orders.models and
# register at import. Annotated because an empty {} gives mypy nothing to infer.
TRANSITION_PRECONDITIONS: dict[str, list[Callable[["Order"], list[str]]]] = {}


def register_transition_preconditions(status, *checks):
    """Attach precondition callables to a status.

    Appending, not replacing, is deliberate: the built-ins and the shipment
    checks compose."""
    TRANSITION_PRECONDITIONS.setdefault(status, []).extend(checks)


def precondition_failures(order, new_status):
    """Every unmet-precondition reason for moving ``order`` to ``new_status``.

    Writers call this beside transition_allowed: the machine gate says the edge
    exists, this says the row qualifies. Empty list = clear to proceed."""
    failures = []
    for check in TRANSITION_PRECONDITIONS.get(new_status, ()):
        failures.extend(check(order) or [])
    return failures


# The legacy single status conflates "did they pay" with "did we ship"; these
# dimensions separate the questions. Ambiguous values like "success" are
# deliberately absent.
PAYMENT_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("authorized", "Authorized"),
    ("captured", "Captured"),
    ("failed", "Failed"),
    ("partially_refunded", "Partially refunded"),
    ("refunded", "Refunded"),
]

FULFILMENT_STATUS_CHOICES = [
    ("unfulfilled", "Unfulfilled"),
    ("partially_fulfilled", "Partially fulfilled"),
    ("fulfilled", "Fulfilled"),
]

# COD needs a marker on the order so the machine can branch its preconditions;
# prepaid is the store's default behavior.
PAYMENT_METHOD_PREPAID = "prepaid"
PAYMENT_METHOD_COD = "cod"

PAYMENT_METHOD_CHOICES = [
    (PAYMENT_METHOD_PREPAID, "Prepaid"),
    (PAYMENT_METHOD_COD, "Cash on delivery"),
]

# The payment dimension moves only along a declared edge, mirroring
# transition_allowed (self-transitions stay legal for idempotent replays).
# failed -> captured is the RETRY edge: a failed verification leaves the order
# status untouched, so the next successful verify captures normally.
PAYMENT_ALLOWED_TRANSITIONS = {
    "pending": {"captured", "failed"},
    "authorized": {"captured"},
    "captured": {"partially_refunded", "refunded"},
    "partially_refunded": {"refunded"},
    "failed": {"captured"},
    "refunded": set(),
}


def payment_transition_allowed(old_payment: str, new_payment: str) -> bool:
    return new_payment == old_payment or new_payment in PAYMENT_ALLOWED_TRANSITIONS.get(
        old_payment, set()
    )


# Every legacy status maps to BOTH dimensions; the 0012 data migration
# backfills from this, so a missing key would leave live rows un-backfilled.
LEGACY_STATUS_DIMENSIONS = {
    "pending": ("pending", "unfulfilled"),
    "confirmed": ("captured", "unfulfilled"),
    "shipped": ("captured", "fulfilled"),
    "delivered": ("captured", "fulfilled"),
    "cancelled": ("pending", "unfulfilled"),
}


def payment_for_status(status: str) -> str:
    """The payment-dimension value implied by a legacy single status."""
    return LEGACY_STATUS_DIMENSIONS[status][0]


def fulfilment_for_status(status: str) -> str:
    """The fulfilment-dimension value implied by a legacy single status."""
    return LEGACY_STATUS_DIMENSIONS[status][1]


# The payment value a capture writes, named here so both capture writers ask
# the machine rather than restating it.
PAYMENT_CAPTURED = "captured"


def _reachable_payment_values(root: str) -> set:
    """Every payment value reachable from ``root`` along declared edges.

    Breadth-first over ``PAYMENT_ALLOWED_TRANSITIONS``, so the answer is
    whatever the machine says rather than whatever a caller listed.
    """
    seen, frontier = {root}, [root]
    while frontier:
        for target in PAYMENT_ALLOWED_TRANSITIONS.get(frontier.pop(), set()):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


# Payment values meaning the customer has paid and the store still holds some
# money: the closure of PAYMENT_ALLOWED_TRANSITIONS from captured, less the
# values with no outgoing edge (which excludes "refunded"). DERIVED, NEVER
# LISTED - a literal here is what once let the returns gate fall behind the
# machine.
CAPTURED_MONEY_PAYMENT_STATUSES = frozenset(
    value
    for value in _reachable_payment_values(PAYMENT_CAPTURED)
    if PAYMENT_ALLOWED_TRANSITIONS.get(value)
)

# The values that prove a CAPTURE already happened: the same closure WITHOUT
# the sink filter, so "refunded" counts - the sale was committed and then
# unwound. Dropping it would let the inventory guard treat a refunded order as
# never sold and decrement its stock a second time.
CAPTURED_SALE_PAYMENT_STATUSES = frozenset(_reachable_payment_values(PAYMENT_CAPTURED))

# status_for_payment needs a progression, not a set: one payment value spans
# several statuses, and the answer is the EARLIEST one it implies.
LIFECYCLE_SEQUENCE = ("pending", "confirmed", "shipped", "delivered", "cancelled")


def status_for_payment(payment_status: str) -> str | None:
    """The earliest lifecycle status whose payment dimension is ``payment_status``.

    The inverse of :func:`payment_for_status`, so a writer never names a status
    literal beside a payment value. ``None`` when no status implies that payment
    (the refund-dimension values), which callers must handle rather than assume
    a mapping exists.
    """
    for status in LIFECYCLE_SEQUENCE:
        if LEGACY_STATUS_DIMENSIONS.get(status, ("", ""))[0] == payment_status:
            return status
    return None


# Every legal transition appends an OrderStatusEvent naming the surface that
# performed it. The trigger vocabulary lives here beside the machine it audits:
# a new writer must pick one of these, so the trail cannot grow unregistered
# sources.
TRIGGER_ORDER_CREATE = "order_create"
TRIGGER_ADMIN_CHANGE_FORM = "admin_change_form"
TRIGGER_ADMIN_BULK_ACTION = "admin_bulk_action"
TRIGGER_PAYMENT_VERIFY = "payment_verify"
TRIGGER_ADMIN_API_FULFIL = "admin_api_fulfil"
TRIGGER_ADMIN_API_CANCEL = "admin_api_cancel"
# The failed-verify writer records from == to ('pending'), since the order
# status does not move on a failed attempt; the trigger names the failure.
TRIGGER_PAYMENT_FAILED = "payment_failed"
# Its own trigger, not TRIGGER_PAYMENT_VERIFY: the trail must name WHICH
# surface performed the transition.
TRIGGER_PAYMENT_WEBHOOK = "payment_webhook"

STATUS_EVENT_TRIGGERS = [
    (TRIGGER_ORDER_CREATE, "Order created"),
    (TRIGGER_ADMIN_CHANGE_FORM, "Admin change form"),
    (TRIGGER_ADMIN_BULK_ACTION, "Admin bulk action"),
    (TRIGGER_PAYMENT_VERIFY, "Payment verified"),
    (TRIGGER_ADMIN_API_FULFIL, "Admin fulfilment API"),
    (TRIGGER_ADMIN_API_CANCEL, "Admin cancel API"),
    (TRIGGER_PAYMENT_FAILED, "Payment verification failed"),
    (TRIGGER_PAYMENT_WEBHOOK, "Payment webhook"),
]

# The names the payment gateway sends in its event header. An event outside
# this vocabulary is still RECORDED (the trail must never discard a delivery the
# provider says happened) - it simply moves nothing.
WEBHOOK_EVENT_CAPTURED = "payment.captured"
WEBHOOK_EVENT_AUTHORIZED = "payment.authorized"
WEBHOOK_EVENT_FAILED = "payment.failed"
WEBHOOK_EVENT_REFUNDED = "payment.refunded"
# Both spellings are the same fact: Razorpay's settled-refund event is
# "refund.processed" while "payment.refunded" is the payment-scoped name it
# documents, and accepting either means a provider rename cannot stop the trail.
WEBHOOK_EVENT_REFUND_DONE = "refund.processed"
WEBHOOK_REFUND_EVENTS = frozenset({WEBHOOK_EVENT_REFUNDED, WEBHOOK_EVENT_REFUND_DONE})

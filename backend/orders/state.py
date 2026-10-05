"""Order lifecycle state machine — the single source of truth.

[R-10.1] SPEC-10-01a: the order machine's status choices, transition table,
allowed-from sets and helpers used to live scattered across models.py (the
status choices), admin.py (ALLOWED_TRANSITIONS / transition_allowed) and
views.py (ADMIN_FULFILMENT_NEXT). They are consolidated here so a machine
edge can only ever be changed in one place.

This module must stay dependency-free (no Django or model imports): the
0012 data migration imports the legacy→dimensions mapping, and state.py is
imported by models, admin and views alike, so anything heavier here would
risk import cycles or break migration-time imports.

[R-10.1 DEVIATES] The legacy single ``status`` column remains the compat
surface (frontend, admin filters, existing tests all read it); the two
explicit dimensions below (spec 10.2) are additive and kept in sync by the
writers. No consumer is forced onto the new fields in this batch.
"""

# Typing imports (TIER-2). `Callable` is a RUNTIME import, not a typing-only
# one, because it appears in the module-level annotation on
# TRANSITION_PRECONDITIONS below and Python evaluates that annotation when the
# module is loaded. `TYPE_CHECKING` gates the model reference, and that one IS
# typing-only: this module promises to stay dependency-free (the 0012 data
# migration imports from here), so a real import of orders.models would risk an
# import cycle at migration time. A string annotation around the name means
# nothing is resolved when this module is loaded.
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import guard, never executed at runtime
    from orders.models import Order

# ——— legacy single status (compat surface) ———————————————————————————

STATUS_CHOICES = [
    ("pending", "Pending"),
    ("confirmed", "Confirmed"),
    ("shipped", "Shipped"),
    ("delivered", "Delivered"),
    ("cancelled", "Cancelled"),
]

# Legal status flow. Cancelling a *paid* order is deliberately impossible —
# the machine declares no edge into "cancelled" from confirmed/shipped/
# delivered. Its money comes back through the SPEC-1-05 refund seam instead,
# which records a Refund and moves the payment dimension without moving the
# order's status.
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


# The admin fulfilment step map (SPEC-9-07) only NAMES the candidate edge;
# the state machine (transition_allowed) is still the single gate — if a
# machine edge is ever revoked, the fulfilment endpoint 409s on it instead
# of silently widening the machine.
ADMIN_FULFILMENT_NEXT = {
    "pending": "confirmed",
    "confirmed": "shipped",
    "shipped": "delivered",
}

# The fulfilment queue: the statuses a packing/shipping step can still be
# taken FROM, i.e. exactly the orders that await a packer. Derived from
# ADMIN_FULFILMENT_NEXT rather than spelled out, so the queue can never list a
# status the machine will not advance (or miss one it will) — the admin
# listing that shows it and the API seam that advances it read the same
# constant.
FULFILMENT_QUEUE_STATUSES = tuple(ADMIN_FULFILMENT_NEXT)

# ——— [R-10.19]/[R-10.14] SPEC-10-03: transition preconditions ———————————
# ALLOWED_TRANSITIONS says WHICH moves are legal; preconditions say what
# must be TRUE about the row before the move (spec 10.3 lists
# "Preconditions" beside allowed source/destination for every transition).
# EXTENSION HOOK for SPEC-1-08 (shipment checks): each entry is a callable
# receiving the Order instance and returning a list of human-readable
# failure reasons (empty list = precondition met); register more checks
# for "shipped" — or any status — via register_transition_preconditions.
# This module stays dependency-free (see the module docstring), so the
# built-in ORM-backed checks live in orders.models and register
# themselves at import (models is imported by every writer surface, so
# the registry is populated before any writer runs). precondition_failures
# is the ONLY evaluation point — writers must never special-case a check.
# Annotated rather than left bare (TIER-2): the registry is a mapping from a
# status to the list of callables registered against it, and an empty `{}` is
# precisely the case mypy cannot infer -- it has no value to read the type
# from. `Callable[[Order], list[str]]` is the contract the docstring above
# already states (callables receiving the Order, returning failure reasons),
# and the `Order` reference is a string because this module stays
# dependency-free (see the module docstring) and must not import the model.
TRANSITION_PRECONDITIONS: dict[str, list[Callable[["Order"], list[str]]]] = {}


def register_transition_preconditions(status, *checks):
    """SPEC-1-08 extension point: attach precondition callables to a
    status. Appending (not replacing) is deliberate: the built-ins and the
    shipment section's checks compose — a row must satisfy all of them."""
    TRANSITION_PRECONDITIONS.setdefault(status, []).extend(checks)


def precondition_failures(order, new_status):
    """Every unmet-precondition reason for moving ``order`` to
    ``new_status``. Writers call this beside transition_allowed: the
    machine gate says the edge exists, this says the row qualifies for
    it. Empty list = clear to proceed."""
    failures = []
    for check in TRANSITION_PRECONDITIONS.get(new_status, ()):
        failures.extend(check(order) or [])
    return failures


# ——— [R-10.1] explicit lifecycle dimensions (spec 10.2) —————————————————
# The legacy single status conflates "did they pay" with "did we ship"; the
# two dimensions below separate those questions. The spec's example states
# are the canonical sets: ambiguous values like "success" or "done" are
# deliberately absent (spec 10.2's own warning). The failure writer is
# SPEC-10-04's; partially_refunded / refunded are written by the SPEC-1-05
# refund seam; authorized has no writer yet.
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

# ——— [R-10.2] SPEC-10-04: payment-method vocabulary —————————————————————
# Spec 10.1 makes COD orders and failed payments an explicit machine
# responsibility. COD needs a payment-method marker on the order so the
# machine can branch its preconditions; prepaid is the store's current and
# default behavior (gateway capture before fulfilment). The checkout INPUT
# (accepting COD as a choice) is a checkout-section row — this is only the
# machine-side vocabulary the row carries.
PAYMENT_METHOD_PREPAID = "prepaid"
PAYMENT_METHOD_COD = "cod"

PAYMENT_METHOD_CHOICES = [
    (PAYMENT_METHOD_PREPAID, "Prepaid"),
    (PAYMENT_METHOD_COD, "Cash on delivery"),
]

# ——— [R-10.4] SPEC-10-04: payment-dimension transition table ————————————
# ALLOWED_TRANSITIONS gates the order status; the payment dimension (spec
# 10.2) gets the same treatment so a payment value can only ever move
# along a declared edge. The failure/retry semantics live here: the
# failure writer moves pending -> failed, and failed -> captured is the
# RETRY edge — a failed verification leaves the order status untouched
# (retryable by design), so the next successful verify captures normally.
# No writer exists yet for authorized (the gateway two-step capture,
# SPEC-10-04's remaining payment work); its edge is declared so the machine
# is complete and that work pins against a table that already answers it.
# partially_refunded / refunded are written by the SPEC-1-05 refund seam.
# Mirrors transition_allowed (self-transitions stay legal: idempotent
# replays).
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


# Total legacy→dimensions mapping: EVERY legacy status value maps to BOTH
# dimensions — this is what the 0012 data migration backfills from, so a
# key missing here would leave a live row with un-backfilled dimensions.
# Reading: a pending order has not paid and nothing shipped; "confirmed"
# means the payment was captured but fulfilment has not started; shipped
# and delivered both mean the items went out (legacy status cannot express
# partial fulfilment); a cancelled order was never paid (cancel is legal
# only from pending) and nothing shipped.
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


# [R-10.4] The payment value a capture writes. Named here because the machine
# is the single source for what a capture MEANS: verify_payment (the customer
# callback) and the SPEC-1-06 webhook reconciler both write this value and both
# ask this module whether the row may reach it, so neither restates it.
PAYMENT_CAPTURED = "captured"


def _reachable_payment_values(root: str) -> set:
    """Every payment value reachable from ``root`` along declared edges.

    Follows ``PAYMENT_ALLOWED_TRANSITIONS`` breadth-first, so the answer is
    whatever the machine says rather than whatever a caller listed. Used by
    :data:`CAPTURED_MONEY_PAYMENT_STATUSES`, which is why it is private.
    """
    seen, frontier = {root}, [root]
    while frontier:
        for target in PAYMENT_ALLOWED_TRANSITIONS.get(frontier.pop(), set()):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


# [R-1.14] SPEC-1-05: the payment values that mean THE CUSTOMER HAS PAID and
# the store still holds some of that money - the capture point itself plus
# everything the machine places downstream of it that it can still move a
# refund out of.
#
# DERIVED, NEVER LISTED, and the derivation is the whole point. It is the
# closure of ``PAYMENT_ALLOWED_TRANSITIONS`` from ``PAYMENT_CAPTURED``, less the
# values with no declared outgoing edge. Each half is the machine's own
# evidence rather than a judgment:
#
# * the closure is what admits ``partially_refunded``. The table places it
#   STRICTLY AFTER ``captured``, orders/models.py's shipped-edge precondition
#   admits it ALONGSIDE ``captured`` as "real money too - a capture minus a
#   recorded refund", and the SPEC-1-05 refund seam is the writer that puts an
#   order there. A customer who has had part of their money back still has
#   money in the transaction, so they still have something to be RETURNING.
# * the no-outgoing-edge filter is what excludes ``refunded``: a value the
#   machine declares no edge out of is a value whose money has all gone back,
#   and an order in that state has nothing left to send anything against.
#
# Restating either half as a literal here is what let the cycle-3 audit find
# the returns gate tracking only ``captured`` while the machine tracked two
# values; a reader that walks the table instead cannot fall behind it.
CAPTURED_MONEY_PAYMENT_STATUSES = frozenset(
    value
    for value in _reachable_payment_values(PAYMENT_CAPTURED)
    if PAYMENT_ALLOWED_TRANSITIONS.get(value)
)

# The lifecycle order the dimension mapping above is read in. status_for_payment
# needs a progression, not a set: one payment value spans several statuses
# (captured covers confirmed/shipped/delivered) and the answer to "which status
# does a capture put the order in?" is the EARLIEST one it implies.
LIFECYCLE_SEQUENCE = ("pending", "confirmed", "shipped", "delivered", "cancelled")


def status_for_payment(payment_status: str) -> str | None:
    """The earliest lifecycle status whose payment dimension is ``payment_status``.

    The inverse of :func:`payment_for_status`, and the reason a writer never
    has to name a status literal beside a payment value. ``None`` when no
    status implies that payment — which is the honest answer for the
    refund-dimension values, states the legacy single status cannot express
    (the documented [R-10.1] divergence), so a caller must handle it rather
    than assume a mapping exists.

    The return annotation is ``str | None`` and it was ``str`` until mypy was
    pointed at this module (TIER-2). The annotation was the defect, not the
    body: ``None`` is a real, intended and TESTED return here — the pins in
    ``orders.tests_webhooks`` assert ``assertIsNone`` for both refund-dimension
    and unmapped values — and ``orders.webhooks`` branches on it. A ``-> str``
    signature on a function that returns ``None`` tells every future caller,
    and every type checker reading this module, that the None branch cannot
    happen. ``str | None`` says what the function actually does.
    """
    for status in LIFECYCLE_SEQUENCE:
        if LEGACY_STATUS_DIMENSIONS.get(status, ("", ""))[0] == payment_status:
            return status
    return None


# ——— [R-10.12]/[R-10.17] SPEC-10-02: transition-audit triggers ——————————
# Every legal status transition appends an OrderStatusEvent row naming the
# surface that performed it. The trigger vocabulary lives here beside the
# machine it audits (single source): a new writer must pick a trigger from
# this list, so the audit trail cannot grow unregistered sources. Spec 10.3
# prescribes "Actor or triggering event" per transition — the pair below is
# how each event answers it (who did it, or what fired it).
TRIGGER_ORDER_CREATE = "order_create"
TRIGGER_ADMIN_CHANGE_FORM = "admin_change_form"
TRIGGER_ADMIN_BULK_ACTION = "admin_bulk_action"
TRIGGER_PAYMENT_VERIFY = "payment_verify"
TRIGGER_ADMIN_API_FULFIL = "admin_api_fulfil"
TRIGGER_ADMIN_API_CANCEL = "admin_api_cancel"
# [R-10.4] SPEC-10-04: the failed-verify writer appends its audit row with
# this trigger. The ORDER status does not move on a failed attempt (it
# stays retryable), so the row records from == to ('pending') and the
# trigger is what names the failure — spec 10.3's "Failure/retry
# behaviour" answer for this edge.
TRIGGER_PAYMENT_FAILED = "payment_failed"
# [R-1.15] SPEC-1-06: the gateway told us the money arrived, so the same
# capture edge the customer callback drives has a second, server-to-server
# source. It is its own trigger (not TRIGGER_PAYMENT_VERIFY) because spec
# 10.3 asks the trail to name WHICH surface performed the transition: a
# reconciliation from the provider's own record and a customer-initiated
# verify are different facts about the same edge.
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

# ——— [R-1.15] SPEC-1-06: the provider's webhook event vocabulary ————————
# The names the payment gateway sends in its event header. They live here,
# beside the machine they feed, for the same reason STATUS_EVENT_TRIGGERS
# does: the handler dispatches on constants, so no string literal can drift
# away from the vocabulary the rest of the file declares. An event outside
# this vocabulary is still RECORDED (the reconciliation trail must never
# discard a delivery the provider says happened) — it simply moves nothing.
WEBHOOK_EVENT_CAPTURED = "payment.captured"
WEBHOOK_EVENT_AUTHORIZED = "payment.authorized"
WEBHOOK_EVENT_FAILED = "payment.failed"
WEBHOOK_EVENT_REFUNDED = "payment.refunded"
# A settled refund. Both spellings are the same fact: Razorpay's own event name
# for a settled refund is ``refund.processed`` while ``payment.refunded`` is the
# payment-scoped name the same provider documents for it, and accepting either
# means a rename on the provider's side cannot silently stop the trail. The
# constant is named for what it means (the refund is done) because its literal is
# fixed: only the provider gets to spell that event.
WEBHOOK_EVENT_REFUND_DONE = "refund.processed"
WEBHOOK_REFUND_EVENTS = frozenset({WEBHOOK_EVENT_REFUNDED, WEBHOOK_EVENT_REFUND_DONE})

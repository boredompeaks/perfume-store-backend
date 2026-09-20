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

# ——— legacy single status (compat surface) ———————————————————————————

STATUS_CHOICES = [
    ("pending", "Pending"),
    ("confirmed", "Confirmed"),
    ("shipped", "Shipped"),
    ("delivered", "Delivered"),
    ("cancelled", "Cancelled"),
]

# Legal status flow. Cancelling a *paid* order is deliberately impossible —
# there is no refund flow yet (V-03); reconciliation is manual by design.
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

# ——— [R-10.1] explicit lifecycle dimensions (spec 10.2) —————————————————
# The legacy single status conflates "did they pay" with "did we ship"; the
# two dimensions below separate those questions. The spec's example states
# are the canonical sets: ambiguous values like "success" or "done" are
# deliberately absent (spec 10.2's own warning). Refund/failure/COD writers
# are SPEC-10-04's scope — the values exist here so the machine is complete,
# but nothing writes them yet.
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

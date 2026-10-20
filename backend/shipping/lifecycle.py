"""Advance a shipment's order to the lifecycle its parcels imply (SPEC-D-01).

A parcel and an order are two records of two facts, and the customer reads the
order. Before this module a status move was two manual steps - move the parcel,
then remember to move the order - so an order could sit in "confirmed" while
its parcel was on a truck, and the storefront showed the wrong state.

This is the seam between them, and it is deliberately a READER of
``orders.state`` rather than a second machine: ``transition_allowed`` is the
only authority on which edges exist and ``precondition_failures`` on whether
this row qualifies for one. A shipment event therefore cannot force a move the
order machine refuses, which is why a pending (unpaid) order is never
auto-shipped and a cancelled one stays cancelled. Cancellation and returns are
separate actions with their own writers; nothing here starts one.

The caller is ``ShipmentAdmin.save_model``, the only writer of a shipment
status move, and it calls into here inside its own transaction so the parcel
move, its tracking event and the order move commit or roll back together.

The order event it writes names the surface that performed the move, not the
shipment that prompted it: ``admin_change_form`` is the registered trigger for
an edit made through the admin change form, and that vocabulary is an exact-set
contract pinned to its migration-recorded spellings, so a shipment-specific
trigger is a vocabulary change with its own migration rather than a new string
here. The conflation is bounded on both sides - a tracking-number-only edit
reaches this module on every ``save_model`` but writes no order event while the
order already sits at the status its parcel implies, and a move that writes an
order event always wrote its ``ShipmentEvent`` first - but a reader of the
order trail cannot recover "this came from the shipment screen" from the row
today.
"""

from django.db import transaction

from orders.events import notify_transition
from orders.models import Order, OrderStatusEvent
from orders.state import (
    TRIGGER_ADMIN_CHANGE_FORM,
    fulfilment_for_status,
    precondition_failures,
    transition_allowed,
)

from .models import Shipment

# The shipment states that mean the order's items moved, and the order status
# each one implies. "label_created" (the parcel has not left) and "exception"
# (a delivery failure) are deliberately absent: neither is a lifecycle move
# the order machine can express, and listing them would invite a caller to
# treat a failure as a state.
ORDER_STATUS_FOR_SHIPMENT = {
    Shipment.Status.DISPATCHED: "shipped",
    Shipment.Status.IN_TRANSIT: "shipped",
    Shipment.Status.DELIVERED: "delivered",
}

# The order status that means the WHOLE order arrived. Derived from the map
# above rather than restated, so the completeness gate below cannot drift from
# the target it guards.
ORDER_DELIVERED = ORDER_STATUS_FOR_SHIPMENT[Shipment.Status.DELIVERED]


def _every_parcel_delivered(order_id):
    """Whether ``order_id`` has parcels and every one of them has arrived.

    The order's parcels are the authority on delivery, not the one row being
    saved: a split order is delivered when its LAST parcel arrives, and a
    sibling still in the warehouse - or one whose delivery failed, since the
    customer does not have it either - means the order has not arrived. The
    read rides the caller's transaction, whose ``select_for_update`` on the
    Order row serialises two siblings reaching "delivered" at once, so the
    second writer sees the first writer's parcel already at its new status.
    """
    statuses = list(
        Shipment.objects.filter(order_id=order_id).values_list("status", flat=True)
    )
    return bool(statuses) and all(
        status == Shipment.Status.DELIVERED for status in statuses
    )


def sync_order_status(shipment, *, actor=None):
    """Mirror ``shipment``'s status onto its order where the machine allows it.

    Returns the status the order moved to, or ``None`` when this shipment
    status changes nothing, the order is already there, or the machine refuses
    the move. Every refusal is silent by design: a parcel may legitimately be
    marked delivered before its order was ever paid for, and the honest answer
    to that is to leave the order alone rather than to fail the operator's
    save with an error they cannot act on.

    "Delivered" is a claim about the WHOLE order, so it is the one target that
    is also gated on the order's other parcels: a split order advances only
    when every one of its parcels has arrived, and the order stays at
    "shipped" until then. A single-parcel order is unaffected because its one
    parcel is the one being saved.

    The order is read under ``select_for_update()`` and the target compared to
    the live status before anything is written, so this is idempotent: a replay
    of the same move, or a second parcel reaching the same stage, writes no
    second ``OrderStatusEvent``. The ``atomic`` block is a savepoint inside the
    caller's transaction rather than a transaction of its own, so it also makes
    the lock meaningful for a caller that reaches here outside the admin.
    """
    target = ORDER_STATUS_FOR_SHIPMENT.get(shipment.status)
    if target is None:
        return None

    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=shipment.order_id)
        if order.status == target:
            return None
        if not transition_allowed(order.status, target):
            return None
        if precondition_failures(order, target):
            return None
        if target == ORDER_DELIVERED and not _every_parcel_delivered(order.pk):
            return None

        previous = order.status
        order.status = target
        # The fulfilment dimension rides every legal status change
        # (orders.state.LEGACY_STATUS_DIMENSIONS is the mapping); a writer
        # that moved only the status would leave the two out of step.
        order.fulfilment_status = fulfilment_for_status(target)
        # updated_at rides update_fields explicitly, on orders/admin.py's bulk
        # writer's reasoning: an auto_now column is skipped unless it is listed,
        # and a transition is a write to the row, so a "changed since" query
        # must see this move rather than the row's creation time.
        order.save(update_fields=["status", "fulfilment_status", "updated_at"])
        OrderStatusEvent.objects.create(
            order=order,
            from_status=previous,
            to_status=target,
            actor=actor,
            trigger=TRIGGER_ADMIN_CHANGE_FORM,
        )
        notify_transition(order, previous, target)
    return target

"""Advance a shipment's order to the lifecycle its parcels imply.

This is a reader of ``orders.state`` rather than a second machine, so a
shipment event cannot force a move the order machine refuses; the caller is
``ShipmentAdmin.save_model``, inside the transaction that commits parcel,
event and order together.
"""

from dataclasses import dataclass

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

# label_created and exception are deliberately absent: neither is a lifecycle
# move the order machine can express.
ORDER_STATUS_FOR_SHIPMENT = {
    Shipment.Status.DISPATCHED: "shipped",
    Shipment.Status.IN_TRANSIT: "shipped",
    Shipment.Status.DELIVERED: "delivered",
}

# Derived from the map above so the completeness gate cannot drift from it.
ORDER_DELIVERED = ORDER_STATUS_FOR_SHIPMENT[Shipment.Status.DELIVERED]


@dataclass(frozen=True)
class OrderSyncResult:
    """What :func:`sync_order_status` did, and - when it did nothing - why.

    Both fields are ``None`` when the parcel implies no move or the order is
    already there, so callers can tell "nothing to do" from "refused"; the
    reason is built beside the gates that produced it.
    """

    moved_to: str | None = None
    refusal: str | None = None


def _every_parcel_delivered(order_id):
    """Whether ``order_id`` has parcels and every one of them has arrived.

    The order's parcels are the authority, not the row being saved; the
    caller's ``select_for_update`` on the Order serialises siblings reaching
    "delivered".
    """
    statuses = list(
        Shipment.objects.filter(order_id=order_id).values_list("status", flat=True)
    )
    return bool(statuses) and all(
        status == Shipment.Status.DELIVERED for status in statuses
    )


def sync_order_status(shipment, *, actor=None):
    """Mirror ``shipment``'s status onto its order where the machine allows it.

    Refusals are non-fatal and name the rule that held; "delivered" is gated on
    every parcel having arrived, and the order is locked and compared to the
    live status so replays write no second ``OrderStatusEvent``.
    """
    target = ORDER_STATUS_FOR_SHIPMENT.get(shipment.status)
    if target is None:
        return OrderSyncResult()

    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=shipment.order_id)
        if order.status == target:
            return OrderSyncResult()
        if not transition_allowed(order.status, target):
            return OrderSyncResult(
                refusal=f"{order.status} -> {target} is not a legal transition"
            )
        failures = precondition_failures(order, target)
        if failures:
            return OrderSyncResult(refusal="; ".join(failures))
        if target == ORDER_DELIVERED and not _every_parcel_delivered(order.pk):
            return OrderSyncResult(refusal="not every parcel has been delivered")

        previous = order.status
        order.status = target
        # The fulfilment dimension rides every legal status change; moving
        # status alone would leave the two out of step.
        order.fulfilment_status = fulfilment_for_status(target)
        # ``updated_at`` must be listed explicitly: ``auto_now`` is skipped on
        # ``update_fields``, and a transition is a write.
        order.save(update_fields=["status", "fulfilment_status", "updated_at"])
        OrderStatusEvent.objects.create(
            order=order,
            from_status=previous,
            to_status=target,
            actor=actor,
            trigger=TRIGGER_ADMIN_CHANGE_FORM,
        )
        notify_transition(order, previous, target)
    return OrderSyncResult(moved_to=target)

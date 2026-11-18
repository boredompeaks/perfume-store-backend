"""Staff-facing order seam: the unscoped order book plus the two lifecycle
writers staff drive directly (fulfil one step, cancel). State changes go
through ``orders.state``, and the page-size contract is shared with
``order.py``.
"""

from django.contrib.admin.models import CHANGE
from django.core.paginator import Paginator
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from common.audit import log_api_action
from common.permissions import (
    HasOrdersCancel,
    HasOrdersFulfill,
    HasOrdersRead,
    user_has_capability,
)

# A cancelled checkout releases its holds in the same transaction.
from products.models import StockReservation

# Per-transition side effects, shared with the payment writer.
from ..events import notify_transition
from ..models import Order, OrderStatusEvent
from ..serializers import OrderSerializer

# Order machine (transition table, gate, step map) lives in orders.state.
from ..state import (
    ADMIN_FULFILMENT_NEXT,
    ALLOWED_TRANSITIONS,
    FULFILMENT_QUEUE_STATUSES,
    TRIGGER_ADMIN_API_CANCEL,
    TRIGGER_ADMIN_API_FULFIL,
    fulfilment_for_status,
    precondition_failures,
    transition_allowed,
)

# Same page-number envelope and page-size cap as the customer history.
from .order import HISTORY_PAGE_SIZE_QUERY_PARAM, _history_page_size


@api_view(["GET"])
@permission_classes([HasOrdersRead])
def admin_order_list(request):
    """GET /api/admin/orders/ — every order, staff eyes only: ``orders.read``
    is the gate, so the queryset is unscoped unlike the customer list."""
    orders = Order.objects.select_related("coupon").order_by("-created_at", "-id")

    paginator = Paginator(
        orders,
        _history_page_size(request.query_params.get(HISTORY_PAGE_SIZE_QUERY_PARAM)),
    )
    page = paginator.get_page(request.query_params.get("page", 1))

    serializer = OrderSerializer(page.object_list, many=True)
    return Response(
        {
            "count": paginator.count,
            "total_pages": paginator.num_pages,
            "current_page": page.number,
            "next_page": page.has_next(),
            "previous_page": page.has_previous(),
            "results": serializer.data,
        }
    )


@api_view(["GET"])
@permission_classes([HasOrdersRead])
def admin_order_detail(request, order_id):
    """GET /api/admin/orders/:id/ — one order, staff eyes only. Unlike the
    customer detail there is no ownership scoping, so an unknown id is a
    plain uniform 404."""
    try:
        order = Order.objects.select_related("coupon").get(id=order_id)
    except Order.DoesNotExist:
        return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

    return Response(OrderSerializer(order).data)


def _may_fulfil(user, order):
    """Whether ``user`` may advance ``order`` one fulfilment step. There are two
    authorities: ``orders.read`` (or superuser) is unscoped, while a caller
    holding only ``orders.fulfill`` is scoped to the fulfilment queue."""
    if user.is_superuser or user_has_capability(user, "orders.read"):
        return True
    return user_has_capability(user, "orders.fulfill") and (
        order.status in FULFILMENT_QUEUE_STATUSES
    )


@api_view(["POST"])
@permission_classes([HasOrdersFulfill])
def admin_order_fulfill(request, order_id):
    """POST /api/admin/orders/:id/fulfill — advance one step under the row
    lock, so concurrent calls cannot skip a step; deliberately not idempotent.
    Callers holding only ``orders.fulfill`` are scoped to the fulfilment
    queue, and an out-of-queue order gets the same 404 as an unknown id."""
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(id=order_id)
        except Order.DoesNotExist:
            return Response(
                {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if not _may_fulfil(request.user, order):
            return Response(
                {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
            )

        target = ADMIN_FULFILMENT_NEXT.get(order.status)
        if target is None or not transition_allowed(order.status, target):
            allowed = ", ".join(sorted(ALLOWED_TRANSITIONS.get(order.status, set())))
            return Response(
                {
                    "error": f"Order cannot be fulfilled from status '{order.status}'",
                    "allowed": allowed,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Shipped preconditions gate here too; the envelope exposes the
        # reasons at ``details.preconditions``.
        precondition_reasons = precondition_failures(order, target)
        if precondition_reasons:
            return Response(
                {
                    "error": f"Order cannot be fulfilled from status '{order.status}'",
                    "preconditions": precondition_reasons,
                },
                status=status.HTTP_409_CONFLICT,
            )

        previous_status = order.status
        order.status = target
        # Fulfilment dimension rides the transition, in a second
        # same-transaction write to the already-locked row.
        order.fulfilment_status = fulfilment_for_status(target)
        order.save(update_fields=["status"])
        order.save(update_fields=["fulfilment_status"])
        # Audit row lands in the same transaction, so a rolled-back fulfil
        # leaves no phantom event.
        OrderStatusEvent.objects.create(
            order=order,
            from_status=previous_status,
            to_status=target,
            actor=request.user,
            trigger=TRIGGER_ADMIN_API_FULFIL,
        )
        # Side effects ride the same atomic block, after the audit row.
        notify_transition(order, previous_status, target)
        # Privileged-action record the admin surface would have written.
        log_api_action(
            request,
            order,
            CHANGE,
            f"Fulfilled via API: status moved to {target}.",
        )

    return Response(
        {
            "message": f"Order status advanced to {target}",
            "order_id": order.id,
            "status": order.status,
        }
    )


@api_view(["POST"])
@permission_classes([HasOrdersCancel])
def admin_order_cancel(request, order_id):
    """POST /api/admin/orders/:id/cancel — cancel an unpaid order; only
    ``pending`` carries a cancel edge, so a paid order's money returns through
    the refund seam instead. Idempotent: a re-cancel is a no-op 200 with no
    second audit row, and ``cancelled_at`` is written once."""
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(id=order_id)
        except Order.DoesNotExist:
            return Response(
                {"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if order.status == "cancelled":
            # The machine's self-transition: a replay, not a change.
            return Response(
                {
                    "message": "Order is already cancelled",
                    "order_id": order.id,
                    "status": order.status,
                }
            )

        if not transition_allowed(order.status, "cancelled"):
            return Response(
                {
                    "error": f"Order cannot be cancelled from status "
                    f"'{order.status}'. A paid order cannot be "
                    f"cancelled — issue a refund instead "
                    f"(POST /api/admin/orders/<id>/refund/).",
                },
                status=status.HTTP_409_CONFLICT,
            )

        previous_status = order.status
        order.status = "cancelled"
        order.cancelled_at = order.cancelled_at or timezone.now()
        # Fulfilment dimension rides the cancel transition.
        order.fulfilment_status = fulfilment_for_status("cancelled")
        order.save(update_fields=["status", "cancelled_at"])
        order.save(update_fields=["fulfilment_status"])
        # Audit row lands in this transaction; the replay above returns before
        # it, so a re-cancel never appends a second event.
        OrderStatusEvent.objects.create(
            order=order,
            from_status=previous_status,
            to_status="cancelled",
            actor=request.user,
            trigger=TRIGGER_ADMIN_API_CANCEL,
        )
        # Cancelled units return to available-to-sell now, not at the TTL
        # sweep; the replay above returns before this site.
        order.stock_reservations.filter(status=StockReservation.Status.ACTIVE).update(
            status=StockReservation.Status.RELEASED
        )
        # The replay returns before this site, so a re-cancel never notifies
        # twice.
        notify_transition(order, previous_status, "cancelled")
        log_api_action(request, order, CHANGE, "Cancelled via API.")

    return Response(
        {
            "message": "Order cancelled",
            "order_id": order.id,
            "status": order.status,
        }
    )

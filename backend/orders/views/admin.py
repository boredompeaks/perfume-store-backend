"""The staff-facing order seam: the unscoped order book plus the two
lifecycle writers a staff member drives directly (fulfil one step, cancel).

One of six modules split out of the former single-file `orders/views.py`.
Authorization is entirely declarative here - `permission_classes` carries
`orders.read`, `orders.fulfill` and `orders.cancel` - and every state change
goes through the shared machine in `orders.state` rather than a status write
of its own. The money-moving counterpart of this seam is `refund.py`.

The page-size contract is imported from `order.py` rather than restated: the
staff listing and the customer's history are deliberately the same envelope
with the same cap.
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

# [SPEC-12-02] StockReservation: a cancelled checkout releases its holds in
# this same transaction (admin_order_cancel below).
from products.models import StockReservation

# [R-10.16] SPEC-10-05: the per-transition side-effect contract (one
# dispatch point, shared with the payment writer).
from ..events import notify_transition
from ..models import Order, OrderStatusEvent

# Same house page-number envelope and page-size config as the customer
# history - the paginator cap exists so no caller, staff included, can
# request an unbounded page.
from .order import HISTORY_PAGE_SIZE_QUERY_PARAM, _history_page_size
from ..serializers import OrderSerializer

# [R-10.1] The order machine (transition table, gate, fulfilment step map)
# lives in orders.state - the single source; this module only consumes it.
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

# ==================================
# Admin orders JSON seam (SPEC-9-07, spec 9.4 Orders module)
# ==================================

# SPEC-9-07: the fulfilment endpoint drives the order one legal step per
# call along the flow the admin surface's bulk actions encode.
# ADMIN_FULFILMENT_NEXT (the step map) and transition_allowed (the gate)
# live in orders.state — [R-10.1] single source.


@api_view(["GET"])
@permission_classes([HasOrdersRead])
def admin_order_list(request):
    """[R-9.4.8] GET /api/admin/orders/ — every order, staff eyes only.

    The customer list scopes to ``user=request.user``; this seam is reached
    only through ``orders.read`` (support/finance/admin roles), so it serves
    the unscoped queryset. Same house page-number envelope and page-size
    config as the customer history — the paginator cap exists so no caller,
    staff included, can request an unbounded page."""
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
    """[R-9.4.9] GET /api/admin/orders/:id/ — one order, staff eyes only.

    Unlike the customer detail endpoint there is no ownership scoping to
    enforce, so an unknown id is a plain uniform 404 (never an existence
    leak — the caller has already passed the orders.read gate)."""
    try:
        order = Order.objects.select_related("coupon").get(id=order_id)
    except Order.DoesNotExist:
        return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

    return Response(OrderSerializer(order).data)


def _may_fulfil(user, order):
    """Whether ``user`` may advance ``order`` one fulfilment step.

    Two questions, not one. A caller holding ``orders.read`` sees the whole
    order book, so the fulfilment walk is unscoped for it (support and admin,
    the two roles that hold both capabilities; finance holds ``orders.read``
    but not ``orders.fulfill``, so it never reaches this seam at all). A
    caller holding ONLY ``orders.fulfill`` is the packing operator, whose
    authority is the queue — the statuses the walk can still advance, the
    same constant ``OrderAdmin``'s scoped grid lists — and nothing else.

    Django's ``is_superuser`` flag is preserved as the bypass every other
    surface keeps (mirroring ``capability_required``): the trust anchor must
    not be narrowed by a least-privilege rule meant for staff roles.

    Deny-by-default in both directions: an unknown capability grants nothing,
    and a caller with no role at all has already been refused by
    ``HasOrdersFulfill`` before this runs.
    """
    if user.is_superuser or user_has_capability(user, "orders.read"):
        return True
    return user_has_capability(user, "orders.fulfill") and (
        order.status in FULFILMENT_QUEUE_STATUSES
    )


@api_view(["POST"])
@permission_classes([HasOrdersFulfill])
def admin_order_fulfill(request, order_id):
    """[R-9.4.10] POST /api/admin/orders/:id/fulfill — advance one step.

    The gate-then-update pair runs under the row lock: two concurrent
    fulfils cannot both pass the gate on the same stale status, so an order
    can never skip two steps in one call. Deliberately NOT idempotent —
    each accepted call performs one visible transition; the machine itself
    rejects re-running a step from the new status (409). No business-event
    stamp is written here: the admin surface's mark_shipped/mark_delivered
    do not write shipped_at/delivered_at either, and the named-stamp
    writers are their own later task — the JSON seam never invents a richer
    record than the admin surface for the same transition.

    [R-1-B03] The fulfilment capability is not order visibility (spec 1.1
    line 110 splits them), so a caller holding ``orders.fulfill`` WITHOUT
    ``orders.read`` — the inventory/fulfilment operator — is scoped to the
    queue its own admin surface lists (``FULFILMENT_QUEUE_STATUSES``) and
    cannot advance an order outside it by guessing a pk. An out-of-queue
    order is answered with the SAME uniform 404 an unknown id gets: it must
    not confirm that the order exists, nor name its status, to a role that
    was deliberately not given order visibility. A caller that does hold
    ``orders.read`` (support, finance, admin) and Django's superuser flag
    keep the unrestricted contract above."""
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

        # [R-10.19]/[R-10.14] SPEC-10-03: the fulfil seam advances
        # confirmed→shipped too, so the shipped preconditions (payment
        # captured + items present) gate it here with the same authority
        # as the admin surface (insertion-only hunk; the 9-07 write below
        # stays byte-identical). The envelope middleware wraps this body,
        # so callers read the reasons at details.preconditions.
        precondition_reasons = precondition_failures(order, target)
        if precondition_reasons:
            return Response(
                {
                    "error": f"Order cannot be fulfilled from status '{order.status}'",
                    "preconditions": precondition_reasons,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # [R-10.12] SPEC-10-02: capture the pre-transition status for the
        # audit row written below (insertion-only hunk).
        previous_status = order.status
        order.status = target
        # [R-10.1] SPEC-10-01b: the fulfilment dimension rides the same
        # transition. Insertion-only hunk (the 9-07 save below stays
        # byte-identical), so the dimension persists via a second
        # same-transaction write to the row locked above.
        order.fulfilment_status = fulfilment_for_status(target)
        order.save(update_fields=["status"])
        order.save(update_fields=["fulfilment_status"])
        # [R-10.12]/[R-10.18] SPEC-10-02: the audit row lands in this same
        # transaction — a rolled-back fulfil never leaves a phantom event.
        OrderStatusEvent.objects.create(
            order=order,
            from_status=previous_status,
            to_status=target,
            actor=request.user,
            trigger=TRIGGER_ADMIN_API_FULFIL,
        )
        # [R-10.16] SPEC-10-05: the side-effect hook rides the same
        # atomic block, after the transition + its audit row
        # (insertion-only hunk).
        notify_transition(order, previous_status, target)
        # [6.12.6] API-side staff write: land the privileged-action record
        # the admin surface would have written (audit-log route reads it).
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
    """[R-9.4.11] POST /api/admin/orders/:id/cancel — cancel an unpaid order.

    The same machine gate the admin uses decides: only ``pending`` carries
    a cancel edge, so a paid order cannot be cancelled at all - its money
    comes back through the refund seam below instead, which records a
    Refund and moves the payment dimension without moving the status. The
    409 names that path.
    Idempotent: the machine's self-transition makes a re-cancel a no-op
    200 (no second stamp, no duplicate audit row). cancelled_at rides the
    transition exactly like admin ``cancel_pending`` — the is-none guard
    keeps a set event time immutable ([R-8.16])."""
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

        # [R-10.12] SPEC-10-02: capture the pre-transition status for the
        # audit row written below (insertion-only hunk).
        previous_status = order.status
        order.status = "cancelled"
        order.cancelled_at = order.cancelled_at or timezone.now()
        # [R-10.1] SPEC-10-01b: fulfilment dimension rides the cancel
        # transition (insertion-only; second same-transaction write).
        order.fulfilment_status = fulfilment_for_status("cancelled")
        order.save(update_fields=["status", "cancelled_at"])
        order.save(update_fields=["fulfilment_status"])
        # [R-10.12]/[R-10.18] SPEC-10-02: the audit row lands in this same
        # transaction; the idempotent replay above returns before reaching
        # it, so a re-cancel never appends a second event.
        OrderStatusEvent.objects.create(
            order=order,
            from_status=previous_status,
            to_status="cancelled",
            actor=request.user,
            trigger=TRIGGER_ADMIN_API_CANCEL,
        )
        # [R-12.8] SPEC-12-02 §12.1 step 6: a cancelled checkout releases
        # its holds in this same transaction — cancelled units return to
        # available-to-sell immediately, not at the TTL sweep. The
        # idempotent replay above returns before this site, and the
        # active-only filter is a no-op on already-released holds.
        order.stock_reservations.filter(status=StockReservation.Status.ACTIVE).update(
            status=StockReservation.Status.RELEASED
        )
        # [R-10.16] SPEC-10-05: the side-effect hook rides the same
        # atomic block; the idempotent replay above returns before this
        # site, so a re-cancel never notifies twice (insertion-only hunk).
        notify_transition(order, previous_status, "cancelled")
        log_api_action(request, order, CHANGE, "Cancelled via API.")

    return Response(
        {
            "message": "Order cancelled",
            "order_id": order.id,
            "status": order.status,
        }
    )

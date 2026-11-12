"""The customer's own orders: the history listing and the keyed detail read.
The ``page_size`` contract here is shared with the returns listing."""

import logging

from django.conf import settings
from django.core.paginator import Paginator
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import Order
from ..serializers import OrderSerializer

logger = logging.getLogger(__name__)

# History page size is deployment config, capped so a client cannot request
# unbounded pages.
HISTORY_PAGE_SIZE_QUERY_PARAM = "page_size"


def _history_page_size(raw):
    """Resolve ``?page_size`` to an integer in [1, ORDER_HISTORY_MAX_PAGE_SIZE],
    falling back to the configured default and clamping over-cap values."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return settings.ORDER_HISTORY_PAGE_SIZE
    if value < 1:
        return settings.ORDER_HISTORY_PAGE_SIZE
    return min(value, settings.ORDER_HISTORY_MAX_PAGE_SIZE)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_list(request):

    orders = Order.objects.filter(user=request.user).order_by("-created_at")

    # The unique id tiebreaker makes the sort total, so a paginated partition
    # never repeats or skips a row across requests.
    orders = orders.order_by("-created_at", "-id")

    paginator = Paginator(
        orders,
        _history_page_size(request.query_params.get(HISTORY_PAGE_SIZE_QUERY_PARAM)),
    )
    # get_page never raises: a bad page falls back to 1 and a page past the end
    # to the last page — no 404 for a stale page link.
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
@permission_classes([IsAuthenticated])
def order_detail(request, order_id):
    """GET /account/orders/:id — the caller's OWN order only. Ownership is part
    of the lookup, so a foreign order and an unknown id get the same uniform
    404 (never a 403 that would leak the id's existence)."""
    try:
        order = Order.objects.get(id=order_id, user=request.user)

    except Order.DoesNotExist:
        return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

    serializer = OrderSerializer(order)

    return Response(serializer.data)

"""The customer's own orders: the history listing and the keyed detail read.

One of six modules split out of the former single-file `orders/views.py`. The
`page_size` contract defined here is deliberately SHARED with the returns
listing - the original comment on `RETURNS_PAGE_SIZE_QUERY_PARAM` said one
name across both account listings is the point - so `return.py` imports these
two names from this module rather than repeating them.
"""

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

# ==================================
# Order List
# ==================================

# SPEC-9-04 [R-9.2.14]: history page size. Spec §9.2 prescribes the
# order-history endpoint without pinning a page size, so the default is
# deployment config (conventions.md: no hardcoded thresholds), capped for
# ?page_size callers so a client cannot request unbounded pages.
HISTORY_PAGE_SIZE_QUERY_PARAM = "page_size"


def _history_page_size(raw):
    """Resolve the ?page_size query param: an integer in
    [1, ORDER_HISTORY_MAX_PAGE_SIZE]; anything unparseable, non-positive or
    over the cap falls back to the configured default / cap respectively."""
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

    # SPEC-9-04: the unique id tiebreaker makes the sort total, so a
    # paginated partition never repeats or skips a row across requests
    # (same reasoning as the products listing's F-12 fix).
    orders = orders.order_by("-created_at", "-id")

    paginator = Paginator(
        orders,
        _history_page_size(request.query_params.get(HISTORY_PAGE_SIZE_QUERY_PARAM)),
    )
    # get_page never raises: an unparsable page falls back to 1, a page
    # past the end to the last page — no 404 for a stale page link.
    page = paginator.get_page(request.query_params.get("page", 1))

    serializer = OrderSerializer(page.object_list, many=True)

    # House page-number envelope (products-listing parity).
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
    """[R-9.2.15] GET /account/orders/:id — the caller's OWN order only.

    Ownership is part of the lookup itself: a foreign user's order (and an
    unknown id alike) gets the same uniform 404 — never a 200 (the IDOR
    pin) and never a 403 that would confirm the id's existence
    (conventions.md: no existence leaks)."""
    try:
        order = Order.objects.get(id=order_id, user=request.user)

    except Order.DoesNotExist:
        return Response({"error": "Order not found"}, status=status.HTTP_404_NOT_FOUND)

    serializer = OrderSerializer(order)

    return Response(serializer.data)

"""The customer's returns family: create a return request, list the caller's
own, and read one by pk. Ownership is the query filter, guest returns are
structurally unreachable, and nothing here moves money."""

from datetime import timedelta

from django.conf import settings
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

# The merchant-configurable window lives in ops.SiteSettings (a leaf module, so
# no cycle); CLOSED_RETURN_WINDOW_DAYS means returns are off (0).
from ops.models import CLOSED_RETURN_WINDOW_DAYS, SiteSettings

# The return-request row and the open-status tuple its duplicate guard reads.
from ..models import RETURN_OPEN_STATUSES, Order, ReturnRequest
from ..serializers import ReturnRequestSerializer

# The captured-money set, derived from the machine's transition table.
from ..state import CAPTURED_MONEY_PAYMENT_STATUSES

# Same query-param name as the order history; the default and cap differ.
from .order import HISTORY_PAGE_SIZE_QUERY_PARAM

RETURNS_PAGE_SIZE_QUERY_PARAM = HISTORY_PAGE_SIZE_QUERY_PARAM


# Every body field this seam reads, so the type gate below cannot drift from it.
_RETURN_BODY_FIELDS = ("order_number", "reason_code", "reason_note")


class MalformedReturnRequestBody(Exception):
    """A body this seam cannot read as text; carries its own 400 answer."""

    def __init__(self, error):
        super().__init__(error)
        self.error = error


def _return_body(request):
    """The three text fields of this seam's body, stripped. Type-gated before
    any string operation: a non-``str`` value would 500 instead of being
    refused, and absent/null reads as empty."""
    data = request.data
    if not isinstance(data, dict):
        raise MalformedReturnRequestBody("A JSON object body is required")
    fields = {}
    for field in _RETURN_BODY_FIELDS:
        raw = data.get(field)
        if raw is None:
            fields[field] = ""
        elif isinstance(raw, str):
            fields[field] = raw.strip()
        else:
            raise MalformedReturnRequestBody(f"{field} must be a string")
    return fields


# Refusals as machine-readable codes, not prose, so the storefront can render
# them without matching English; only the closed refusal says so explicitly.
RETURN_REFUSAL_NOT_ELIGIBLE = "not_eligible"
RETURN_REFUSAL_WINDOW_CLOSED = "window_closed"
RETURN_REFUSAL_OUTSIDE_WINDOW = "outside_window"

# The two order-specific refusals share one sentence byte-for-byte; naming
# which fired would disclose nothing but fork a pinned contract.
RETURN_REFUSAL_ERRORS = {
    RETURN_REFUSAL_NOT_ELIGIBLE: "This order is not eligible for a return",
    RETURN_REFUSAL_OUTSIDE_WINDOW: "This order is not eligible for a return",
    RETURN_REFUSAL_WINDOW_CLOSED: "Returns aren't available for this order",
}

# Not ``code``: the error envelope assigns ``code`` from the status family and
# moves the rest into ``details``, where this code would be buried.
RETURN_REFUSAL_BODY_KEY = "return_refusal"


def _return_eligible(order):
    """Whether ``order`` has anything a customer could send back: ineligible
    only while neither dimension moved (no captured money, nothing shipped),
    then subject to the store's return window. Both halves come off the machine,
    never from status literals."""
    money_moved = order.payment_status in CAPTURED_MONEY_PAYMENT_STATUSES
    goods_moved = order.fulfilment_status != "unfulfilled"
    if not (money_moved or goods_moved):
        return False
    return _return_window_refusal(order, timezone.now()) is None


def _return_window_days():
    """The return window this store publishes, in days. Read from
    ``ops.SiteSettings`` (store policy, not deployment config); a published 0 is
    returned as 0 so the refusal gate sees the closed state."""
    return SiteSettings.load().resolved_return_window_days()


def _return_window_anchor(order):
    """The instant the window is measured from: ``delivered_at`` when stamped,
    else the order date. The fallback is the production case, since nothing
    writes the delivery stamps yet."""
    return order.delivered_at or order.created_at


def _returns_closed():
    """Whether this store has published ``CLOSED_RETURN_WINDOW_DAYS`` (0). Read
    after the verdict to pick the sentence, and it overrides the order-specific
    reason because it is a fact about the store."""
    return SiteSettings.load().returns_closed()


def _return_window_refusal(order, now):
    """The window's verdict on ``order`` at ``now``: a refusal code or None. The
    closed check comes first (a published 0 refuses at every age); otherwise the
    window is inclusive of its last day."""
    window = _return_window_days()
    if window == CLOSED_RETURN_WINDOW_DAYS:
        return RETURN_REFUSAL_WINDOW_CLOSED
    if now > _return_window_anchor(order) + timedelta(days=window):
        return RETURN_REFUSAL_OUTSIDE_WINDOW
    return None


def _return_request_miss():
    """The one answer every return-request lookup failure gets: unknown order,
    another customer's order and a guest order are byte-identical, so the
    endpoint is not an order-existence oracle."""
    return Response(
        {"error": "Order not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def _return_request_payload(request_row):
    """The confirmation body, driven by ``ReturnRequestSerializer`` so create,
    list and detail share one representation."""
    return ReturnRequestSerializer(request_row).data


def _return_detail_miss():
    """The one answer every return-request detail miss gets: nonexistent pk,
    another customer's pk and a guest return are byte-identical 404s, never an
    IDOR oracle."""
    return Response(
        {"error": "Return request not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def _returns_page_size(raw):
    """Resolve ``?page_size`` for the returns listing: default and cap come from
    this surface's own settings, which is why it is not shared with the order
    resolver."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return settings.RETURNS_HISTORY_PAGE_SIZE
    if value < 1:
        return settings.RETURNS_HISTORY_PAGE_SIZE
    return min(value, settings.RETURNS_HISTORY_MAX_PAGE_SIZE)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def return_requests(request):
    """The customer's returns family root: GET lists the caller's own and POST
    creates one. One url pattern dispatches both, since Django would otherwise
    leave the second pattern unreachable behind the first."""
    if request.method == "POST":
        return _create_return_request(request)
    return _list_return_requests(request)


def _list_return_requests(request):
    """GET .../returns/ — the caller's OWN returns, ownership being the filter.
    Paginated in the house envelope with a total sort order."""
    returns = (
        ReturnRequest.objects.filter(order__user=request.user)
        .select_related("order")
        .order_by("-created_at", "-id")
    )

    paginator = Paginator(
        returns,
        _returns_page_size(request.query_params.get(RETURNS_PAGE_SIZE_QUERY_PARAM)),
    )
    # get_page never raises: a bad page falls back to 1 and a page past the end
    # to the last page — no 404 for a stale page link.
    page = paginator.get_page(request.query_params.get("page", 1))

    return Response(
        {
            "count": paginator.count,
            "total_pages": paginator.num_pages,
            "current_page": page.number,
            "next_page": page.has_next(),
            "previous_page": page.has_previous(),
            "results": ReturnRequestSerializer(page.object_list, many=True).data,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def return_request_detail(request, return_request_id):
    """GET .../returns/<pk>/ — ONE of the caller's returns. The lookup is
    ownership-scoped, and the miss is one uniform 404 (never a 403 that confirms
    the pk is real)."""
    try:
        return_row = ReturnRequest.objects.select_related("order").get(
            pk=return_request_id,
            order__user=request.user,
        )
    except ReturnRequest.DoesNotExist:
        return _return_detail_miss()

    return Response(ReturnRequestSerializer(return_row).data)


def _create_return_request(request):
    """POST /api/v1/store/orders/returns/ — request a return, named by
    customer-facing order number and always scoped to the caller. Nothing here
    moves money; duplicates are prevented under the order lock."""
    try:
        body = _return_body(request)
    except MalformedReturnRequestBody as malformed:
        return Response(
            {"error": malformed.error},
            status=status.HTTP_400_BAD_REQUEST,
        )
    order_number = body["order_number"]
    reason_code = body["reason_code"]
    reason_note = body["reason_note"]

    # Input validation runs before the lookup and answers 400s identical
    # whatever the order number is, so no refusal can disclose an order exists.
    if not order_number:
        return Response(
            {"error": "order_number is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if reason_code not in ReturnRequest.ReasonCode.values:
        return Response(
            {"error": "A valid reason_code is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        # Lock the ORDER row: every requester takes it first, so no other writer
        # can insert an open request between this probe and this commit.
        try:
            order = Order.objects.select_for_update().get(
                order_number=order_number,
                user=request.user,
            )
        except Order.DoesNotExist:
            # One shape for "no such order", "not yours" and "a guest order".
            return _return_request_miss()

        if not _return_eligible(order):
            # Two independent refusals answer 409 with the same body (dimensions
            # and window); the closed one overrides the reason and carries a code.
            if _returns_closed():
                return Response(
                    {
                        "error": RETURN_REFUSAL_ERRORS[RETURN_REFUSAL_WINDOW_CLOSED],
                        RETURN_REFUSAL_BODY_KEY: RETURN_REFUSAL_WINDOW_CLOSED,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(
                {"error": RETURN_REFUSAL_ERRORS[RETURN_REFUSAL_NOT_ELIGIBLE]},
                status=status.HTTP_409_CONFLICT,
            )

        already_open = ReturnRequest.objects.filter(
            order=order,
            status__in=RETURN_OPEN_STATUSES,
        ).exists()
        if already_open:
            return Response(
                {"error": "This order already has an open return request"},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            # Savepoint: an IntegrityError inside this transaction would poison
            # the outer block, turning the duplicate into a 500 instead of a 409.
            with transaction.atomic():
                return_request = ReturnRequest.objects.create(
                    order=order,
                    reason_code=reason_code,
                    reason_note=reason_note,
                )
        except IntegrityError:
            return Response(
                {"error": "This order already has an open return request"},
                status=status.HTTP_409_CONFLICT,
            )

    return Response(
        _return_request_payload(return_request),
        status=status.HTTP_201_CREATED,
    )

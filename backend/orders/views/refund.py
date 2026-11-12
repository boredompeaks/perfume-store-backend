"""Staff refund seam: reversing a captured payment in full or in part, plus the
customer's refund REQUEST. The admin writer moves only the payment dimension
(a refund is not a cancel); the request seam records the ask, never the money.
"""

from decimal import Decimal

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.money import quantize_money
from common.permissions import HasRefundsCreate

from ..models import Order, Refund, RefundRequest

# Same issuance core as the admin action: only transport lives here, so the two
# surfaces cannot drift.
from ..refunds import RefundGatewayError, RefundRefused, issue_refund
from ..serializers import RefundRequestSerializer

# The refundable money set is the machine's own answer, never a literal.
from ..state import CAPTURED_MONEY_PAYMENT_STATUSES

# The idempotency header contract is defined once, in checkout.
from .checkout import IDEMPOTENCY_KEY_HEADER, IDEMPOTENCY_KEY_MAX_LENGTH

REFUSAL_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
}


def _object_body(data):
    """The write body as a dict, or ``{}`` when it is not a JSON object. A
    non-object body has no ``.get``, so reading one would 500 this money seam;
    inlined from ``cart.views`` to avoid an import cycle."""
    return data if isinstance(data, dict) else {}


def _refund_payload(refund):
    """The refund representation, built inline: one writer and its own callers,
    so a serializer would only name the same keys. Money stays Decimal for the
    renderer to stringify."""
    return {
        "id": refund.id,
        "amount": refund.amount,
        "kind": refund.kind,
        "status": refund.status,
        "reason": refund.reason,
        "gateway_refund_id": refund.gateway_refund_id,
        "actor_id": refund.actor_id,
        "created_at": refund.created_at,
    }


@api_view(["POST"])
@permission_classes([HasRefundsCreate])
def admin_order_refund(request, order_id):
    """POST /api/admin/orders/:id/refund — refund a captured payment in full or
    in part. Authority is ``refunds.create`` (finance/admin), not order read or
    fulfil; the Order row and its refund rows are locked for the whole attempt,
    so concurrent refunds cannot both pass the balance gate."""
    body = _object_body(request.data)

    idempotency_key = (
        request.headers.get(IDEMPOTENCY_KEY_HEADER) or ""
    ).strip() or None

    if (
        idempotency_key is not None
        and len(idempotency_key) > IDEMPOTENCY_KEY_MAX_LENGTH
    ):
        return Response(
            {"error": "Idempotency-Key is too long"}, status=status.HTTP_400_BAD_REQUEST
        )

    try:
        receipt = issue_refund(
            request,
            order_id=order_id,
            amount=body.get("amount"),
            reason=body.get("reason") or "",
            idempotency_key=idempotency_key,
            source="API",
        )
    except RefundRefused as exc:
        return Response({"error": exc.message}, status=REFUSAL_STATUS[exc.code])
    except RefundGatewayError:
        # Provider detail stays in the core's log; the caller gets the fact
        # only.
        return Response(
            {"error": "The payment gateway could not complete this refund"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    if receipt.replayed:
        return Response(
            {
                "message": "Refund already issued",
                "order_id": receipt.order.id,
                "status": receipt.order.status,
                "payment_status": receipt.order.payment_status,
                "refunded_total": Refund.refunded_total(receipt.order),
                "refundable_remaining": receipt.order.refundable_remaining,
                "refund": _refund_payload(receipt.refund),
            }
        )

    return Response(
        {
            "message": "Refund issued",
            "order_id": receipt.order.id,
            "status": receipt.order.status,
            "payment_status": receipt.order.payment_status,
            "refunded_total": Refund.refunded_total(receipt.order),
            "refundable_remaining": receipt.order.refundable_remaining,
            "refund": _refund_payload(receipt.refund),
        },
        status=status.HTTP_201_CREATED,
    )


# The refundable set comes from ``orders/state.py`` rather than a literal here,
# so this seam and the payment machine cannot drift.


def _refund_request_miss():
    """The one answer for an unknown order and a stranger's order: identical
    body and status, so the seam is not an order-existence oracle."""
    return Response(
        {"error": "Order not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def refund_request_amount(order, item_ids):
    """The money a filed request asks for, in 2 dp: empty ``items`` means the
    order's remaining refundable balance, otherwise the frozen ``subtotal`` sum
    of the named lines. Raises :class:`RefundRefused` when a named line is no
    longer on the order."""
    ids = list(dict.fromkeys(item_ids or []))
    if not ids:
        return quantize_money(order.refundable_remaining)
    lines = list(order.items.filter(pk__in=ids))
    if len(lines) != len(ids):
        raise RefundRefused(
            "This request names order items that are no longer on the order",
            code="invalid",
        )
    return quantize_money(sum((line.subtotal for line in lines), Decimal("0.00")))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def refund_request(request, order_id):
    """POST /api/orders/:id/refund-request/ — the customer's ask, not the
    refund: no gateway call and no ``Refund`` row, so it never touches
    ``payment_status``. The lookup is ownership-scoped and locked, and a
    partial unique index bounds filings to one pending request per order."""
    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().get(
                pk=order_id,
                user=request.user,
            )
        except Order.DoesNotExist:
            return _refund_request_miss()

        if order.payment_status not in CAPTURED_MONEY_PAYMENT_STATUSES:
            return Response(
                {"error": "Only a captured payment can be refunded"},
                status=status.HTTP_409_CONFLICT,
            )

        already_pending = RefundRequest.objects.filter(
            order=order,
            status=RefundRequest.Status.PENDING,
        ).exists()
        if already_pending:
            return Response(
                {"error": "This order already has a pending refund request"},
                status=status.HTTP_409_CONFLICT,
            )

        serializer = RefundRequestSerializer(
            data=request.data,
            context={"order": order},
        )
        serializer.is_valid(raise_exception=True)

        try:
            # Savepoint so a racing insert's IntegrityError becomes a 409
            # instead of a poisoned outer transaction.
            with transaction.atomic():
                instance = serializer.save(order=order, requester=request.user)
        except IntegrityError:
            return Response(
                {"error": "This order already has a pending refund request"},
                status=status.HTTP_409_CONFLICT,
            )

    return Response(
        RefundRequestSerializer(instance).data,
        status=status.HTTP_201_CREATED,
    )

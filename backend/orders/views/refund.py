"""The staff refund seam: reversing a captured payment, in full or in part.

One of six modules split out of the former single-file `orders/views.py`. This
is the only writer of the two refund payment values, and it moves the PAYMENT
dimension without ever moving the order status - a refund is not a cancel,
which is why an order whose money is returned stays refundable-eligible rather
than becoming a `cancelled` row.

Nothing in this module was rewritten - the bodies moved verbatim - so the
guarantees documented on the view are the ones the split inherits. The
idempotency-key header names are imported from `checkout.py`, the one module
that defines that contract, so the two surfaces cannot drift apart.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from common.permissions import HasRefundsCreate

from ..models import Refund

# [R-1.14] SPEC-1-05: the same issuance core the admin action calls. Transport
# concerns (headers, response shapes) stay here; every decision about whether
# money may move lives in the core, so the two surfaces cannot drift.
from ..refunds import RefundGatewayError, RefundRefused, issue_refund

# The header-keyed idempotency contract is defined once, in checkout, and
# reused here rather than restated.
from .checkout import IDEMPOTENCY_KEY_HEADER, IDEMPOTENCY_KEY_MAX_LENGTH

# The domain refusal's code -> the HTTP status this endpoint has always used.
REFUSAL_STATUS = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
}


def _object_body(data):
    """The write body as a dict, or ``{}`` when it is not a JSON object.

    A JSON array / scalar / null body parses fine but has no ``.get`` to
    call, so reading keys off it raised AttributeError — a 500 on this
    staff money seam (F7-B1). Reading it as the empty object routes every
    such body into this endpoint's own refusal below (the required-reason
    400), byte-identical to posting ``{}``; form posts are QueryDict (a
    dict subclass) and are unaffected. Same helper as
    ``cart.views._object_body``, inlined because cart.views imports
    orders.views — importing it back here would be an order-dependent
    circular import."""
    return data if isinstance(data, dict) else {}


# ==================================
# Admin refund seam (SPEC-1-05, spec 1.14 / [1.31])
# ==================================


def _refund_payload(refund):
    """The refund representation this seam returns (explicit fields).

    Built inline rather than through a serializer module: the record is
    written by this one writer and read only by its own callers, so a
    serializer class would exist purely to name the same eight keys. Money
    stays a Decimal here and the renderer stringifies it, exactly as
    OrderSerializer does with the order's amounts.
    """
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
    """[R-1.14] POST /api/admin/orders/:id/refund — refund a captured payment.

    Spec 1.14 makes a paid order refundable in full or in part, and the
    finance operator the one who reconciles payments and refunds ([1.31]), so
    authority is the ``refunds.create`` capability (finance/admin). That is
    the split spec 1.1 spells out: a catalogue manager must not be able to
    issue refunds, and neither can a support agent who may read and fulfil
    orders.

    Atomic and serialized: the Order row is locked for the whole attempt
    (this order's refund rows beside it, since they are the other half of the
    balance being spent), the balance is recomputed under those locks, and the
    gateway call happens inside the same transaction. So two concurrent
    refunds can never both pass the balance gate, and a gateway failure rolls
    the attempt back whole — no refund row, no payment-dimension move, no
    timestamp, no audit record.

    Idempotent-safe twice over: an ``Idempotency-Key`` collapses a retry onto
    the refund it already produced (no second gateway call), and without a key
    the balance gate alone still refuses to hand back the same money twice.

    The payment dimension moves to the choices orders.state already declares
    (``partially_refunded`` / ``refunded``) — this writer is their first
    writer, and no new choice was added for them.
    """
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
        # The provider's own text went to the log in the core; the caller
        # gets the fact and nothing about the provider's internals.
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

"""The staff refund seam: reversing a captured payment, in full or in part.

One of six modules split out of the former single-file `orders/views.py`. This
is the only writer of the two refund payment values, and it moves the PAYMENT
dimension without ever moving the order status - a refund is not a cancel,
which is why an order whose money is returned stays refundable-eligible rather
than becoming a `cancelled` row.

It also carries the customer's refund REQUEST (SPEC-E-01). That seam is the
ask and never the money: it writes no ``Refund`` row and moves no payment
value, which is what keeps "refund requested" from being read as "refund
completed"; approval is E-02.

Nothing in this module was rewritten - the bodies moved verbatim - so the
guarantees documented on the view are the ones the split inherits. The
idempotency-key header names are imported from `checkout.py`, the one module
that defines that contract, so the two surfaces cannot drift apart.
"""

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import HasRefundsCreate

from ..models import Order, Refund, RefundRequest

# [R-1.14] SPEC-1-05: the same issuance core the admin action calls. Transport
# concerns (headers, response shapes) stay here; every decision about whether
# money may move lives in the core, so the two surfaces cannot drift.
from ..refunds import RefundGatewayError, RefundRefused, issue_refund
from ..serializers import RefundRequestSerializer

# [R-10.1] The money half of a refundable order is the machine's own answer to
# "can this store still move money back out", never a literal beside it.
from ..state import CAPTURED_MONEY_PAYMENT_STATUSES

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


# ==================================
# Customer refund request (SPEC-E-01)
# ==================================

# The refundable set is NOT restated here. ``refunded`` has nothing left to
# return, so a request against it is a conflict rather than a new ask; that
# judgement already lives in ``orders/state.py``, where it is derived from the
# payment machine's own edges. A literal here would be the exact drift B07a
# was raised for - the returns gate tracked one value while the machine
# tracked two - and the two seams must answer from one source.


def _refund_request_miss():
    """The one answer for an unknown order and a stranger's order.

    Identical status and body for both, so the seam is not an order-existence
    oracle: ``order.user == request.user`` is part of the lookup, not a check
    after it.
    """
    return Response(
        {"error": "Order not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def refund_request(request, order_id):
    """[SPEC-E-01] POST /api/orders/:id/refund-request/.

    The customer's ASK, not the refund. No gateway call is made and no
    ``Refund`` row is written here - E-02 is the staff approval that may hand
    the request to the refund writer. Keeping the two apart is what makes
    "refund requested" and "refund completed" different facts (spec 6.8 line
    1985), and it is why this seam never touches ``payment_status``.

    The order lookup is ownership-scoped and locked for the attempt. Ownership
    in the lookup means a foreign order gets the same uniform 404 an unknown
    one gets; the lock is what serializes concurrent filings for this order,
    with the partial unique index on ``RefundRequest.order`` as the
    last-resort authority behind it.

    Body: ``reason`` (required, non-blank) and ``items`` (optional list of
    this order's item ids; empty or omitted means the whole order). Invalid
    item ids are a 400, never a silently narrowed request. The 201 body is the
    explicit-field ``RefundRequestSerializer`` projection.

    No throttle scope: this seam is ``IsAuthenticated`` and the partial unique
    index bounds the write to one pending request per owned order, so a caller
    cannot amplify rows the way an anonymous public endpoint could (the
    throttle rule in conventions.md is aimed at those).
    """
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
            # Savepoint so a racing insert's IntegrityError is a 409 rather
            # than a poisoned outer transaction (the same shape the returns
            # seam uses).
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

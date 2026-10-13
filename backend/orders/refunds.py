"""The Razorpay refund seam - the ONE place this store asks the gateway to
give money back.

Thin on purpose. It translates an internal refund decision into the
provider's refund call and nothing else: it does not decide whether an order
may be refunded, how much, or what the order's payment dimension becomes
afterwards (that is the writer's job, inside its own transaction - see
``orders.views.admin_order_refund``). Keeping the policy out of here is what
makes the seam mockable at one boundary, so the endpoint's atomicity can be
tested without a network.

Credentials are read from settings, which resolve ``RAZORPAY_KEY_ID`` /
``RAZORPAY_KEY_SECRET`` from the environment - no credential is written here.
``RAZORPAY_TIMEOUT`` bounds the gateway socket for the same reason
``EMAIL_TIMEOUT`` bounds the mail socket: this call is made while the caller's
transaction still holds the order lock, so an unbounded wait is a
checkout-blocking outage rather than a slow request.
The amount is quantized with the shared ``common.money`` helper before it is
scaled to the provider's minor units, the same rounding rule the capture
amount uses in ``create_payment``, so paying and refunding cannot disagree
about one amount.
"""

import razorpay
from django.conf import settings

from common.money import quantize_money


class RefundGatewayError(Exception):
    """The gateway did not refund the payment.

    Raised for a missing payment reference, any provider error, and a 2xx
    response that carries no refund id. Callers MUST let this escape their
    transaction (or re-raise it) so the attempt rolls back whole: a store
    that recorded a refund the gateway never made would be reconciling money
    it does not have. The provider's own message rides the exception text for
    the caller's log lines - never for an API response.
    """


def _minor_units(amount):
    """``Decimal('10.00') -> 1000``: the provider counts in the currency's
    smallest unit. Quantized first, so a half-paise request is rounded once
    here and never again by the provider."""
    return int(quantize_money(amount) * 100)


def refund_payment(*, payment_id, amount):
    """Refund ``amount`` of a captured Razorpay payment; return the refund id.

    ``amount`` is the store's Decimal money, not provider units - the scaling
    is this function's job. Raises :class:`RefundGatewayError` for every
    outcome that is not a refund the provider acknowledged.
    """
    if not payment_id:
        raise RefundGatewayError("no captured payment reference to refund")

    # Keyword form, never positional. The SDK's signature is
    # ``__init__(self, session=None, auth=None, **options)``, so passing the
    # credential pair positionally binds the KEY ID to ``session`` and the
    # SECRET to ``auth``; the client then calls ``.post`` on a string and every
    # refund dies as an AttributeError. ``orders.views.payment`` has always
    # used the keyword form - this seam must match it.
    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
    )

    try:
        # The timeout is the SDK's per-request kwarg, which it forwards
        # untouched to ``requests``. It is NOT a Client option: ``__init__``
        # consumes only base_url and the retry knobs, so a ``timeout=`` passed
        # to the constructor would be silently discarded. Without it the
        # refund socket waits forever, and this call runs inside the caller's
        # ``transaction.atomic()`` holding the order's row lock - a stalled
        # gateway would park every refund for that order behind it. A tripped
        # timeout raises ``requests.Timeout``, which the handler below already
        # turns into a typed RefundGatewayError, so the attempt rolls back
        # whole instead of hanging.
        response = client.refund.create(
            {"payment_id": payment_id, "amount": _minor_units(amount)},
            timeout=settings.RAZORPAY_TIMEOUT,
        )
    except Exception as exc:
        # razorpay's four error classes (BadRequestError, GatewayError,
        # ServerError, SignatureVerificationError) each derive from Exception
        # directly - there is no common base to catch - and the transport can
        # raise anything at all. A refund must never let a raw provider
        # exception escape as a 500: the caller only rolls the attempt back
        # when it recognizes a typed error.
        raise RefundGatewayError(f"{type(exc).__name__}: {exc}") from exc

    refund_id = (response or {}).get("id")
    if not refund_id:
        # A success with no id is not a refund: recording one would put a row
        # in the reconciliation trail that nobody can look up at the provider.
        raise RefundGatewayError("gateway refund response carried no id")

    return refund_id

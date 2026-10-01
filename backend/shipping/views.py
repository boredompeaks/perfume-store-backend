"""The storefront shipping estimate (SPEC-1-B05 [R-1.07], spec 9.1).

Spec 9.1 line 2703 declares `GET /store/shipping/estimate` beside the
product listing and ahead of the serviceability check, and the storefront's
delivery step is built on it: the mock at spec line 939 shows shipping as
"Calculated", not chosen, so the customer needs a price before checkout.

Anonymous by design (conventions.md:26, opened deliberately): a guest
checks out without an account (spec line 74), so a wall here would hide the
delivery price from exactly the customer who needs it. It is safe to open
because the response is a public price list for a destination - it carries no
cart, no order, no customer and no staff data, and the same numbers are
derivable by anyone from the published rate table.

No throttle scope: this is a read-only endpoint, and the convention requires
one on public MUTATING endpoints. The worst an anonymous caller can do here
is read a price.

[SPEC-1-B06] owns the second half of this module: the customer tracking
endpoint, which is the one shipping read that carries customer data and
therefore the only one that has to answer for a credential.
"""

from django.conf import settings

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Shipment
from .pricing import ShippingUnavailable, shipping_options
from .serializers import ShipmentTrackingSerializer, ShippingOptionSerializer


class ShippingEstimateView(APIView):
    """Delivery options for a destination, priced server-side.

    Both destination fields are REQUIRED rather than one being treated as an
    unconstrained wildcard, because the answer has to be the answer checkout
    will give: checkout always has both (they are required shipping fields),
    and resolving with only one of them could tell the storefront a
    destination is unserviceable when it is not - the region's rate is
    exactly the rule a pincode-only lookup cannot evaluate.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        region = (request.query_params.get("state") or "").strip()
        postal_code = (request.query_params.get("pincode") or "").strip()

        missing = [
            field
            for field, value in (("state", region), ("pincode", postal_code))
            if not value
        ]
        if missing:
            return Response(
                {
                    "error": (
                        f"{' and '.join(missing)} "
                        f"{'is' if len(missing) == 1 else 'are'} required "
                        "to estimate shipping"
                    )
                },
                status=400,
            )

        try:
            options = shipping_options(region=region, postal_code=postal_code)
        except ShippingUnavailable as exc:
            # The same refusal checkout gives, for the same destination: a
            # 400 the storefront renders as "we cannot deliver there yet",
            # never a zero it would render as free delivery.
            return Response({"error": str(exc)}, status=400)

        return Response(
            {
                "state": region,
                "pincode": postal_code,
                # The store currency rides every quoted amount (spec 8.3:
                # store the currency alongside the amount). Read from the
                # same never-changes-at-runtime config orders mint their own
                # currency from, so a quoted rate and a charged order can
                # never be denominated differently.
                "currency": settings.DEFAULT_CURRENCY,
                "options": ShippingOptionSerializer(options, many=True).data,
            }
        )


# ==================================
# [R-1.08] SPEC-1-B06: customer shipment tracking
# ==================================

# The credential header SPEC-1-B04 established for a guest order read. Tracking
# reads the SAME header, because it answers the same question for the same
# owner: possession of the token B04 minted at checkout. The name is repeated
# rather than imported so this app's view layer never depends on the orders
# app's view layer (orders.views already imports shipping.pricing, and a
# shipping -> orders.view import would close a two-way edge between them).
# ``tests_tracking.py`` pins this name and the length cap equal to B04's
# constants, so the duplication cannot drift.
GUEST_ORDER_TOKEN_HEADER = "X-Guest-Order-Token"
GUEST_TOKEN_MAX_LENGTH = 64


def _tracking_lookup_miss():
    """The one answer every tracking failure gets.

    Byte-identical for: no credential at all, a wrong token, an order number
    that does not exist, an order number that belongs to somebody else, a
    token minted for a DIFFERENT order, and an order that has no shipment yet.
    Any difference between those cases - a status code, a body, a key - would
    turn this endpoint into the order-existence oracle B04 spent its audit
    cycles closing (conventions.md: uniform responses on anonymous flows, no
    existence leaks).

    404 rather than 403 for the same reason ``_guest_lookup_miss`` is: "forbidden"
    confirms the order is real.
    """
    return Response(
        {"error": "Shipment not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def _trackable_shipment(request, order_number):
    """The one shipment ``request`` may see for ``order_number``, or None.

    Two credentials, and the order number is never one of them - spec 3.9
    line 1185 names ``/track-order`` and line 1189 defines its flow as "a
    secure, limited-access token or authenticated account".

    * An AUTHENTICATED caller owns the order by account. Filtering on
      ``order__user`` returns that customer's own account orders and no guest
      row at all, so signing in is never a way to read a guest order by its
      number.
    * An ANONYMOUS caller has no account, so possession of the B04 guest token
      IS the authorization: the filter runs against the STORED token, so a hit
      is by definition the guest order that token was minted for.
    """
    if request.user.is_authenticated:
        owner = {"order__user": request.user}
    else:
        token = (request.headers.get(GUEST_ORDER_TOKEN_HEADER) or "").strip()
        if not token or len(token) > GUEST_TOKEN_MAX_LENGTH:
            return None
        owner = {"order__guest_token": token}

    return (
        Shipment.objects.select_related("order")
        .filter(order__order_number=order_number, **owner)
        .first()
    )


class ShipmentTrackingView(APIView):
    """GET /api/v1/store/shipping/track/<order_number>/ - your parcel.

    Opened deliberately (conventions.md:26), and the shape of that opening is
    exactly B04's: a guest checks out without an account (spec line 74) and
    spec 3.9 line 1189 requires the tracking page to work for one. ``AllowAny``
    therefore does NOT mean unguarded - ``_trackable_shipment`` is the guard,
    and it is a guard by possession of a credential, not by identity: it can
    only ever return the one shipment whose order the caller owns.

    No throttle scope, for the same reason the estimate above has none: this
    is a read-only endpoint and the convention requires one on public MUTATING
    endpoints. Brute-forcing order numbers is also not the threat here - the
    order number is never the credential, so guessing one in a million earns
    the caller nothing, and the token that IS the credential is 256 bits.

    The response is the customer half of the row and nothing else: spec 6.9
    line 2027 requires the tracking page to withhold internal carrier
    credentials and warehouse notes, and the serializer is where that
    promise is kept.
    """

    permission_classes = [AllowAny]

    def get(self, request, order_number):
        shipment = _trackable_shipment(request, order_number)
        if shipment is None:
            return _tracking_lookup_miss()
        return Response(ShipmentTrackingSerializer(shipment).data)

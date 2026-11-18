"""The storefront shipping estimate and the customer tracking read.

The estimate is anonymous by design (a guest needs the price before checkout)
and carries no customer data; tracking is the one shipping read with customer
data, so it answers for a credential.
"""

from django.conf import settings
from django.utils.cache import patch_vary_headers
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Shipment
from .pricing import ShippingUnavailable, shipping_options
from .serializers import ShipmentTrackingSerializer, ShippingOptionSerializer


class TrackingRateThrottle(ScopedRateThrottle):
    """The dedicated 'tracking' budget for the customer tracking read.

    The endpoint's credential is possession of the order number plus the guest
    token; the order number is enumerable, so the throttle bounds enumeration.
    """


class ShippingEstimateView(APIView):
    """Delivery options for a destination, priced server-side.

    Both fields are required rather than wildcarded so the answer matches what
    checkout will charge; resolving with only one could call a serviceable
    destination unserviceable.
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
            # Same refusal checkout gives: a 400 "we cannot deliver", never a
            # zero the storefront would render as free delivery.
            return Response({"error": str(exc)}, status=400)

        return Response(
            {
                "state": region,
                "pincode": postal_code,
                # Read from the same runtime config orders mint their currency
                # from, so a quoted rate and a charged order cannot differ.
                "currency": settings.DEFAULT_CURRENCY,
                "options": ShippingOptionSerializer(options, many=True).data,
            }
        )


# Same header B04 established: repeated not imported to avoid a two-way view
# dependency; ``tests_tracking.py`` pins the name and length cap.
GUEST_ORDER_TOKEN_HEADER = "X-Guest-Order-Token"
GUEST_TOKEN_MAX_LENGTH = 64


def _tracking_lookup_miss():
    """The one answer every tracking miss gets.

    Byte-identical for every failure, because any difference would turn the
    endpoint into an order-existence oracle; 404 rather than 403 because
    "forbidden" confirms the order is real.
    """
    return Response(
        {"error": "Shipment not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def _trackable_shipments(request, order_number):
    """Every parcel of ``order_number`` that ``request`` may see, as a list.

    Split shipments mean one order has many parcels, so a one-row answer would
    hide all but the newest; authorization is the account owner or possession
    of the stored guest token, never the order number itself.
    """
    if request.user.is_authenticated:
        owner = {"order__user": request.user}
    else:
        token = (request.headers.get(GUEST_ORDER_TOKEN_HEADER) or "").strip()
        if not token or len(token) > GUEST_TOKEN_MAX_LENGTH:
            return []
        owner = {"order__guest_token": token}

    # Oldest parcel first (newest-first is the admin's); select_related the
    # provider so ``tracking_url`` does not cost one query per parcel.
    return list(
        Shipment.objects.select_related("order", "provider")
        .prefetch_related("events")
        .filter(order__order_number=order_number, **owner)
        .order_by("created_at", "id")
    )


class ShipmentTrackingView(APIView):
    """GET /api/v1/store/shipping/track/<order_number>/ - your parcels.

    ``AllowAny`` is not unguarded: ``_trackable_shipments`` returns only
    parcels of an order the caller owns, and the dedicated throttle bounds
    order-number enumeration. The body is a list because an order may have
    split shipments.
    """

    permission_classes = [AllowAny]
    throttle_classes = [TrackingRateThrottle]
    throttle_scope = "tracking"

    def finalize_response(self, request, response, *args, **kwargs):
        """Stamp privacy headers on every answer, including the uniform miss.

        ``no-store`` keeps parcel data out of shared caches, and ``Vary`` on
        the guest credential stops a cache keyed only by URL serving one
        guest's parcels to another; both apply to 404/429 too.
        """
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        patch_vary_headers(response, (GUEST_ORDER_TOKEN_HEADER,))
        return response

    def get(self, request, order_number):
        shipments = _trackable_shipments(request, order_number)
        if not shipments:
            # The credential decides WHETHER the list is empty, never what an
            # empty list looks like: every miss is one byte-identical answer.
            return _tracking_lookup_miss()
        return Response(
            {"shipments": ShipmentTrackingSerializer(shipments, many=True).data}
        )

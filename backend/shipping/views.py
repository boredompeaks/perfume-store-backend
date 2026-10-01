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
"""

from django.conf import settings

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .pricing import ShippingUnavailable, shipping_options
from .serializers import ShippingOptionSerializer


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

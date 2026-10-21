"""SPEC-D-08: the customer tracking endpoint is throttled and uncacheable.

The endpoint already had a credential (guest token or account) and a uniform
404. This file adds the two properties a cache/abuse review needs and pins that
neither the credential nor the uniform miss moved:

* a dedicated ``tracking`` throttle (60/min default) returns 429 after the
  limit, and the 429 still carries the privacy headers;
* every response - hit, uniform miss, and 429 - is ``Cache-Control: no-store``
  and ``Vary: X-Guest-Order-Token``;
* the estimate endpoint, which has no scope, is unaffected.
"""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from rest_framework.settings import api_settings

from shipping.models import Shipment, ShippingMethod, ShippingRate
from shipping.tests_tracking import ShipmentTestCase
from shipping.views import (
    GUEST_ORDER_TOKEN_HEADER,
    ShipmentTrackingView,
    TrackingRateThrottle,
)

ESTIMATE = "/api/v1/store/shipping/estimate/"


class TrackingHardeningTests(ShipmentTestCase):
    def _rates(self, tracking="1/min"):
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["tracking"] = tracking
        return rates

    def assert_privacy_headers(self, response):
        self.assertEqual(response["Cache-Control"], "no-store")
        vary = [part.strip() for part in response["Vary"].split(",")]
        self.assertIn(GUEST_ORDER_TOKEN_HEADER, vary)

    def test_the_view_declares_the_tracking_scope_and_rate(self):
        self.assertEqual(ShipmentTrackingView.throttle_scope, "tracking")
        self.assertEqual(ShipmentTrackingView.throttle_classes, [TrackingRateThrottle])
        self.assertEqual(api_settings.DEFAULT_THROTTLE_RATES["tracking"], "60/min")

    def test_a_hit_carries_the_privacy_headers(self):
        response = self.track(self.guest_order.order_number, token="guest-token-value")

        self.assertEqual(response.status_code, 200, response.data)
        self.assert_privacy_headers(response)

    def test_the_uniform_miss_is_unchanged_and_carries_the_headers(self):
        no_credential = self.track(self.guest_order.order_number)
        wrong_token = self.track(self.guest_order.order_number, token="not-the-token")

        self.assertEqual(no_credential.status_code, 404)
        self.assertEqual(wrong_token.status_code, 404)
        self.assertEqual(no_credential.data["error"], "Shipment not found")
        self.assertEqual(no_credential.data, wrong_token.data)
        self.assert_privacy_headers(no_credential)
        self.assert_privacy_headers(wrong_token)

    def test_the_limit_returns_429_and_the_429_keeps_the_headers(self):
        with mock.patch.object(TrackingRateThrottle, "THROTTLE_RATES", self._rates()):
            cache.clear()
            first = self.track(self.guest_order.order_number, token="guest-token-value")
            second = self.track(
                self.guest_order.order_number, token="guest-token-value"
            )

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 429, second.data)
        self.assert_privacy_headers(first)
        self.assert_privacy_headers(second)

    def test_the_estimate_endpoint_is_not_throttled_by_the_tracking_scope(self):
        method = ShippingMethod.objects.create(code="standard", name="Standard")
        ShippingRate.objects.create(method=method, amount=Decimal("49.00"))

        with mock.patch.object(TrackingRateThrottle, "THROTTLE_RATES", self._rates()):
            cache.clear()
            responses = [
                self.client.get(ESTIMATE, {"state": "Maharashtra", "pincode": "400001"})
                for _ in range(3)
            ]

        for response in responses:
            self.assertEqual(response.status_code, 200, response.data)
        # The tracking view still owns the budget: a fourth tracking read is
        # refused even though the estimate calls above spent nothing.
        with mock.patch.object(TrackingRateThrottle, "THROTTLE_RATES", self._rates()):
            first = self.track(self.guest_order.order_number, token="guest-token-value")
            second = self.track(
                self.guest_order.order_number, token="guest-token-value"
            )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 429, second.data)

    def test_an_account_owner_still_reads_only_their_own_parcel(self):
        self.login_as(self.buyer)

        own = self.track(self.order.order_number)
        guest = self.track(self.guest_order.order_number, token="guest-token-value")

        self.assertEqual(own.status_code, 200, own.data)
        self.assertTrue(
            Shipment.objects.filter(
                order__order_number=self.order.order_number
            ).exists()
        )
        # Signing in is not a second way to read a guest order.
        self.assertEqual(guest.status_code, 404)

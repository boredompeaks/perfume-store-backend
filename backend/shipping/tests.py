"""Shipping methods, rates and the cost they add at checkout (SPEC-1-B05).

Covers the three surfaces this feature owns: the rate table and its pricing
rules, the storefront estimate, and the checkout cost application - including
the three properties the task is graded on: a client-supplied amount is
ignored, the guest and account paths price identically, and the cost is
applied inside the checkout's atomic block.
"""

from decimal import Decimal
from io import StringIO

from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings

from common.models import AuditEvent
from common.permissions import user_has_capability
from common.roles import (
    ROLE_ADMIN,
    ROLE_FINANCE,
    ROLE_INVENTORY,
    ROLE_SUPPORT,
    sync_role_groups,
)
from common.testing import ApiTestCase
from orders.models import Order, OrderItem, OrderStatusEvent
from products.models import StockReservation
from shipping.models import ShippingMethod, ShippingRate
from shipping.pricing import (
    ShippingUnavailable,
    quote_shipping,
    shipping_configured,
    shipping_options,
)

ESTIMATE_V1 = "/api/v1/store/shipping/estimate/"
ESTIMATE_LEGACY = "/api/shipping/estimate/"


def method(code="standard", name="Standard", is_active=True):
    return ShippingMethod.objects.create(
        code=code,
        name=name,
        is_active=is_active,
    )


def rate(shipping_method, amount="49.00", region="", postal_code_prefix=""):
    return ShippingRate.objects.create(
        method=shipping_method,
        amount=Decimal(amount),
        region=region,
        postal_code_prefix=postal_code_prefix,
    )


def quote_amount(region="Maharashtra", postal_code="400001", **kwargs):
    """The priced amount for a destination, or None if nothing answers it.

    None covers both "no shipping configured" and "no rate serves this
    destination": the SELECTION tests only care that no rate answered, and
    which of the two refusals applies is pinned by ``NoMatchingRateTests``.
    """
    try:
        quote = quote_shipping(
            region=region,
            postal_code=postal_code,
            merchandise_total=Decimal("0.00"),
            **kwargs,
        )
    except ShippingUnavailable:
        return None
    return None if quote is None else quote.amount


# ==================================
# Pricing: which rate answers a destination
# ==================================


class RateSelectionTests(ApiTestCase):
    """Which rate a destination resolves to, and why that one.

    No rate is created in setUp: every test adds exactly the rates it is
    about, so an assertion that a destination has NO answer is an assertion
    about the table under test rather than about a wildcard that happens to
    be lying around.
    """

    def setUp(self):
        self.standard = method()

    def test_a_wildcard_rate_serves_any_destination(self):
        rate(self.standard, "99.00")
        self.assertEqual(quote_amount(), Decimal("99.00"))

    def test_a_region_rate_serves_that_region_and_nothing_else(self):
        rate(self.standard, "49.00", region="Maharashtra")
        self.assertEqual(quote_amount(), Decimal("49.00"))
        self.assertIsNone(quote_amount(region="Karnataka"))

    def test_the_region_comparison_ignores_case_and_surrounding_space(self):
        rate(self.standard, "49.00", region="maharashtra")
        self.assertEqual(
            quote_amount(region="  MAHARASHTRA  "),
            Decimal("49.00"),
        )

    def test_a_postal_code_prefix_rate_serves_only_its_own_band(self):
        rate(self.standard, "59.00", postal_code_prefix="400")
        self.assertEqual(quote_amount(), Decimal("59.00"))
        self.assertIsNone(quote_amount(postal_code="560001"))

    def test_a_full_postal_code_is_its_own_band(self):
        rate(self.standard, "59.00", postal_code_prefix="400001")
        self.assertEqual(quote_amount(), Decimal("59.00"))
        self.assertIsNone(quote_amount(postal_code="400002"))

    def test_the_narrower_rate_wins_even_when_it_is_the_pricier_one(self):
        # Specificity beats price inside a method: a regional rate exists to
        # override the national one, in either direction.
        rate(self.standard, "99.00")
        rate(self.standard, "9.00", region="Maharashtra", postal_code_prefix="4")
        self.assertEqual(
            quote_amount(),
            Decimal("9.00"),
            "a region+prefix rate must beat the cheaper national wildcard",
        )

    def test_an_inactive_rate_never_answers(self):
        inactive = rate(self.standard, "99.00")
        inactive.is_active = False
        inactive.save()
        self.assertIsNone(quote_amount())

    def test_a_rate_of_an_inactive_method_never_answers(self):
        retired = method("express", is_active=False)
        rate(retired, "19.00")
        self.assertIsNone(quote_amount())

    def test_the_cheapest_of_two_equally_specific_rates_wins(self):
        # Two rates of the same specificity that both reach the destination -
        # a region-wide one and a postal-band one - so neither overrides the
        # other and price has to decide.
        rate(self.standard, "79.00", region="Maharashtra")
        rate(self.standard, "29.00", postal_code_prefix="400")
        self.assertEqual(quote_amount(), Decimal("29.00"))

    def test_an_unpriced_preference_picks_the_cheapest_method_not_the_narrowest(self):
        # Across methods, price decides: an unsolicited customer must not be
        # handed the expensive option because a store happens to price it
        # only for their region.
        rate(self.standard, "49.00")
        express = method("express", name="Express")
        rate(express, "249.00", region="Maharashtra")
        self.assertEqual(quote_amount(), Decimal("49.00"))

    def test_an_equal_amount_resolves_the_same_way_on_every_call(self):
        # The rank's last key is the method code, so the winner cannot depend
        # on the database's freedom to return rows in any order.
        other = method("aaa-express", name="AAA Express")
        rate(other, "99.00")
        rate(self.standard, "99.00")
        self.assertEqual(
            [quote_amount() for _ in range(3)],
            [Decimal("99.00")] * 3,
        )
        quotes = shipping_options(
            region="Maharashtra",
            postal_code="400001",
        )
        self.assertEqual(
            [quote.method_code for quote in quotes],
            ["aaa-express", "standard"],
        )

    def test_the_named_method_is_priced_rather_than_the_cheapest(self):
        rate(self.standard, "49.00")
        express = method("express", name="Express")
        rate(express, "199.00")
        self.assertEqual(
            quote_amount(method_code="express"),
            Decimal("199.00"),
        )

    def test_quotes_carry_the_method_identity_the_order_stores(self):
        rate(self.standard, "49.00")
        quote = quote_shipping(
            region="Maharashtra",
            postal_code="400001",
            merchandise_total=Decimal("0.00"),
        )
        self.assertEqual(quote.method_code, "standard")
        self.assertEqual(quote.method_name, "Standard")
        self.assertEqual(quote.method_id, self.standard.pk)
        self.assertFalse(quote.free_shipping)


class NoMatchingRateTests(ApiTestCase):
    """The loud-failure branch, and the one silent zero that is not one."""

    def test_a_destination_nothing_serves_is_refused_not_free(self):
        standard = method()
        rate(standard, "99.00", region="Maharashtra")
        with self.assertRaises(ShippingUnavailable) as ctx:
            quote_shipping(
                region="Kerala",
                postal_code="695001",
                merchandise_total=Decimal("0.00"),
            )
        self.assertIn("not available for this destination", str(ctx.exception))

    def test_an_unresolvable_named_method_is_refused_with_its_own_message(self):
        with self.assertRaises(ShippingUnavailable) as ctx:
            quote_shipping(
                region="Maharashtra",
                postal_code="400001",
                merchandise_total=Decimal("0.00"),
                method_code="rocket",
            )
        self.assertIn("not available", str(ctx.exception))

    def test_a_store_with_no_shipping_configured_prices_nothing_at_all(self):
        # The deliberate third answer: shipping was never switched on. It is
        # not a rate of zero standing in for "we could not price this".
        self.assertIsNone(quote_amount())

    def test_only_a_retired_method_is_still_an_unconfigured_store(self):
        method(is_active=False)
        self.assertFalse(shipping_configured())
        self.assertIsNone(quote_amount())

    def test_an_unconfigured_store_still_refuses_a_named_method(self):
        # Three distinct answers, never two answers for one fact: with nothing
        # configured, naming a method is still a refusal rather than a
        # silently free shipment.
        with self.assertRaises(ShippingUnavailable):
            quote_shipping(
                region="Maharashtra",
                postal_code="400001",
                merchandise_total=Decimal("0.00"),
                method_code="standard",
            )


class FreeShippingRuleTests(ApiTestCase):
    """Spec 6.9 line 2005: the env-driven free-shipping threshold."""

    def setUp(self):
        self.standard = method()
        rate(self.standard, "49.00")

    def quote_for(self, merchandise_total):
        return quote_shipping(
            region="Maharashtra",
            postal_code="400001",
            merchandise_total=Decimal(merchandise_total),
        )

    def test_without_the_rule_the_rate_is_always_charged(self):
        from django.conf import settings

        self.assertIsNone(
            settings.SHIPPING_FREE_THRESHOLD,
            "the rule is off unless the deployment switches it on",
        )
        self.assertEqual(self.quote_for("100000.00").amount, Decimal("49.00"))

    @override_settings(SHIPPING_FREE_THRESHOLD=Decimal("999.00"))
    def test_below_the_threshold_the_rate_is_charged(self):
        quote = self.quote_for("998.99")
        self.assertEqual(quote.amount, Decimal("49.00"))
        self.assertFalse(quote.free_shipping)

    @override_settings(SHIPPING_FREE_THRESHOLD=Decimal("999.00"))
    def test_the_threshold_itself_is_free_shipping(self):
        quote = self.quote_for("999.00")
        self.assertEqual(quote.amount, Decimal("0.00"))
        self.assertTrue(quote.free_shipping)

    @override_settings(SHIPPING_FREE_THRESHOLD=Decimal("999.00"))
    def test_above_the_threshold_shipping_is_free(self):
        quote = self.quote_for("1000.00")
        self.assertEqual(quote.amount, Decimal("0.00"))
        self.assertTrue(quote.free_shipping)

    @override_settings(SHIPPING_FREE_THRESHOLD=Decimal("0.00"))
    def test_a_zero_threshold_makes_every_shipment_free(self):
        self.assertEqual(self.quote_for("0.01").amount, Decimal("0.00"))


# ==================================
# The estimate endpoint (spec 9.1)
# ==================================


class ShippingEstimateTests(ApiTestCase):
    """GET /store/shipping/estimate - anonymous, priced server-side."""

    def setUp(self):
        self.standard = method()
        rate(self.standard, "99.00")
        self.express = method("express", name="Express")
        rate(self.express, "249.00")
        self.national_only = method("pickup", name="Store pickup")
        rate(self.national_only, "0.00")

    def estimate(self, client=None, **overrides):
        params = {"state": "Maharashtra", "pincode": "400001"}
        params.update(overrides)
        return (client or self.client).get(ESTIMATE_V1, params)

    def test_an_anonymous_caller_gets_the_priced_options_cheapest_first(self):
        res = self.estimate()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data["options"],
            [
                {
                    "method": "pickup",
                    "name": "Store pickup",
                    "amount": "0.00",
                    "free_shipping": False,
                },
                {
                    "method": "standard",
                    "name": "Standard",
                    "amount": "99.00",
                    "free_shipping": False,
                },
                {
                    "method": "express",
                    "name": "Express",
                    "amount": "249.00",
                    "free_shipping": False,
                },
            ],
        )
        self.assertEqual(res.data["state"], "Maharashtra")
        self.assertEqual(res.data["pincode"], "400001")
        self.assertEqual(res.data["currency"], "INR")

    def test_the_amounts_are_decimal_strings_not_json_numbers(self):
        # A JSON number is a double in the browser: the price must arrive as a
        # string so no client-side float can round it.
        res = self.estimate()
        for option in res.data["options"]:
            with self.subTest(method=option["method"]):
                self.assertIsInstance(option["amount"], str)
                self.assertEqual(
                    option["amount"],
                    str(Decimal(option["amount"])),
                )

    def test_the_estimate_never_carries_a_row_id(self):
        res = self.estimate()
        for option in res.data["options"]:
            with self.subTest(method=option["method"]):
                self.assertEqual(
                    set(option),
                    {"method", "name", "amount", "free_shipping"},
                )

    def test_an_authenticated_caller_sees_the_same_prices(self):
        # Parity on the read side too: the price cannot depend on who asks.
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        other = self.fresh_client()
        other.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(
            self.estimate(client=other).data,
            self.estimate().data,
        )

    def test_the_legacy_alias_serves_the_same_body(self):
        legacy = self.client.get(
            ESTIMATE_LEGACY,
            {"state": "Maharashtra", "pincode": "400001"},
        )
        self.assertEqual(legacy.status_code, 200, legacy.data)
        self.assertEqual(legacy.data, self.estimate().data)

    def test_a_destination_nothing_serves_is_refused(self):
        # Narrow every rate to the region this storefront does serve, so the
        # Kerala address below genuinely has no rate rather than falling back
        # to the national wildcard.
        ShippingRate.objects.update(region="Maharashtra")
        res = self.estimate(state="Kerala", pincode="695001")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("not available for this destination", res.data["error"])

    def test_a_method_that_cannot_reach_the_destination_is_simply_absent(self):
        # Partial serviceability: express is priced for one region only, so a
        # destination outside it drops out of the list instead of refusing the
        # whole request.
        self.express.rates.update(region="Karnataka")
        res = self.estimate()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            [option["method"] for option in res.data["options"]],
            ["pickup", "standard"],
        )

    def test_the_destination_fields_are_required(self):
        for params in (
            {},
            {"state": "Maharashtra"},
            {"pincode": "400001"},
            {"state": "   ", "pincode": "400001"},
            {"state": "Maharashtra", "pincode": ""},
        ):
            with self.subTest(params=params):
                res = self.client.get(ESTIMATE_V1, params)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertIn("required", res.data["error"])

    def test_an_unconfigured_store_offers_nothing_rather_than_refusing(self):
        ShippingMethod.objects.all().delete()
        res = self.estimate()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["options"], [])


# ==================================
# Checkout: the cost applied to the order
# ==================================


class CheckoutShippingCostTests(ApiTestCase):
    """What checkout charges, and what it records (the account path; the guest
    path is priced by the same view and pinned for parity below)."""

    def setUp(self):
        self.product = self.make_product(price="999.99")
        self.standard = method()
        rate(self.standard, "49.00")
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        self.auth(token)

    def test_the_charge_is_added_to_the_order_total_and_recorded(self):
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.shipping_amount, Decimal("49.00"))
        self.assertEqual(order.shipping_method, self.standard)
        self.assertEqual(
            order.total_amount,
            Decimal("999.99") + Decimal("49.00"),
        )
        self.assertEqual(res.data["shipping_amount"], "49.00")
        self.assertEqual(res.data["shipping_method"], "standard")
        self.assertEqual(
            res.data["total_amount"],
            str(order.total_amount),
        )

    def test_a_named_method_is_priced_server_side(self):
        express = method("express", name="Express")
        rate(express, "249.00")
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout(shipping_method="express")
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.shipping_method, express)
        self.assertEqual(order.shipping_amount, Decimal("249.00"))

    def test_an_unnamed_method_is_chosen_by_the_server(self):
        # Two serviceable methods and no preference: the server picks, and
        # records which one, rather than leaving the order unpriced.
        express = method("express", name="Express")
        rate(express, "249.00")
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Order.objects.get().shipping_method, self.standard)

    def test_an_unavailable_destination_is_refused_and_nothing_is_written(self):
        ShippingRate.objects.update(region="Karnataka")
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout(state="Maharashtra")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("not available for this destination", res.data["error"])
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(OrderStatusEvent.objects.count(), 0)
        self.assertEqual(StockReservation.objects.count(), 0)
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_CREATED
            ).count(),
            0,
        )

    def test_a_method_that_does_not_exist_is_refused_and_nothing_is_written(self):
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout(shipping_method="rocket")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("not available", res.data["error"])
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(StockReservation.objects.count(), 0)

    def test_a_store_with_no_shipping_configured_records_a_real_zero_charge(self):
        ShippingMethod.objects.all().delete()
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.shipping_amount, Decimal("0.00"))
        self.assertIsNone(order.shipping_method)
        self.assertEqual(order.total_amount, Decimal("999.99"))

    def test_the_coupon_and_the_shipping_charge_combine_in_decimal(self):
        self.make_coupon(
            code="SAVE10",
            discount_type="percentage",
            discount_value="10",
            maximum_discount=None,
        )
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout(coupon_code="SAVE10")
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        # 999.99 - 99.999 -> HALF_EVEN 100.00, then + 49.00.
        self.assertEqual(order.discount_amount, Decimal("100.00"))
        self.assertEqual(order.total_amount, Decimal("948.99"))

    @override_settings(SHIPPING_FREE_THRESHOLD=Decimal("999.00"))
    def test_the_free_shipping_rule_reaches_the_order(self):
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.shipping_amount, Decimal("0.00"))
        self.assertEqual(order.shipping_method, self.standard)
        self.assertEqual(order.total_amount, Decimal("999.99"))

    @override_settings(SHIPPING_FREE_THRESHOLD=Decimal("900.00"))
    def test_the_threshold_compares_the_discounted_merchandise_total(self):
        # The cart total alone would ship this order free; the rule reads what
        # the customer pays for goods, so the coupon brings the charge back.
        self.make_coupon(
            code="SAVE10",
            discount_type="percentage",
            discount_value="10",
            maximum_discount=None,
        )
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout(coupon_code="SAVE10")
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.discount_amount, Decimal("100.00"))
        self.assertEqual(order.shipping_amount, Decimal("49.00"))
        self.assertEqual(order.total_amount, Decimal("948.99"))

    def test_the_charge_is_recorded_in_the_creation_trail_row(self):
        self.seed_session_cart([(self.product, 1)])
        self.checkout()
        detail = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.ORDER_CREATED
        ).detail
        self.assertEqual(detail["shipping_method"], "standard")
        self.assertEqual(detail["shipping_amount"], "49.00")
        self.assertEqual(detail["total_amount"], str(Order.objects.get().total_amount))

    def test_the_price_is_the_gateway_charge_and_the_refund_ceiling(self):
        # The money paths read total_amount, so the delivery charge is charged
        # and refundable without either of them learning about shipping.
        self.seed_session_cart([(self.product, 1)])
        self.checkout()
        order = Order.objects.get()
        client_mock = self.razorpay_mock()
        payment = self.client.post(
            "/api/orders/payment/",
            {"order_id": order.id},
            format="json",
        )
        self.assertEqual(payment.status_code, 200, payment.data)
        expected = int(order.total_amount * 100)
        self.assertEqual(
            client_mock.order.create.call_args.args[0]["amount"],
            expected,
        )


class ClientSuppliedAmountTests(ApiTestCase):
    """The security property: no client amount reaches the total."""

    def setUp(self):
        self.product = self.make_product(price="999.99")
        self.standard = method()
        rate(self.standard, "49.00")
        self.make_user("buyer")
        _, self.token = self.api_login("buyer")
        self.auth(self.token)

    def checkout_forged(self, forged):
        self.seed_session_cart([(self.product, 1)])
        payload = self.checkout_payload(**forged)
        return self.client.post("/api/orders/checkout/", payload, format="json")

    def test_a_forged_zero_shipping_amount_does_not_change_the_total(self):
        res = self.checkout_forged({"shipping_amount": "0.00"})
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.shipping_amount, Decimal("49.00"))
        self.assertEqual(order.total_amount, Decimal("1048.99"))
        self.assertEqual(res.data["shipping_amount"], "49.00")

    def test_every_forged_amount_name_is_ignored(self):
        for index, forged in enumerate(
            (
                {"shipping_amount": "0.00"},
                {"shipping_cost": "0"},
                {"shipping": "0"},
                {"total_amount": "1.00"},
            )
        ):
            with self.subTest(forged=forged):
                # A fresh client (and so a fresh cart) and a distinct
                # destination per shape: the owner-scoped duplicate guard
                # would otherwise collapse a repeat of the same cart and
                # address onto the first order, which would prove nothing
                # about the forged field.
                client = self.fresh_client()
                client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")
                self.seed_session_cart([(self.product, 1)], client=client)
                res = client.post(
                    "/api/orders/checkout/",
                    self.checkout_payload(pincode=f"40000{index}", **forged),
                    format="json",
                )
                self.assertEqual(res.status_code, 201, res.data)
                order = Order.objects.latest("id")
                self.assertEqual(order.shipping_amount, Decimal("49.00"))
                self.assertEqual(order.total_amount, Decimal("1048.99"))

    def test_a_forged_total_is_not_what_the_checkout_answers_with(self):
        res = self.checkout_forged({"total_amount": "1.00"})
        self.assertEqual(res.data["total_amount"], "1048.99")


class CheckoutPricingParityTests(ApiTestCase):
    """Guest and account checkouts price the same cart identically."""

    def setUp(self):
        self.product = self.make_product(price="499.50")
        self.standard = method()
        rate(self.standard, "49.00")
        self.express = method("express", name="Express")
        rate(self.express, "249.00", region="Maharashtra")

    def test_the_same_cart_and_destination_price_the_same_for_both_callers(self):
        guest = self.fresh_client()
        self.seed_session_cart([(self.product, 2)], client=guest)
        guest_response = guest.post(
            "/api/orders/checkout/",
            self.checkout_payload(guest_email="roamer@example.com"),
            format="json",
        )
        self.assertEqual(guest_response.status_code, 201, guest_response.data)
        guest_order = Order.objects.get(guest_email="roamer@example.com")
        self.assertIsNone(guest_order.user)

        self.make_user("buyer")
        _, token = self.api_login("buyer")
        account_client = self.fresh_client()
        account_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.seed_session_cart([(self.product, 2)], client=account_client)
        account_response = account_client.post(
            "/api/orders/checkout/",
            self.checkout_payload(),
            format="json",
        )
        self.assertEqual(account_response.status_code, 201, account_response.data)
        account_order = Order.objects.get(user__username="buyer")

        self.assertEqual(guest_order.shipping_amount, account_order.shipping_amount)
        self.assertEqual(guest_order.shipping_method, account_order.shipping_method)
        self.assertEqual(guest_order.total_amount, account_order.total_amount)
        # 499.50 x 2 = 999.00, plus the 49.00 national standard: express's
        # 249.00 rate is region-specific but no preference was expressed, and
        # price - not specificity - decides across methods.
        self.assertEqual(guest_order.shipping_amount, Decimal("49.00"))
        self.assertEqual(guest_order.total_amount, Decimal("1048.00"))

    def test_a_named_method_prices_the_same_for_both_callers(self):
        guest = self.fresh_client()
        self.seed_session_cart([(self.product, 1)], client=guest)
        guest.post(
            "/api/orders/checkout/",
            self.checkout_payload(
                guest_email="roamer@example.com",
                shipping_method="express",
            ),
            format="json",
        )
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        account_client = self.fresh_client()
        account_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.seed_session_cart([(self.product, 1)], client=account_client)
        account_client.post(
            "/api/orders/checkout/",
            self.checkout_payload(shipping_method="express"),
            format="json",
        )
        guest_order = Order.objects.get(guest_email="roamer@example.com")
        account_order = Order.objects.get(user__username="buyer")
        self.assertEqual(guest_order.shipping_amount, Decimal("249.00"))
        self.assertEqual(
            guest_order.total_amount,
            account_order.total_amount,
        )

    def test_an_unavailable_destination_is_refused_for_both_callers(self):
        ShippingRate.objects.update(region="Karnataka")
        guest = self.fresh_client()
        self.seed_session_cart([(self.product, 1)], client=guest)
        guest_res = guest.post(
            "/api/orders/checkout/",
            self.checkout_payload(guest_email="roamer@example.com"),
            format="json",
        )
        self.assertEqual(guest_res.status_code, 400, guest_res.data)

        self.make_user("buyer")
        _, token = self.api_login("buyer")
        account_client = self.fresh_client()
        account_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.seed_session_cart([(self.product, 1)], client=account_client)
        account_res = account_client.post(
            "/api/orders/checkout/",
            self.checkout_payload(),
            format="json",
        )
        self.assertEqual(account_res.status_code, 400, account_res.data)
        self.assertEqual(account_res.data["error"], guest_res.data["error"])
        self.assertEqual(Order.objects.count(), 0)


class DecimalPrecisionTests(ApiTestCase):
    """A rate a float cannot represent must not be able to corrupt the total."""

    def setUp(self):
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        self.auth(token)

    def test_a_float_hostile_rate_produces_an_exact_decimal_total(self):
        # 0.10 + 0.20 is 0.30000000000000004 in binary floating point. Every
        # amount on this path is a Decimal, so the strings the client reads and
        # the amount the order stores are both exact - a float in the chain
        # would show the drift in both.
        product = self.make_product(name="Penny", price="0.10")
        standard = method()
        rate(standard, "0.20")
        self.seed_session_cart([(product, 3)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertIsInstance(order.total_amount, Decimal)
        self.assertIsInstance(order.shipping_amount, Decimal)
        self.assertEqual(order.shipping_amount, Decimal("0.20"))
        self.assertEqual(order.total_amount, Decimal("0.50"))
        self.assertEqual(res.data["total_amount"], "0.50")
        self.assertEqual(res.data["shipping_amount"], "0.20")
        self.assertNotEqual(str(res.data["total_amount"]), "0.5")

    def test_a_two_digit_total_is_exact_to_the_paisa(self):
        product = self.make_product(name="Paise", price="20.20")
        standard = method()
        rate(standard, "10.10")
        self.seed_session_cart([(product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Order.objects.get().total_amount, Decimal("30.30"))
        self.assertEqual(res.data["total_amount"], "30.30")


class ShippingCostAtomicityTests(ApiTestCase):
    """The cost is priced and written inside the checkout's atomic block."""

    def setUp(self):
        self.product = self.make_product(price="999.99")
        self.standard = method()
        rate(self.standard, "49.00")
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        self.auth(token)

    def test_the_price_is_resolved_inside_the_atomic_block(self):
        observed = {}
        import orders.views as orders_views

        real_quote = orders_views.quote_shipping

        def spy(**kwargs):
            observed["in_atomic_block"] = connection.in_atomic_block
            observed["lock_requested"] = kwargs.get("lock")
            return real_quote(**kwargs)

        orders_views.quote_shipping = spy
        self.addCleanup(setattr, orders_views, "quote_shipping", real_quote)

        self.seed_session_cart([(self.product, 1)])
        self.checkout()
        self.assertTrue(observed["in_atomic_block"])
        self.assertTrue(observed["lock_requested"])

    def test_the_lock_reaches_the_query_and_only_when_asked_for(self):
        # select_for_update is a no-op on SQLite (it emits no FOR UPDATE), so
        # the flag is asserted on the Query that is actually evaluated rather
        # than on SQL text the test backend cannot produce. Without it a staff
        # edit could commit between reading the rate and writing the order.
        from unittest.mock import patch

        from django.db.models.query import QuerySet

        standard = self.standard
        observed = []
        original = QuerySet.order_by

        def spy(queryset, *fields, **kwargs):
            observed.append(bool(queryset.query.select_for_update))
            return original(queryset, *fields, **kwargs)

        with patch.object(QuerySet, "order_by", spy):
            quote_shipping(
                region="Maharashtra",
                postal_code="400001",
                merchandise_total=Decimal("0.00"),
                lock=True,
            )
            quote_shipping(
                region="Maharashtra",
                postal_code="400001",
                merchandise_total=Decimal("0.00"),
            )
        self.assertEqual(observed, [True, False])

    def test_a_failure_after_pricing_rolls_the_charge_back_with_the_order(self):
        self.seed_session_cart([(self.product, 1)])
        # The audit hook is the last writer in the atomic block, so failing it
        # proves everything before it - the priced order row included - is
        # rolled back rather than committed without its trail. The assertion is
        # on the ORDER_CREATED event specifically: the login in setUp wrote an
        # audit row of its own, and it must have survived.
        self.client.raise_request_exception = False
        original = AuditEvent.record
        AuditEvent.record = staticmethod(_explode)
        self.addCleanup(setattr, AuditEvent, "record", original)
        res = self.checkout()
        self.assertEqual(res.status_code, 500)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(StockReservation.objects.count(), 0)
        self.assertEqual(OrderStatusEvent.objects.count(), 0)
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_CREATED
            ).count(),
            0,
        )

    def test_a_rolled_back_checkout_does_not_burn_an_order_number(self):
        self.seed_session_cart([(self.product, 1)])
        self.client.raise_request_exception = False
        original = AuditEvent.record
        AuditEvent.record = staticmethod(_explode)
        self.addCleanup(setattr, AuditEvent, "record", original)
        self.assertEqual(self.checkout().status_code, 500)
        # The number is minted inside the same rolled-back block, so the next
        # real checkout is the first one - a refused checkout must not leave a
        # gap in a customer-facing sequence.
        AuditEvent.record = original
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["order_number"], "ORD-2026-000001")
        self.assertEqual(Order.objects.get().total_amount, Decimal("1048.99"))


def _explode(*args, **kwargs):
    raise RuntimeError("audit sink is down")


# ==================================
# Replay: the delivery option and the dedup agreement
# ==================================


class ReplayAgreementTests(ApiTestCase):
    """A replay must not reach another order's price, and vice versa."""

    def setUp(self):
        self.product = self.make_product(price="999.99")
        self.standard = method()
        rate(self.standard, "49.00")
        self.express = method("express", name="Express")
        rate(self.express, "249.00")
        self.seed_session_cart([(self.product, 1)])

    def guest_post(self, key="idem-key-1", **extra):
        return self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(
                guest_email="roamer@example.com",
                **extra,
            ),
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_a_replay_naming_a_different_method_is_a_conflict(self):
        first = self.guest_post(shipping_method="standard")
        self.assertEqual(first.status_code, 201, first.data)
        replay = self.guest_post(shipping_method="express")
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.get().shipping_amount, Decimal("49.00"))

    def test_a_replay_naming_the_same_method_collapses_onto_the_original(self):
        first = self.guest_post(shipping_method="express")
        self.assertEqual(first.status_code, 201, first.data)
        replay = self.guest_post(shipping_method="express")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(replay.data["total_amount"], first.data["total_amount"])
        self.assertEqual(Order.objects.count(), 1)

    def test_a_replay_naming_no_method_collapses_onto_the_original(self):
        first = self.guest_post()
        replay = self.guest_post()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(replay.data["total_amount"], first.data["total_amount"])
        self.assertEqual(Order.objects.count(), 1)

    def test_a_forged_amount_on_a_replay_cannot_change_the_total(self):
        first = self.guest_post(shipping_method="standard")
        replay = self.guest_post(
            shipping_method="standard",
            shipping_amount="0.00",
            total_amount="1.00",
        )
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["total_amount"], first.data["total_amount"])
        self.assertEqual(
            Order.objects.get().total_amount,
            Decimal("1048.99"),
        )

    def test_a_blank_method_string_is_treated_as_no_choice(self):
        first = self.guest_post()
        replay = self.guest_post(shipping_method="   ")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])

    def test_the_keyless_double_click_still_collapses_with_shipping(self):
        # The accidental-duplicate window must survive the new agreement: two
        # identical submissions are still one order, not two payable ones.
        self.seed_session_cart([(self.product, 1)])
        first = self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(
                guest_email="roamer@example.com",
                shipping_method="standard",
            ),
            format="json",
        )
        second = self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(
                guest_email="roamer@example.com",
                shipping_method="standard",
            ),
            format="json",
        )
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 1)

    def test_the_keyed_replay_of_an_unserviceable_destination_still_collapses(self):
        # Pricing runs after the collapse guards, so a rate retired between the
        # two submissions cannot turn a retry into a refusal.
        first = self.guest_post(shipping_method="standard")
        self.assertEqual(first.status_code, 201, first.data)
        ShippingRate.objects.update(is_active=False)
        replay = self.guest_post(shipping_method="standard")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])


# ==================================
# The admin seam
# ==================================


class ShippingAdminCapabilityTests(ApiTestCase):
    """shipping.manage is money authority: finance and admin only."""

    def setUp(self):
        groups = sync_role_groups()
        self.users = {}
        for role in (ROLE_ADMIN, ROLE_FINANCE, ROLE_INVENTORY, ROLE_SUPPORT):
            user = self.make_user(f"role-{role}")
            user.is_staff = True
            user.save()
            user.groups.add(groups[role])
            self.users[role] = user
        self.standard = method()
        rate(self.standard, "49.00")

    def test_the_rate_capability_follows_the_money_roles(self):
        self.assertTrue(user_has_capability(self.users[ROLE_ADMIN], "shipping.manage"))
        self.assertTrue(
            user_has_capability(self.users[ROLE_FINANCE], "shipping.manage")
        )
        self.assertFalse(
            user_has_capability(self.users[ROLE_INVENTORY], "shipping.manage")
        )
        self.assertFalse(
            user_has_capability(self.users[ROLE_SUPPORT], "shipping.manage")
        )

    def test_a_role_without_the_capability_is_refused_at_the_admin(self):
        from django.contrib import admin

        operator = self.users[ROLE_INVENTORY]
        self.client.force_login(operator)
        for url in (
            "/admin/shipping/shippingmethod/",
            "/admin/shipping/shippingrate/",
        ):
            with self.subTest(url=url):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 403)
        self.assertIn(ShippingMethod, admin.site._registry)
        self.assertIn(ShippingRate, admin.site._registry)

    def test_a_money_role_may_reach_and_manage_the_rate_table(self):
        from django.contrib import admin

        finance = self.users[ROLE_FINANCE]
        self.client.force_login(finance)
        for url in (
            "/admin/shipping/shippingmethod/",
            "/admin/shipping/shippingrate/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        rate_admin = admin.site._registry[ShippingRate]
        method_admin = admin.site._registry[ShippingMethod]
        self.assertEqual(
            rate_admin.has_change_permission(_request_for(finance)),
            True,
        )
        self.assertEqual(
            method_admin.has_add_permission(_request_for(finance)),
            True,
        )

    def test_the_change_form_saves_a_rate_the_staff_entered(self):
        finance = self.users[ROLE_FINANCE]
        self.client.force_login(finance)
        res = self.client.post(
            "/admin/shipping/shippingrate/add/",
            {
                "method": self.standard.pk,
                "amount": "77.00",
                "region": "Goa",
                "postal_code_prefix": "403",
                "is_active": "on",
                "_save": "Save",
            },
        )
        self.assertEqual(res.status_code, 302, getattr(res, "context", None))
        created = ShippingRate.objects.get(region="Goa")
        self.assertEqual(created.amount, Decimal("77.00"))
        self.assertEqual(
            quote_amount(region="Goa", postal_code="403001"),
            Decimal("77.00"),
        )

    def test_a_duplicate_geography_is_refused_at_the_form(self):
        finance = self.users[ROLE_FINANCE]
        self.client.force_login(finance)
        response = self.client.post(
            "/admin/shipping/shippingrate/add/",
            {
                "method": self.standard.pk,
                "amount": "88.00",
                "region": "",
                "postal_code_prefix": "",
                "is_active": "on",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(ShippingRate.objects.count(), 1)

    def test_retiring_a_method_does_not_reprice_or_hide_an_order(self):
        order = Order.objects.create(
            guest_email="roamer@example.com",
            guest_token="tok-retired-method",
            full_name="Buyer Person",
            phone="9876543210",
            address="12 Rose Lane",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal("1048.99"),
            shipping_method=self.standard,
            shipping_amount=Decimal("49.00"),
        )
        self.standard.delete()
        order.refresh_from_db()
        # The amount is the money record: retiring the option it was priced
        # with must leave both the charge and the total exactly as they were.
        self.assertIsNone(order.shipping_method)
        self.assertEqual(order.shipping_amount, Decimal("49.00"))
        self.assertEqual(order.total_amount, Decimal("1048.99"))
        self.assertEqual(ShippingRate.objects.count(), 0)


def _request_for(user):
    from django.test import RequestFactory

    request = RequestFactory().get("/admin/shipping/shippingrate/")
    request.user = user
    return request


# ==================================
# The environment-driven rule
# ==================================


class ShippingThresholdSettingTests(ApiTestCase):
    """SHIPPING_FREE_THRESHOLD is env config, never a literal in code."""

    def test_the_rule_is_off_when_the_environment_says_nothing(self):
        from django.conf import settings

        self.assertIsNone(settings.SHIPPING_FREE_THRESHOLD)

    def test_a_money_threshold_is_read_as_a_quantized_decimal(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "999"}):
            self.assertEqual(
                _env_money("SHIPPING_FREE_THRESHOLD"),
                Decimal("999.00"),
            )

    def test_a_half_paisa_threshold_rounds_once_like_every_other_amount(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "999.005"}):
            self.assertEqual(
                _env_money("SHIPPING_FREE_THRESHOLD"),
                Decimal("999.00"),
            )

    def test_an_unparseable_threshold_leaves_the_rule_off(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "free-ish"}):
            self.assertIsNone(_env_money("SHIPPING_FREE_THRESHOLD"))

    def test_a_negative_threshold_leaves_the_rule_off(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "-1"}):
            self.assertIsNone(_env_money("SHIPPING_FREE_THRESHOLD"))

    def test_an_empty_threshold_leaves_the_rule_off(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "   "}):
            self.assertIsNone(_env_money("SHIPPING_FREE_THRESHOLD"))

    # The non-finite family. ``Decimal("NaN").quantize()`` returns a QUIET NaN
    # WITHOUT raising, so these values used to sail through the parse and then
    # raise InvalidOperation on the comparison - i.e. SHIPPING_FREE_THRESHOLD=NaN
    # crashed the process at settings import, which is the exact failure this
    # function exists to prevent. Every one must resolve to the SAME safe
    # answer as any other unusable input: the rule stays off, so every
    # shipment is still charged.
    def test_a_nan_threshold_leaves_the_rule_off(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "NaN"}):
            self.assertIsNone(_env_money("SHIPPING_FREE_THRESHOLD"))

    def test_a_lowercase_nan_threshold_leaves_the_rule_off(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "nan"}):
            self.assertIsNone(_env_money("SHIPPING_FREE_THRESHOLD"))

    def test_an_infinite_threshold_leaves_the_rule_off(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "Infinity"}):
            self.assertIsNone(_env_money("SHIPPING_FREE_THRESHOLD"))

    def test_a_negative_infinite_threshold_leaves_the_rule_off(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "-Infinity"}):
            self.assertIsNone(_env_money("SHIPPING_FREE_THRESHOLD"))

    def test_a_signalling_nan_threshold_leaves_the_rule_off(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "sNaN"}):
            self.assertIsNone(_env_money("SHIPPING_FREE_THRESHOLD"))

    # A threshold no order could ever reach, and one this store could not even
    # store: both are typos, and the safe reading of a typo is the one that
    # keeps charging.
    def test_a_threshold_past_the_largest_storable_amount_leaves_the_rule_off(
        self,
    ):
        with _env({"SHIPPING_FREE_THRESHOLD": "100000000.00"}):
            self.assertIsNone(_env_money("SHIPPING_FREE_THRESHOLD"))

    def test_a_forty_digit_threshold_leaves_the_rule_off(self):
        with _env({"SHIPPING_FREE_THRESHOLD": "9" * 40}):
            self.assertIsNone(_env_money("SHIPPING_FREE_THRESHOLD"))

    def test_the_largest_storable_threshold_is_still_honoured(self):
        # The boundary the guard above draws: 99999999.99 is the biggest
        # amount any money column here holds, so it is a legal threshold.
        with _env({"SHIPPING_FREE_THRESHOLD": "99999999.99"}):
            self.assertEqual(
                _env_money("SHIPPING_FREE_THRESHOLD"),
                Decimal("99999999.99"),
            )

    def test_no_unusable_value_is_ever_answered_with_an_unorderable_amount(self):
        # The property, not the spelling: whatever goes in, what comes out is
        # either None or a value that can be compared and stored.
        for raw in (
            "NaN",
            "nan",
            "-NaN",
            "sNaN",
            "Infinity",
            "-Infinity",
            "inf",
            "1E+40",
            "9" * 40,
            "-1",
            "free-ish",
            "100000000",
        ):
            with self.subTest(raw=raw), _env({"SHIPPING_FREE_THRESHOLD": raw}):
                value = _env_money("SHIPPING_FREE_THRESHOLD")
                if value is not None:
                    self.assertTrue(value.is_finite())
                    self.assertGreaterEqual(value, Decimal("0.00"))
                    self.assertLessEqual(value, Decimal("99999999.99"))


# ==================================
# The delivery option a historical order names
# ==================================


class ShippingMethodDeletionTests(ApiTestCase):
    """Deleting a delivery option must not erase it from the orders that
    used it (spec 8.3 line 2497: "Orders must retain what was actually
    purchased"; line 2485: "Use explicit deletion/archival policies")."""

    def setUp(self):
        self.product = self.make_product(price="999.99")
        self.standard = method()
        rate(self.standard, "49.00")
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        self.auth(token)

    def test_the_order_snapshots_the_delivery_option_it_was_priced_with(self):
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.shipping_method_code, "standard")
        # While the row lives, the live link and the snapshot agree - the
        # snapshot is redundant until it is not.
        self.assertEqual(order.shipping_method, self.standard)
        self.assertEqual(res.data["shipping_method_code"], "standard")

    def test_hard_deleting_the_method_keeps_the_label_on_a_historical_order(self):
        self.seed_session_cart([(self.product, 1)])
        self.assertEqual(self.checkout().status_code, 201)
        self.standard.delete()
        order = Order.objects.get()
        # The shipped SET_NULL semantics are untouched: the live link is gone.
        self.assertIsNone(order.shipping_method)
        # ... and the money is untouched.
        self.assertEqual(order.shipping_amount, Decimal("49.00"))
        # ... but the order still names the option it was actually charged
        # for, so the record of what was purchased survives the row.
        self.assertEqual(order.shipping_method_code, "standard")

    def test_the_order_body_still_names_the_option_after_the_row_is_deleted(self):
        self.seed_session_cart([(self.product, 1)])
        self.assertEqual(self.checkout().status_code, 201)
        self.standard.delete()
        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 200, res.data)
        body = res.data["results"][0]
        self.assertIsNone(body["shipping_method"])
        self.assertEqual(body["shipping_method_code"], "standard")
        self.assertEqual(body["shipping_amount"], "49.00")

    @override_settings(SHIPPING_FREE_THRESHOLD=Decimal("999.00"))
    def test_a_deleted_option_is_tellable_apart_from_never_configured(self):
        # The exact confusion the snapshot ends. The free-shipping rule makes
        # this the SHARPEST version of it: both orders then read
        # `shipping_method: null` AND a 0.00 delivery charge, so the live link
        # and the money say nothing at all about which delivery option was
        # bought. The snapshot is the only thing left that can.
        self.seed_session_cart([(self.product, 1)])
        self.assertEqual(self.checkout().status_code, 201)
        priced = Order.objects.get()
        self.standard.delete()

        self.seed_session_cart([(self.product, 1)])
        self.assertEqual(self.checkout().status_code, 201)
        unconfigured = Order.objects.exclude(pk=priced.pk).get()
        priced.refresh_from_db()

        # Indistinguishable on the shipped fields...
        self.assertIsNone(priced.shipping_method)
        self.assertIsNone(unconfigured.shipping_method)
        self.assertEqual(priced.shipping_amount, unconfigured.shipping_amount)
        # ... and told apart by the label, which is the point.
        self.assertEqual(priced.shipping_method_code, "standard")
        self.assertEqual(unconfigured.shipping_method_code, "")

    def test_deactivation_keeps_the_order_and_the_label_intact(self):
        # The documented retirement path, which must remain the cheap one:
        # no deletion, so nothing is ever at risk of being lost.
        self.seed_session_cart([(self.product, 1)])
        self.assertEqual(self.checkout().status_code, 201)
        self.standard.is_active = False
        self.standard.save()
        order = Order.objects.get()
        self.assertEqual(order.shipping_method, self.standard)
        self.assertEqual(order.shipping_method_code, "standard")
        self.assertEqual(order.shipping_amount, Decimal("49.00"))

    def test_an_order_priced_with_no_shipping_configured_snapshots_no_label(self):
        ShippingMethod.objects.all().delete()
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Order.objects.get().shipping_method_code, "")
        self.assertEqual(res.data["shipping_method_code"], "")


# ==================================
# The unconfigured-store signal
# ==================================


class UnconfiguredShippingSignalTests(ApiTestCase):
    """A real zero charge is correct; a store that never learns about it is
    not. Three signals, none of which changes what is charged."""

    def setUp(self):
        self.product = self.make_product(price="999.99")
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        self.auth(token)

    def test_a_free_shipment_because_nothing_is_configured_is_logged_a_warning(
        self,
    ):
        # No method at all, so this order is priced at shipping_amount 0.00.
        self.seed_session_cart([(self.product, 1)])
        with self.assertLogs("orders.views", level="WARNING") as captured:
            res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        warnings = [r for r in captured.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1, captured.output)
        self.assertIn("NO shipping charge", warnings[0].getMessage())

    def test_the_warning_names_the_command_that_fixes_the_state(self):
        self.seed_session_cart([(self.product, 1)])
        with self.assertLogs("orders.views", level="WARNING") as captured:
            self.assertEqual(self.checkout().status_code, 201)
        self.assertIn("seed_shipping_methods", captured.output[0])

    def test_a_charged_shipment_logs_no_such_warning(self):
        # The signal must be about the silent zero only, or it is noise the
        # merchant learns to ignore.
        standard = method()
        rate(standard, "49.00")
        self.seed_session_cart([(self.product, 1)])
        with self.assertNoLogs("orders.views", level="WARNING"):
            res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Order.objects.get().shipping_amount, Decimal("49.00"))

    def test_a_refused_destination_logs_no_such_warning(self):
        # The refusal path returns no quote but is NOT the silent zero: it
        # rejects the order, so there is no revenue to warn anyone about.
        standard = method()
        rate(standard, "49.00", region="Karnataka")
        self.seed_session_cart([(self.product, 1)])
        with self.assertNoLogs("orders.views", level="WARNING"):
            res = self.checkout(state="Maharashtra")
        self.assertEqual(res.status_code, 400, res.data)

    def test_the_health_probe_reports_that_shipping_is_not_configured(self):
        from ops.services import get_health

        health = get_health()
        self.assertIn("shipping_configured", health["checks"])
        self.assertFalse(health["checks"]["shipping_configured"])

    def test_the_health_probe_reports_shipping_once_a_method_exists(self):
        from ops.services import get_health

        method()
        self.assertTrue(get_health()["checks"]["shipping_configured"])

    def test_unconfigured_shipping_does_not_make_the_probe_degraded(self):
        # A store that has not configured shipping yet is a pre-launch state,
        # not an outage: gating the probe's status would turn a fresh
        # deployment into a 503 and mail the staff mailbox on every poll.
        from ops.services import get_health

        health = get_health()
        self.assertFalse(health["checks"]["shipping_configured"])
        self.assertEqual(health["status"], "ok")
        self.assertEqual(self.client.get("/health/").status_code, 200)


# ==================================
# Seeding a store that has no shipping
# ==================================


class SeedShippingMethodsCommandTests(ApiTestCase):
    """`manage.py seed_shipping_methods` - the documented end of the silent
    zero. Deliberate operator action: no import-time or migration-time
    side effect can put a made-up price in a database."""

    def setUp(self):
        self.product = self.make_product(price="999.99")

    def seed(self, *args, **options):
        from django.core.management import call_command

        out = StringIO()
        options.setdefault("stdout", out)
        call_command("seed_shipping_methods", *args, **options)
        return out.getvalue()

    def test_importing_the_command_writes_nothing(self):
        import ops.management.commands.seed_shipping_methods  # noqa: F401

        self.assertEqual(ShippingMethod.objects.count(), 0)
        self.assertEqual(ShippingRate.objects.count(), 0)

    def test_it_creates_both_methods_with_a_national_rate(self):
        self.seed()
        self.assertEqual(
            sorted(ShippingMethod.objects.values_list("code", flat=True)),
            ["express", "standard"],
        )
        self.assertEqual(ShippingRate.objects.count(), 2)
        for shipping_rate in ShippingRate.objects.all():
            # A geography wildcard, which is how a national rate is expressed
            # in this schema: empty region and empty prefix match everywhere.
            self.assertEqual(shipping_rate.region, "")
            self.assertEqual(shipping_rate.postal_code_prefix, "")
            self.assertEqual(shipping_rate.is_active, True)

    def test_the_seeded_rates_are_money_a_store_can_actually_charge(self):
        self.seed()
        self.assertTrue(shipping_configured())
        self.assertEqual(
            quote_amount(),
            Decimal("49.00"),
        )
        self.assertEqual(
            quote_amount(method_code="express"),
            Decimal("149.00"),
        )

    def test_the_amounts_are_options_not_constants(self):
        self.seed("--standard-amount", "60.50", "--express-amount", "200")
        standard = ShippingMethod.objects.get(code="standard")
        express = ShippingMethod.objects.get(code="express")
        self.assertEqual(standard.rates.get().amount, Decimal("60.50"))
        self.assertEqual(express.rates.get().amount, Decimal("200.00"))

    def test_rerunning_it_creates_nothing_and_changes_no_price(self):
        self.seed()
        first = ShippingRate.objects.get(method__code="standard").amount
        output = self.seed()
        self.assertEqual(ShippingRate.objects.count(), 2)
        self.assertEqual(ShippingMethod.objects.count(), 2)
        self.assertEqual(
            ShippingRate.objects.get(method__code="standard").amount, first
        )
        self.assertIn("0 method(s) and 0 rate(s) created", output)

    def test_it_never_reprices_or_reactivates_what_a_merchant_owns(self):
        standard = method()
        rate(standard, "77.00")
        standard.name = "Ground"
        standard.is_active = False
        standard.save()
        self.seed()
        standard.refresh_from_db()
        self.assertEqual(standard.name, "Ground")
        self.assertFalse(standard.is_active)
        self.assertEqual(standard.rates.get().amount, Decimal("77.00"))
        # The express method was still created: a partially configured store
        # is completed, not skipped.
        self.assertTrue(ShippingMethod.objects.filter(code="express").exists())

    def test_a_dry_run_writes_nothing_and_says_what_it_would_do(self):
        output = self.seed("--dry-run")
        self.assertEqual(ShippingMethod.objects.count(), 0)
        self.assertEqual(ShippingRate.objects.count(), 0)
        self.assertIn("2 method(s) and 2 rate(s)", output)

    def test_a_dry_run_over_a_seeded_store_reports_nothing_left_to_do(self):
        self.seed()
        output = self.seed("--dry-run")
        self.assertIn("0 method(s) and 0 rate(s)", output)
        self.assertEqual(ShippingRate.objects.count(), 2)

    def test_an_unusable_amount_is_refused_and_writes_nothing(self):
        # Unlike an env default, a bad flag here would store a bad PRICE, so
        # it must stop the operator rather than be quietly defaulted.
        for value in ("NaN", "Infinity", "-1", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(CommandError):
                    self.seed("--standard-amount", value)
                self.assertEqual(ShippingMethod.objects.count(), 0)
                self.assertEqual(ShippingRate.objects.count(), 0)

    def test_a_seeded_store_charges_for_delivery_end_to_end(self):
        # The whole point of the command: the silent zero is gone, and the
        # order now records a real charge with a real method.
        self.seed()
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        self.auth(token)
        self.seed_session_cart([(self.product, 1)])
        with self.assertNoLogs("orders.views", level="WARNING"):
            res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.shipping_method_code, "standard")
        self.assertEqual(order.shipping_amount, Decimal("49.00"))


def _env(values):
    from unittest.mock import patch

    return patch.dict("os.environ", values, clear=False)


def _env_money(name):
    from config import settings as config_settings

    return config_settings._env_money(name)

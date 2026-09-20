"""Orders unit tests - docs/test-gaps.md items 30-39.

Razorpay is always mocked (``self.razorpay_mock``); no test touches the
network or the real keys from ``.env`` (V-01 containment).
"""
import os
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings, tag
from django.utils import timezone
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle

from cart.models import Cart, CartItem
from common.models import AuditEvent
from common.roles import ROLE_FINANCE, ROLE_SUPPORT
from common.testing import TEST_RAZORPAY_KEY_ID, ApiTestCase
from config.settings import _env_currency
from orders.admin import OrderAdmin
from orders.models import Coupon, Order, OrderItem, OrderStatusEvent
from orders.serializers import OrderItemSerializer, OrderSerializer
from orders import state as order_state
from orders.views import apply_coupon
from products.models import StockMovement, products


class OrderTestBase(ApiTestCase):
    """Shared fixtures: a logged-in buyer with a seeded session cart."""

    def setUp(self):
        self.buyer = self.make_user("buyer")
        _, self.token = self.api_login("buyer")
        self.product = self.make_product(name="Rose Aurum", price="500.00", stock=10, category="Floral")
        self.cart_data = self.seed_session_cart([(self.product, 2)])  # subtotal 1000.00

    def create_order(self, coupon=None, product=None, quantity=2):
        payload = {}
        if coupon is not None:
            payload["coupon_code"] = coupon.code
        res = self.checkout(**payload)
        self.assertEqual(res.status_code, 201, res.data)
        return Order.objects.get(id=res.data["id"])


@tag("orders")
class CurrencyStoreConfigTests(OrderTestBase):
    """[R-8.11] Currency rides every money column, driven by store config."""

    def test_new_order_and_items_default_to_store_currency(self):
        order = self.create_order()

        self.assertEqual(order.currency, "INR")
        items = list(order.items.all())
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item.currency, "INR")
        # The label rides the money; it never replaces the Decimal amounts.
        self.assertIsInstance(order.total_amount, Decimal)
        self.assertIsInstance(order.discount_amount, Decimal)

    def test_setting_override_changes_currency_of_new_rows(self):
        with override_settings(DEFAULT_CURRENCY="USD"):
            order = Order.objects.create(
                user=self.buyer,
                full_name="B", phone="1", address="a",
                city="c", state="s", pincode="1",
                total_amount=Decimal("10.00"),
            )
            item = OrderItem.objects.create(
                order=order,
                product=self.product,
                product_name=self.product.name,
                price=self.product.price,
                quantity=1,
                subtotal=self.product.price,
            )

        self.assertEqual(order.currency, "USD")
        self.assertEqual(item.currency, "USD")

    def test_checkout_mints_rows_in_the_configured_currency(self):
        with override_settings(DEFAULT_CURRENCY="EUR"):
            order = self.create_order()

        self.assertEqual(order.currency, "EUR")
        for item in order.items.all():
            self.assertEqual(item.currency, "EUR")

    def test_gateway_payload_uses_the_order_currency(self):
        with override_settings(DEFAULT_CURRENCY="USD"):
            order = self.create_order()
        client_mock = self.razorpay_mock(order_id="order_USD001")

        res = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["currency"], "USD")
        client_mock.order.create.assert_called_once_with(
            {"amount": 100000, "currency": "USD", "receipt": f"order_{order.id}"}
        )

    def test_recorded_currency_is_frozen_against_later_setting_changes(self):
        order = self.create_order()  # minted under the current config
        with override_settings(DEFAULT_CURRENCY="USD"):
            order.refresh_from_db()

        # Existing rows keep the currency they were minted with; the setting
        # only ever steers new rows.
        self.assertEqual(order.currency, "INR")


class DefaultCurrencySettingTests(SimpleTestCase):
    """[R-8.11] _env_currency: env-driven with the fail-safe INR default."""

    def test_valid_code_is_normalised_to_uppercase(self):
        with mock.patch.dict(os.environ, {"DEFAULT_CURRENCY": "usd"}):
            self.assertEqual(_env_currency("DEFAULT_CURRENCY", "INR"), "USD")

    def test_malformed_code_falls_back_with_warning(self):
        with mock.patch.dict(os.environ, {"DEFAULT_CURRENCY": "rupees"}):
            with self.assertLogs("config.settings", level="WARNING") as logs:
                self.assertEqual(_env_currency("DEFAULT_CURRENCY", "INR"), "INR")
        # The resolver upper-cases before matching, so the warning names
        # the offending value in its normalised form.
        self.assertIn("RUPEES", logs.output[0])

    def test_missing_code_uses_the_documented_default(self):
        env = {k: v for k, v in os.environ.items() if k != "DEFAULT_CURRENCY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(_env_currency("DEFAULT_CURRENCY", "INR"), "INR")


@tag("orders")
class CurrencyBackfillMigrationTests(TransactionTestCase):
    """[R-8.11] The 0008 backfill is deterministic: rows that predate the
    column land 'INR' regardless of the migrating environment's config."""

    def test_0008_backfills_preexisting_rows_to_inr(self):
        call_command("migrate", "orders", "0007", verbosity=0, interactive=False)
        user_id = User.objects.create_user(
            "backfill", "backfill@example.com", "S3cure-Passphrase!"
        ).id
        # The columns do not exist at 0007, so the ORM cannot write these
        # rows: raw SQL reproduces exactly what a pre-currency store had.
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO orders_order (user_id, full_name, phone, address,"
                " city, state, pincode, status, discount_amount, total_amount,"
                " created_at, updated_at)"
                " VALUES (%s, 'Backfill', '1', 'a', 'c', 's', '1', 'pending',"
                " 0, 100.00, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                [user_id],
            )
            cursor.execute(
                "INSERT INTO orders_orderitem (order_id, product_id, product_name,"
                " sku, variant_name, price, quantity, subtotal)"
                " VALUES ((SELECT MAX(id) FROM orders_order), NULL, 'Legacy',"
                " '', 'Legacy', 100.00, 1, 100.00)"
            )

        call_command("migrate", "orders", verbosity=0, interactive=False)

        order = Order.objects.get(full_name="Backfill")
        self.assertEqual(order.currency, "INR")
        items = list(order.items.all())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].currency, "INR")


@tag("orders")
class CurrencyExposureTests(OrderTestBase):
    """[R-8.11] part 2/2: the minted currency surfaces wherever its money
    surfaces — customer-facing serializers and the admin — and nowhere is
    it writable."""

    def test_order_currency_exposed_read_only_in_order_serializer(self):
        self.assertIn("currency", OrderSerializer.Meta.fields)
        self.assertIn("currency", OrderSerializer.Meta.read_only_fields)

        order = self.create_order()

        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["results"][0]["currency"], "INR")
        self.assertEqual(res.data["results"][0]["id"], order.id)

    def test_checkout_replay_and_item_bodies_carry_the_currency(self):
        """OrderSerializer is the single surface for the checkout response,
        the dedup replay, and every order read; OrderItemSerializer rides
        it via ``items``, so both bodies denominate their amounts."""
        self.assertIn("currency", OrderItemSerializer.Meta.fields)
        self.assertIn("currency", OrderItemSerializer.Meta.read_only_fields)

        checkout = self.checkout()
        self.assertEqual(checkout.status_code, 201, checkout.data)
        self.assertEqual(checkout.data["currency"], "INR")
        self.assertEqual(checkout.data["items"][0]["currency"], "INR")

        replay = self.checkout()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["currency"], "INR")

    def test_default_currency_override_propagates_through_serialization(self):
        with override_settings(DEFAULT_CURRENCY="USD"):
            order = self.create_order()

        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["results"][0]["id"], order.id)
        self.assertEqual(res.data["results"][0]["currency"], "USD")
        self.assertEqual(res.data["results"][0]["items"][0]["currency"], "USD")

    def test_admin_list_display_and_read_only_detail_surface_currency(self):
        self.assertIn("currency", OrderAdmin.list_display)
        self.assertIn("currency", OrderAdmin.readonly_fields)
        # The order change page renders currency inside the read-only
        # payment fieldset, beside the amounts it denominates.
        payment_fieldset = dict(OrderAdmin.fieldsets)[
            "Payment (server-computed — read only)"
        ]
        self.assertIn("currency", payment_fieldset["fields"])

        order = self.create_order()

        User.objects.create_superuser(
            "opsboss", "ops@example.com", "S3cure-Passphrase!"
        )
        self.client.login(username="opsboss", password="S3cure-Passphrase!")
        changelist = self.client.get("/admin/orders/order/")
        self.assertEqual(changelist.status_code, 200)
        self.assertContains(changelist, "Currency")  # the list column header
        change_page = self.client.get(f"/admin/orders/order/{order.id}/change/")
        self.assertEqual(change_page.status_code, 200)
        self.assertContains(change_page, "INR")  # the read-only value renders


@tag("orders")
class ApplyCouponTests(OrderTestBase):
    """30-32. Coupon preview validation and math."""

    def _preview(self, code):
        return self.client.post("/api/orders/apply-coupon/", {"code": code}, format="json")

    # 30. every rejection shares one uniform body: unknown, inactive,
    # not-yet-valid, expired, usage-limit and min-order must be
    # indistinguishable so coupon existence/validation state never leaks
    # to callers probing the code space (V-11) ----------------------------
    def test_coupon_rejections_are_uniform(self):
        cases = [
            ("unknown", "SAVE404"),
            ("inactive", self.make_coupon(code="DEAD", active=False).code),
            (
                "expired",
                self.make_coupon(code="OLD", valid_until=timezone.now() - timedelta(minutes=1)).code,
            ),
            (
                "not-yet-valid",
                self.make_coupon(code="FUTURE", valid_from=timezone.now() + timedelta(days=1)).code,
            ),
            (
                "usage-limit",
                self.make_coupon(code="MAXED", usage_limit=5, used_count=5).code,
            ),
            (
                "min-order",
                self.make_coupon(code="BIGSPEND", minimum_order_amount="5000").code,
            ),
        ]
        for name, code in cases:
            with self.subTest(case=name):
                res = self._preview(code)
                self.assertEqual(res.status_code, 400, res.data)
                # one body for every failure reason; no configuration detail
                # (e.g. minimum_order_amount) may ride along (SPEC-9-03: the
                # uniform envelope adds the status-family code + empty
                # details, the message stays the top-level string)
                self.assertEqual(
                    res.data,
                    {
                        "error": "Invalid coupon code",
                        "code": "validation_error",
                        "details": {},
                    },
                )

    def test_missing_code_rejected(self):
        res = self.client.post("/api/orders/apply-coupon/", {}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Coupon code is required")

    def test_cartless_caller_learns_nothing_about_coupon_existence(self):
        """With no cart, a known-valid and an unknown code must be
        indistinguishable: the cart error comes first and is uniform."""
        self.make_coupon(code="SAVE10", discount_value="10")
        fresh = self.fresh_client()
        known = fresh.post(
            "/api/orders/apply-coupon/", {"code": "SAVE10"}, format="json"
        )
        unknown = fresh.post(
            "/api/orders/apply-coupon/", {"code": "SAVE404"}, format="json"
        )
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.data, unknown.data)
        self.assertEqual(known.data["error"], "Cart not found")

    def test_coupon_code_matched_case_insensitively(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        res = self._preview("save10")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["coupon"], "SAVE10")

    # 31. percentage cap via maximum_discount ----------------------------------------------
    def test_percentage_discount_capped_by_maximum_discount(self):
        self.make_coupon(code="HALFCAP", discount_value="50", maximum_discount="300")
        res = self._preview("HALFCAP")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["subtotal"], Decimal("1000.00"))
        self.assertEqual(res.data["discount"], Decimal("300.00"))  # 500.00 raw -> capped
        self.assertEqual(res.data["final_total"], Decimal("700.00"))

        self.make_coupon(code="HALF", discount_value="50")
        res = self._preview("HALF")
        self.assertEqual(res.data["discount"], Decimal("500.00"))  # uncapped 50%
        self.assertEqual(res.data["final_total"], Decimal("500.00"))

    # 32. discount clamped at subtotal -------------------------------------------------------
    def test_fixed_discount_clamped_to_subtotal(self):
        self.make_coupon(code="FLAT1500", discount_type="fixed", discount_value="1500")
        res = self._preview("FLAT1500")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["discount"], Decimal("1000.00"))  # clamped, not negative total
        self.assertEqual(res.data["final_total"], Decimal("0.00"))

        self.make_coupon(code="FLAT40", discount_type="fixed", discount_value="40")
        res = self._preview("FLAT40")
        self.assertEqual(res.data["discount"], Decimal("40.00"))
        self.assertEqual(res.data["final_total"], Decimal("960.00"))

    def test_preview_without_session_or_cart(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        res = self.fresh_client().post("/api/orders/apply-coupon/", {"code": "SAVE10"}, format="json")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")


@tag("orders")
class ApplyCouponThrottleTests(OrderTestBase):
    """Conventions: every public mutating endpoint gets a throttle scope.
    The preview is the coupon brute-force surface (V-11/V-04)."""

    def _preview(self, code):
        return self.client.post(
            "/api/orders/apply-coupon/", {"code": code}, format="json"
        )

    def test_coupon_scope_and_rate_are_configured(self):
        self.assertEqual(apply_coupon.view_class.throttle_scope, "coupon")
        self.assertIn(ScopedRateThrottle, apply_coupon.view_class.throttle_classes)
        self.assertIn("coupon", api_settings.DEFAULT_THROTTLE_RATES)

    def test_rate_limit_engages_when_budget_spent(self):
        """The scope actually enforces: a third preview inside a 2/min budget
        is 429'd. DRF binds THROTTLE_RATES at import, so the rate is patched
        on the throttle class rather than via override_settings."""
        self.make_coupon(code="SAVE10", discount_value="10")
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["coupon"] = "2/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            self.assertEqual(self._preview("SAVE10").status_code, 200)
            self.assertEqual(self._preview("SAVE10").status_code, 200)
            throttled = self._preview("SAVE10")
        self.assertEqual(throttled.status_code, 429, throttled.data)

    def test_rejections_consume_the_same_budget_as_successes(self):
        """Brute-forcing invalid codes hits the same bucket as valid ones:
        failures cannot be used to probe indefinitely."""
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["coupon"] = "2/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            self.assertEqual(self._preview("GHOST404").status_code, 400)
            self.assertEqual(self._preview("GHOST404").status_code, 400)
            throttled = self._preview("GHOST404")
        self.assertEqual(throttled.status_code, 429, throttled.data)


@tag("orders")
class CheckoutTests(OrderTestBase):
    """33-35. Server-owned pricing, snapshot math, validation."""

    # 33. rounding parity preview vs checkout (F-11) ----------------------------------------
    def test_f11_rounding_parity_between_preview_and_checkout(self):
        """F-11 regression pin: preview and checkout must agree to the paisa
        on fractional percentage math — 30% of 2999.99 is 899.997 raw, and
        both paths quantize it to the same 2-dp amount (900.00). Marker
        removed when quantize parity shipped (SPEC-7-03); the assertEqual
        below is unchanged from the original pin."""
        self.make_product(name="Grand Cru", price="1999.99", stock=5)
        self.seed_session_cart([])  # keep buyer cart; add the expensive item
        product = products.objects.get(name="Grand Cru")
        CartItem.objects.create(cart=Cart.objects.get(session_id=self.client.session.session_key), product=product, quantity=1)
        coupon = self.make_coupon(code="PCT30", discount_value="30")

        preview = self.client.post("/api/orders/apply-coupon/", {"code": "PCT30"}, format="json")
        order = self.create_order(coupon=coupon)

        self.assertEqual(
            Decimal(preview.data["discount"]),
            order.discount_amount,
            f"preview={preview.data['discount']} stored={order.discount_amount}",
        )

    def test_rounding_parity_holds_for_clean_amounts(self):
        """Parity invariant for amounts that quantize exactly (guards against
        regressions in the common case)."""
        coupon = self.make_coupon(code="PCT10", discount_value="10")
        preview = self.client.post("/api/orders/apply-coupon/", {"code": "PCT10"}, format="json")
        order = self.create_order(coupon=coupon)
        self.assertEqual(Decimal(preview.data["discount"]), order.discount_amount)
        self.assertEqual(Decimal(preview.data["final_total"]), order.total_amount)

    # 34. server-side total from DB prices; client-sent amounts ignored ---------------------
    def test_total_computed_server_side_client_amounts_ignored(self):
        other = self.make_user("someone")
        coupon = self.make_coupon(code="PCT10", discount_value="10")

        res = self.checkout(
            coupon_code="pct10",
            total_amount="1.00",
            discount_amount="999.99",
            status="delivered",
            user=other.id,
        )

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertEqual(order.total_amount, Decimal("900.00"))   # 1000 - 10%
        self.assertEqual(order.discount_amount, Decimal("100.00"))
        self.assertEqual(order.status, "pending")                 # not client-controlled
        self.assertEqual(order.user, self.buyer)                  # not client-controlled
        self.assertEqual(order.coupon, coupon)
        item = OrderItem.objects.get(order=order)
        self.assertEqual(item.price, Decimal("500.00"))           # snapshot from DB
        self.assertEqual(item.subtotal, Decimal("1000.00"))
        self.assertEqual(item.product_name, "Rose Aurum")

    def test_item_rows_snapshot_each_cart_line(self):
        other = self.make_product(name="Oud Royale", price="250.00", stock=4)
        self.seed_session_cart([])
        cart = Cart.objects.get(session_id=self.client.session.session_key)
        CartItem.objects.create(cart=cart, product=other, quantity=3)

        order = self.create_order()
        rows = {row.product_name: row for row in order.items.all()}
        self.assertEqual(rows["Rose Aurum"].subtotal, Decimal("1000.00"))
        self.assertEqual(rows["Oud Royale"].subtotal, Decimal("750.00"))
        self.assertEqual(order.total_amount, Decimal("1750.00"))
        self.assertEqual(order.discount_amount, Decimal("0.00"))

    # 35. missing required field -> 400 listing the field ------------------------------------
    def test_missing_required_field_returns_400_naming_field(self):
        for field in ("full_name", "phone", "address", "city", "state", "pincode"):
            with self.subTest(field=field):
                payload = self.checkout_payload()
                payload.pop(field)
                res = self.client.post("/api/orders/checkout/", payload, format="json")
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data["error"], f"{field} is required")

    def test_blank_required_field_rejected(self):
        res = self.checkout(phone="")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "phone is required")
        self.assertEqual(Order.objects.count(), 0)

    def test_checkout_requires_authentication_and_cart(self):
        self.assertEqual(self.fresh_client().post("/api/orders/checkout/", self.checkout_payload(), format="json").status_code, 401)

        self.auth(None)
        res = self.client.post("/api/orders/checkout/", self.checkout_payload(), format="json")
        self.assertEqual(res.status_code, 401, res.data)

        # authenticated but no cart in this session -> documented 404 (F-18)
        client = self.fresh_client()
        self.api_login("buyer", client=client)
        res = client.post("/api/orders/checkout/", self.checkout_payload(), format="json")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")

    def test_checkout_with_empty_cart_rejected(self):
        cart = Cart.objects.get(session_id=self.client.session.session_key)
        cart.items.all().delete()
        res = self.checkout()
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Cart is empty")


@tag("orders")
class CheckoutStockGateTests(OrderTestBase):
    """SPEC-6-01 [6.2.22]: a cart line whose stock dropped after the add is
    rejected at order creation with an actionable 400, so the customer never
    pays for an unfulfillable order. The gate is advisory and read-only: the
    authoritative stock check stays in verify_payment, because stock can
    change again between create and pay."""

    def test_insufficient_stock_at_creation_returns_400_without_side_effects(self):
        # stock dropped after the item was added to the cart
        products.objects.filter(pk=self.product.pk).update(stock=1)

        res = self.checkout()

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(
            res.data["error"],
            'Not enough stock for "Rose Aurum" (requested 2, only 1 in stock). '
            "Reduce the quantity or remove the item to continue.",
        )
        self.assertEqual(
            res.data["details"]["products"],
            [{"name": "Rose Aurum", "requested": 2, "available": 1}],
        )
        # nothing was created or mutated: the customer fixes the cart
        # instead of paying for an order that cannot be fulfilled
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        cart = Cart.objects.get(session_id=self.client.session.session_key)
        self.assertEqual(cart.items.get().quantity, 2)  # cart line untouched
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 1)  # no decrement at creation

    def test_zero_stock_line_rejected(self):
        """[6.2.22]: an out-of-stock product is not purchasable."""
        products.objects.filter(pk=self.product.pk).update(stock=0)

        res = self.checkout()

        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("Rose Aurum", res.data["error"])
        self.assertEqual(Order.objects.count(), 0)

    def test_exact_remaining_stock_succeeds(self):
        """Boundary: requested == available is still purchasable."""
        products.objects.filter(pk=self.product.pk).update(stock=2)

        res = self.checkout()

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertEqual(order.items.get().quantity, 2)

    def test_one_bad_line_names_only_the_bad_product(self):
        second = self.make_product(name="Oud Royale", price="250.00", stock=4)
        self.seed_session_cart([])
        cart = Cart.objects.get(session_id=self.client.session.session_key)
        CartItem.objects.create(cart=cart, product=second, quantity=1)
        products.objects.filter(pk=self.product.pk).update(stock=1)

        res = self.checkout()

        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("Rose Aurum", res.data["error"])
        self.assertNotIn("Oud Royale", res.data["error"])
        self.assertEqual(
            res.data["details"]["products"],
            [{"name": "Rose Aurum", "requested": 2, "available": 1}],
        )
        self.assertEqual(Order.objects.count(), 0)

    def test_multiple_bad_lines_listed_in_error(self):
        second = self.make_product(name="Oud Royale", price="250.00", stock=4)
        self.seed_session_cart([])
        cart = Cart.objects.get(session_id=self.client.session.session_key)
        CartItem.objects.create(cart=cart, product=second, quantity=3)
        products.objects.filter(pk=self.product.pk).update(stock=1)  # wants 2
        products.objects.filter(pk=second.pk).update(stock=2)        # wants 3

        res = self.checkout()

        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("Rose Aurum", res.data["error"])
        self.assertIn("Oud Royale", res.data["error"])
        self.assertIn("remove these items", res.data["error"])
        self.assertEqual(
            {row["name"] for row in res.data["details"]["products"]},
            {"Rose Aurum", "Oud Royale"},
        )
        self.assertEqual(Order.objects.count(), 0)


@tag("orders")
class CheckoutDedupTests(OrderTestBase):
    """[R-21.2.6] duplicate checkout submissions create no duplicate orders.

    The accidental double-click / client retry resubmits the byte-identical
    payload against an unchanged cart (checkout never clears the cart;
    cleanup happens after payment confirmation), so identical still-payable
    pending submissions collapse onto the first order instead of minting a
    second charge target. Deliberate differences (cart lines, coupon,
    shipping, a settled first order) always create a fresh order, and
    cross-user contention is untouched -- the oversell decision stays at
    verify_payment."""

    def test_double_click_resubmission_replays_the_same_order(self):
        first = self.checkout()
        self.assertEqual(first.status_code, 201, first.data)

        second = self.checkout()

        # one payable order, one item snapshot, one creation trail row:
        # the retry is a replay, not a second order
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(second.data["total_amount"], first.data["total_amount"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_CREATED
            ).count(),
            1,
        )

    def test_replayed_order_is_the_one_that_gets_paid(self):
        self.checkout()
        replay = self.checkout()
        self.assertEqual(replay.status_code, 200, replay.data)

        # the client flow continues on the replayed id: exactly one gateway
        # order is minted for it, so exactly one charge can ever follow
        client_mock = self.razorpay_mock()
        payment = self.client.post(
            "/api/orders/payment/", {"order_id": replay.data["id"]}, format="json"
        )
        self.assertEqual(payment.status_code, 200, payment.data)
        client_mock.order.create.assert_called_once()

    def test_different_user_with_identical_cart_still_gets_own_order(self):
        first = self.checkout()
        self.assertEqual(first.status_code, 201, first.data)

        self.make_user("other")
        other_client = self.fresh_client()
        self.api_login("other", client=other_client)
        self.seed_session_cart([(self.product, 2)], client=other_client)
        res = other_client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )

        # the guard is keyed per user: two buyers racing the same stock keep
        # two orders, and the oversell decision stays at verify
        self.assertEqual(res.status_code, 201, res.data)
        self.assertNotEqual(res.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 2)

    def test_deliberately_different_payload_creates_new_order(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        first = self.checkout()
        self.assertEqual(first.status_code, 201, first.data)

        recouponed = self.checkout(coupon_code="SAVE10")
        self.assertEqual(recouponed.status_code, 201, recouponed.data)
        self.assertNotEqual(recouponed.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 2)
        recouponed_order = Order.objects.get(pk=recouponed.data["id"])
        self.assertEqual(recouponed_order.coupon.code, "SAVE10")

        # a deliberate shipping change is a new purchase, not a retry
        moved = self.checkout(city="Pune")
        self.assertEqual(moved.status_code, 201, moved.data)
        self.assertEqual(Order.objects.count(), 3)

    def test_changed_cart_lines_create_new_order(self):
        first = self.checkout()
        self.assertEqual(first.status_code, 201, first.data)
        other = self.make_product(name="Oud Royale", price="250.00", stock=4)
        cart = Cart.objects.get(session_id=self.client.session.session_key)
        CartItem.objects.create(cart=cart, product=other, quantity=1)

        res = self.checkout()

        self.assertEqual(res.status_code, 201, res.data)
        self.assertNotEqual(res.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 2)

    def test_window_expiry_allows_a_genuine_reorder(self):
        first = self.checkout()
        self.assertEqual(first.status_code, 201, first.data)
        Order.objects.filter(pk=first.data["id"]).update(
            created_at=timezone.now()
            - timedelta(seconds=settings.CHECKOUT_DEDUP_WINDOW_SECONDS + 60)
        )

        res = self.checkout()

        # the guard bounds the accidental window; it must never permanently
        # block an identical second purchase
        self.assertEqual(res.status_code, 201, res.data)
        self.assertNotEqual(res.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 2)

    def test_settled_order_does_not_block_a_new_identical_checkout(self):
        first = self.checkout()
        self.assertEqual(first.status_code, 201, first.data)
        # simulate a completed purchase: no longer payable, so the same cart
        # legitimately starts a fresh order
        Order.objects.filter(pk=first.data["id"]).update(
            status="confirmed", razorpay_payment_id="pay_SETTLED"
        )

        res = self.checkout()

        self.assertEqual(res.status_code, 201, res.data)
        self.assertNotEqual(res.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 2)


@tag("orders")
class CreatePaymentTests(OrderTestBase):
    """36. Razorpay order creation (mocked), idempotent reuse, ownership."""

    def test_payment_creation_mocked_razorpay(self):
        client_mock = self.razorpay_mock(order_id="order_TEST001")
        order = self.create_order()

        res = self.client.post("/api/orders/payment/", {"order_id": order.id}, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["razorpay_order_id"], "order_TEST001")
        self.assertEqual(res.data["amount"], 100000)  # 1000.00 * 100 paise
        self.assertEqual(res.data["amount_in_rupees"], Decimal("1000.00"))
        self.assertEqual(res.data["currency"], "INR")
        self.assertEqual(res.data["key_id"], TEST_RAZORPAY_KEY_ID)  # dummy, never the real key
        client_mock.order.create.assert_called_once_with(
            {"amount": 100000, "currency": "INR", "receipt": f"order_{order.id}"}
        )
        order.refresh_from_db()
        self.assertEqual(order.razorpay_order_id, "order_TEST001")

    def test_payment_creation_reuses_existing_razorpay_order_id(self):
        client_mock = self.razorpay_mock()
        order = self.create_order()

        first = self.client.post("/api/orders/payment/", {"order_id": order.id}, format="json")
        second = self.client.post("/api/orders/payment/", {"order_id": order.id}, format="json")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["razorpay_order_id"], second.data["razorpay_order_id"])
        client_mock.order.create.assert_called_once()  # idempotent: no second API call

    def test_payment_requires_order_id(self):
        self.razorpay_mock()
        res = self.client.post("/api/orders/payment/", {}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "order_id is required")

    def test_payment_unknown_order_404(self):
        self.razorpay_mock()
        res = self.client.post("/api/orders/payment/", {"order_id": 999999}, format="json")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Order not found")

    def test_payment_for_another_users_order_404(self):
        """Security: order ids must not be enumerable across accounts."""
        client_mock = self.razorpay_mock()
        other = self.make_user("seller")
        other_order = Order.objects.create(
            user=other, full_name="S", phone="1", address="a", city="c", state="s",
            pincode="1", total_amount=Decimal("10.00"),
        )
        res = self.client.post("/api/orders/payment/", {"order_id": other_order.id}, format="json")
        self.assertEqual(res.status_code, 404, res.data)
        client_mock.order.create.assert_not_called()

    def test_payment_rejected_for_non_pending_order(self):
        self.razorpay_mock()
        order = self.create_order()
        order.status = "confirmed"
        order.save(update_fields=["status"])

        res = self.client.post("/api/orders/payment/", {"order_id": order.id}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "This order cannot be paid")


@tag("orders")
class VerifyPaymentTests(OrderTestBase):
    """37-39. Signature check, success side effects, idempotency."""

    def _prepare_paid_setup(self, coupon=None, order_id="order_TEST001", payment_id="pay_TEST001"):
        client_mock = self.razorpay_mock(order_id=order_id)
        order = self.create_order(coupon=coupon)
        res = self.client.post("/api/orders/payment/", {"order_id": order.id}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()  # picks up razorpay_order_id written by the payment call
        payload = {
            "order_id": order.id,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": "sig",
        }
        return client_mock, order, payload

    # 37. forged signature -> 400 ------------------------------------------------------------
    def test_forged_signature_rejected_400(self):
        client_mock, order, payload = self._prepare_paid_setup()
        self.razorpay_fail_signature(client_mock)

        res = self.client.post("/api/orders/payment/verify/", payload, format="json")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Payment verification failed")
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertIsNone(order.razorpay_payment_id)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)  # untouched

    def test_verify_requires_all_payment_fields(self):
        self.razorpay_mock()
        full = {
            "razorpay_order_id": "order_X",
            "razorpay_payment_id": "pay_X",
            "razorpay_signature": "sig",
        }
        for missing in ("razorpay_order_id", "razorpay_payment_id", "razorpay_signature"):
            with self.subTest(missing=missing):
                payload = {k: v for k, v in full.items() if k != missing}
                res = self.client.post("/api/orders/payment/verify/", payload, format="json")
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data["error"], "Payment details are required")

    # 38. success path: stock, coupon, cart cleanup, status ----------------------------------
    def test_verified_payment_updates_stock_coupon_cart_and_status(self):
        second = self.make_product(name="Oud Royale", price="250.00", stock=4)
        self.seed_session_cart([])
        cart = Cart.objects.get(session_id=self.client.session.session_key)
        CartItem.objects.create(cart=cart, product=second, quantity=1)
        coupon = self.make_coupon(code="PCT10", discount_value="10", usage_limit=5)
        order = self.create_order(coupon=coupon)
        self.razorpay_mock(order_id="order_PAYOK")

        res = self.client.post("/api/orders/payment/", {"order_id": order.id}, format="json")
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        payload = {
            "order_id": order.id,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": "pay_OK123",
            "razorpay_signature": "sig",
        }
        res = self.client.post("/api/orders/payment/verify/", payload, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], "confirmed")
        self.assertEqual(res.data["razorpay_payment_id"], "pay_OK123")

        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.razorpay_payment_id, "pay_OK123")

        self.product.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.product.stock, 8)  # 10 - 2
        self.assertEqual(second.stock, 3)        # 4 - 1

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)

        cart_items = list(CartItem.objects.filter(cart=cart))
        self.assertEqual(cart_items, [])  # both paid lines removed

    def test_verified_payment_writes_one_sale_movement_per_product(self):
        """SPEC-6-02 [6.5.17]: payment-time decrements are inventory
        mutations too — one SALE ledger row per product, system actor (no
        user), the order number as reference, and stock_after equal to the
        real post-decrement stock."""
        second = self.make_product(name="Oud Royale", price="250.00", stock=4)
        cart = Cart.objects.get(session_id=self.client.session.session_key)
        CartItem.objects.create(cart=cart, product=second, quantity=1)
        order = self.create_order()
        self.razorpay_mock(order_id="order_LEDGER")
        self.client.post("/api/orders/payment/", {"order_id": order.id}, format="json")
        order.refresh_from_db()
        payload = {
            "order_id": order.id,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": "pay_LEDGER",
            "razorpay_signature": "sig",
        }

        res = self.client.post("/api/orders/payment/verify/", payload, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        movements = list(StockMovement.objects.order_by("product_id"))
        self.assertEqual(len(movements), 2)  # exactly one per decremented product

        first, second_move = movements
        self.assertEqual(first.product_id, self.product.id)
        self.assertEqual(first.delta, -2)
        self.assertEqual(first.reason, StockMovement.Reason.SALE)
        self.assertEqual(first.stock_after, 8)
        self.assertIsNone(first.created_by)  # system actor, not the buyer
        self.assertEqual(first.note, f"Order #{order.id}")

        self.assertEqual(second_move.product_id, second.id)
        self.assertEqual(second_move.delta, -1)
        self.assertEqual(second_move.reason, StockMovement.Reason.SALE)
        self.assertEqual(second_move.stock_after, 3)
        self.assertIsNone(second_move.created_by)
        self.assertEqual(second_move.note, f"Order #{order.id}")

        # ledger agrees with reality on the products themselves
        self.product.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.product.stock, 8)
        self.assertEqual(second.stock, 3)

    def test_verify_without_session_cookie_skips_cart_cleanup(self):
        """JWT-only client (no cookies): payment succeeds; there is simply no
        session cart to clean (documented hybrid-auth behaviour)."""
        coupon = self.make_coupon(code="PCT10", discount_value="10")
        order = self.create_order(coupon=coupon)
        self.razorpay_mock(order_id="order_NOPAY")

        res = self.client.post("/api/orders/payment/", {"order_id": order.id}, format="json")
        order.refresh_from_db()
        payload = {
            "order_id": order.id,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": "pay_OK",
            "razorpay_signature": "sig",
        }
        no_cookie = self.fresh_client()
        no_cookie.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")
        res = no_cookie.post("/api/orders/payment/verify/", payload, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(CartItem.objects.count(), 1)  # cart untouched, not the verify's job here

    # 39. idempotency; insufficient stock -> 409 (V-03: no refund path yet) ------------------
    def test_second_verify_rejected_idempotent(self):
        coupon = self.make_coupon(code="PCT10", discount_value="10", usage_limit=5)
        _, order, payload = self._prepare_paid_setup(coupon=coupon)
        first = self.client.post("/api/orders/payment/verify/", payload, format="json")
        self.assertEqual(first.status_code, 200, first.data)

        second = self.client.post("/api/orders/payment/verify/", payload, format="json")
        self.assertEqual(second.status_code, 400, second.data)
        self.assertEqual(second.data["error"], "This order has already been processed")

        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 8)          # decremented exactly once
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)           # incremented exactly once
        self.assertEqual(order.status, "confirmed")
        # SPEC-6-02: the rejected replay must not double-book the ledger either
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_insufficient_stock_at_verify_returns_409(self):
        """V-03 (documents the charged-but-unfulfilled gap): when stock is
        gone at verify time the order stays pending with money captured; the
        refund flow does not exist yet, so this asserts the current contract."""
        order = self.create_order()
        self.razorpay_mock(order_id="order_RACE")
        self.client.post("/api/orders/payment/", {"order_id": order.id}, format="json")
        order.refresh_from_db()
        products.objects.filter(pk=self.product.pk).update(stock=1)  # sold out elsewhere meanwhile

        payload = {
            "order_id": order.id,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": "pay_RACE",
            "razorpay_signature": "sig",
        }
        res = self.client.post("/api/orders/payment/verify/", payload, format="json")

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data["error"], "An item is no longer available in the requested quantity")
        order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertEqual(self.product.stock, 1)
        self.assertIsNone(order.razorpay_payment_id)
        # no decrement, no ledger row (SPEC-6-02)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_coupon_invalidated_between_checkout_and_verify_returns_409(self):
        coupon = self.make_coupon(code="PCT10", discount_value="10", usage_limit=1)
        order = self.create_order(coupon=coupon)
        self.razorpay_mock(order_id="order_CP")
        self.client.post("/api/orders/payment/", {"order_id": order.id}, format="json")

        # someone else burns the single use before this verify lands
        Coupon.objects.filter(pk=coupon.pk).update(used_count=1)
        order.refresh_from_db()

        payload = {
            "order_id": order.id,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": "pay_CP",
            "razorpay_signature": "sig",
        }
        res = self.client.post("/api/orders/payment/verify/", payload, format="json")

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data["error"], "The coupon is no longer valid")
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_verify_rejects_mismatched_razorpay_order_id(self):
        """Security: a signature valid for another razorpay order must not
        confirm this order."""
        _, order, payload = self._prepare_paid_setup()
        payload["razorpay_order_id"] = "order_SOMEONE_ELSE"

        res = self.client.post("/api/orders/payment/verify/", payload, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Payment does not belong to this order")
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")

    def test_verify_requires_order_id_and_known_order(self):
        self.razorpay_mock()
        res = self.client.post("/api/orders/payment/verify/", {
            "razorpay_order_id": "order_X",
            "razorpay_payment_id": "pay_X",
            "razorpay_signature": "sig",
        }, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "order_id is required")

        res = self.client.post("/api/orders/payment/verify/", {
            "order_id": 999999,
            "razorpay_order_id": "order_X",
            "razorpay_payment_id": "pay_X",
            "razorpay_signature": "sig",
        }, format="json")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Order not found")

    def test_verify_scoped_to_owner(self):
        other = self.make_user("seller")
        other_order = Order.objects.create(
            user=other, full_name="S", phone="1", address="a", city="c", state="s",
            pincode="1", total_amount=Decimal("10.00"),
        )
        self.razorpay_mock(order_id="order_THEIRS")
        res = self.client.post("/api/orders/payment/verify/", {
            "order_id": other_order.id,
            "razorpay_order_id": "order_THEIRS",
            "razorpay_payment_id": "pay_THEIRS",
            "razorpay_signature": "sig",
        }, format="json")
        self.assertEqual(res.status_code, 404, res.data)


@tag("orders")
class CheckoutCouponValidationTests(OrderTestBase):
    """The checkout path re-validates the coupon server-side (never trust the
    preview); every rejection branch must be reachable at checkout too."""

    def test_checkout_coupon_rejections(self):
        cases = [
            ("unknown", "GHOST404", "Invalid coupon code"),
            ("inactive", self.make_coupon(code="DEAD", active=False).code, "This coupon is inactive"),
            (
                "expired",
                self.make_coupon(code="OLD", valid_until=timezone.now() - timedelta(minutes=1)).code,
                "This coupon has expired",
            ),
            (
                "not-yet-valid",
                self.make_coupon(code="FUTURE", valid_from=timezone.now() + timedelta(days=1)).code,
                "This coupon is not active yet",
            ),
            (
                "usage-limit",
                self.make_coupon(code="MAXED", usage_limit=5, used_count=5).code,
                "This coupon has reached its usage limit",
            ),
            (
                "min-order",
                self.make_coupon(code="BIGSPEND", minimum_order_amount="5000").code,
                "Minimum order amount is required",
            ),
        ]
        for name, code, expected_error in cases:
            with self.subTest(case=name):
                res = self.checkout(coupon_code=code)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data["error"], expected_error)
        self.assertEqual(Order.objects.count(), 0)  # nothing was created

    def test_checkout_percentage_cap_reaches_persisted_totals(self):
        self.make_coupon(code="HALFCAP", discount_value="50", maximum_discount="300")
        res = self.checkout(coupon_code="HALFCAP")
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.discount_amount, Decimal("300.00"))
        self.assertEqual(order.total_amount, Decimal("700.00"))

    def test_checkout_fixed_clamp_reaches_persisted_totals(self):
        self.make_coupon(code="FLAT1500", discount_type="fixed", discount_value="1500")
        res = self.checkout(coupon_code="FLAT1500")
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get()
        self.assertEqual(order.discount_amount, Decimal("1000.00"))
        self.assertEqual(order.total_amount, Decimal("0.00"))


@tag("orders")
class CheckoutCartLookupTests(OrderTestBase):
    def test_checkout_with_session_but_no_cart_row_404(self):
        """Session cookie exists, but the Cart row is gone (expired/cleared
        server-side) -> the documented 'Cart not found' 404."""
        Cart.objects.all().delete()
        res = self.checkout()
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")

    def test_apply_coupon_with_session_but_no_cart_row_404(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        Cart.objects.all().delete()
        res = self.client.post("/api/orders/apply-coupon/", {"code": "SAVE10"}, format="json")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")


@tag("orders")
class OrderListTests(OrderTestBase):
    def test_order_list_requires_authentication(self):
        res = self.fresh_client().get("/api/orders/")
        self.assertEqual(res.status_code, 401, res.data)

    def test_order_list_scoped_to_owner(self):
        other = self.make_user("seller")
        coupon = self.make_coupon(code="PCT10", discount_value="10")
        mine = self.create_order(coupon=coupon)
        theirs = Order.objects.create(
            user=other, full_name="S", phone="1", address="a", city="c", state="s",
            pincode="1", total_amount=Decimal("10.00"),
        )

        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 200, res.data)
        ids = [row["id"] for row in res.data["results"]]
        self.assertEqual(ids, [mine.id])
        self.assertNotIn(theirs.id, ids)
        # serializer shape: coupon rendered as its code, items embedded
        self.assertEqual(res.data["results"][0]["coupon"], "PCT10")
        self.assertEqual(res.data["results"][0]["items"][0]["price"], "500.00")

    def test_model_string_representations(self):
        order = self.create_order()
        item = order.items.first()
        self.assertEqual(str(order), f"Order #{order.id} - buyer")
        self.assertEqual(str(item), "Rose Aurum x 2")


@tag("orders")
class OrderDetailTests(OrderTestBase):
    """SPEC-9-04 [R-9.2.15] (spec lines 2951-2959, `GET /account/orders/:id`
    = "Owned order details"): the caller's OWN order only. Ownership is part
    of the lookup, so a foreign order is a uniform 404 — never a 200 (the
    IDOR pin) and never a 403 that would confirm existence."""

    def test_owner_gets_own_order_detail(self):
        order = self.create_order()
        res = self.client.get(f"/api/orders/{order.id}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], order.id)
        self.assertEqual(res.data["order_number"], order.order_number)
        self.assertEqual(res.data["status"], "pending")
        self.assertEqual(res.data["total_amount"], "1000.00")
        self.assertEqual(res.data["items"][0]["product_name"], "Rose Aurum")

    def test_other_users_order_is_404_not_200(self):
        """The IDOR case: another user's order id must not resolve."""
        mine = self.create_order()
        self.make_user("intruder")
        intruder = self.fresh_client()
        self.api_login("intruder", client=intruder)
        res = intruder.get(f"/api/orders/{mine.id}/")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Order not found")

    def test_unknown_order_id_is_404(self):
        res = self.client.get("/api/orders/999999/")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Order not found")

    def test_order_detail_requires_authentication(self):
        order = self.create_order()
        res = self.fresh_client().get(f"/api/orders/{order.id}/")
        self.assertEqual(res.status_code, 401, res.data)

    def test_order_detail_served_on_the_v1_mirror(self):
        """New app-urlconf routes are automatically served on both mounts."""
        order = self.create_order()
        res = self.client.get(f"/api/v1/store/orders/{order.id}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], order.id)


@tag("orders")
class OrderHistoryPaginationTests(OrderTestBase):
    """SPEC-9-04 [R-9.2.14] (spec lines 2938-2946, `GET /account/orders`
    = "Customer order history"). The spec pins neither a page size nor an
    envelope shape for this endpoint, so the house page-number envelope
    (products-listing parity, the shape the frontend's pagination UI is
    built to) is used with env-driven defaults: ORDER_HISTORY_PAGE_SIZE,
    capped by ORDER_HISTORY_MAX_PAGE_SIZE for ?page_size callers."""

    def _checkout_distinct_orders(self, count):
        # Distinct shipping payloads so the SPEC-21-1 dedup guard treats
        # each checkout as a deliberate new purchase, not a replay.
        for i in range(count):
            res = self.checkout(city=f"City {i}")
            self.assertEqual(res.status_code, 201, res.data)

    def test_history_envelope_shape_and_default_page(self):
        self._checkout_distinct_orders(3)
        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            set(res.data.keys()),
            {
                "count",
                "total_pages",
                "current_page",
                "next_page",
                "previous_page",
                "results",
            },
        )
        self.assertEqual(res.data["count"], 3)
        self.assertEqual(res.data["total_pages"], 1)
        self.assertEqual(res.data["current_page"], 1)
        self.assertFalse(res.data["next_page"])
        self.assertFalse(res.data["previous_page"])
        self.assertEqual(len(res.data["results"]), 3)

    def test_history_is_newest_first_and_page_param_selects(self):
        older = self.create_order()
        res = self.checkout(city="Pune")
        newer = Order.objects.get(id=res.data["id"])

        with override_settings(ORDER_HISTORY_PAGE_SIZE=1):
            page1 = self.client.get("/api/orders/")
            self.assertEqual(page1.data["total_pages"], 2)
            self.assertEqual(
                [row["id"] for row in page1.data["results"]], [newer.id]
            )
            self.assertTrue(page1.data["next_page"])
            self.assertFalse(page1.data["previous_page"])

            page2 = self.client.get("/api/orders/?page=2")
            self.assertEqual(
                [row["id"] for row in page2.data["results"]], [older.id]
            )
            self.assertTrue(page2.data["previous_page"])
            self.assertFalse(page2.data["next_page"])

    def test_page_size_param_overrides_default(self):
        self._checkout_distinct_orders(3)
        res = self.client.get("/api/orders/?page_size=2")
        self.assertEqual(len(res.data["results"]), 2)
        self.assertEqual(res.data["total_pages"], 2)
        self.assertEqual(res.data["current_page"], 1)

    def test_page_size_is_capped(self):
        self._checkout_distinct_orders(3)
        with override_settings(ORDER_HISTORY_MAX_PAGE_SIZE=2):
            res = self.client.get("/api/orders/?page_size=1000")
        self.assertEqual(len(res.data["results"]), 2)

    def test_invalid_page_size_falls_back_to_default(self):
        self._checkout_distinct_orders(2)
        for raw in ("abc", "0", "-5"):
            with self.subTest(page_size=raw):
                res = self.client.get(f"/api/orders/?page_size={raw}")
                self.assertEqual(len(res.data["results"]), 2)  # default (10)

    def test_invalid_page_number_falls_back_to_first_page(self):
        self._checkout_distinct_orders(2)
        res = self.client.get("/api/orders/?page=not-a-page")
        self.assertEqual(res.data["current_page"], 1)
        self.assertEqual(len(res.data["results"]), 2)

    def test_empty_history_is_a_valid_first_page(self):
        res = self.client.get("/api/orders/")
        self.assertEqual(res.data["count"], 0)
        self.assertEqual(res.data["total_pages"], 1)
        self.assertEqual(res.data["results"], [])


@tag("orders")
class BusinessEventTimestampTests(OrderTestBase):
    """[R-8.16] Business-event timestamps (spec 8.3 "Timestamps"): the order
    lifecycle carries one named column per business event, NULL until the
    event happens, written exactly once by the code path that performs it and
    never mutated afterwards — ``updated_at`` is never overloaded to stand
    for an event. paid_at/cancelled_at have live writers (verify_payment /
    admin cancel); fulfilled_at/shipped_at/delivered_at/refunded_at are the
    named pattern the later fulfilment and refund sections write.
    """

    BUSINESS_FIELDS = (
        "paid_at",
        "fulfilled_at",
        "shipped_at",
        "delivered_at",
        "cancelled_at",
        "refunded_at",
    )

    def _verify_payload(self, order):
        return {
            "order_id": order.id,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": "pay_TS001",
            "razorpay_signature": "sig",
        }

    def _admin_client(self):
        """Superuser session on a fresh client: logging in on the API client
        would swap the session and drop the buyer's session cart under the
        order being created."""
        if not User.objects.filter(username="tsboss").exists():
            User.objects.create_superuser(
                "tsboss", "tsboss@example.com", "S3cure-Passphrase!"
            )
        admin_client = self.fresh_client()
        self.assertTrue(
            admin_client.login(username="tsboss", password="S3cure-Passphrase!")
        )
        return admin_client

    def _run_admin_action(self, action, orders):
        """Bulk action through the real admin UI (mirrors test_e2e_admin_ops)."""
        admin_client = self._admin_client()
        return admin_client.post(
            "/admin/orders/order/",
            {
                "action": action,
                "_selected_action": [str(order.id) for order in orders],
                "select_across": "0",
            },
            follow=True,
        )

    def _admin_login(self):
        if not User.objects.filter(username="tsboss").exists():
            User.objects.create_superuser(
                "tsboss", "tsboss@example.com", "S3cure-Passphrase!"
            )
        self.assertTrue(
            self.client.login(username="tsboss", password="S3cure-Passphrase!")
        )

    def test_new_order_starts_with_all_business_timestamps_null(self):
        order = self.create_order()
        for field in self.BUSINESS_FIELDS:
            with self.subTest(field=field):
                self.assertIsNone(getattr(order, field))

    def test_verify_writes_paid_at_once_and_replay_never_overwrites(self):
        order = self.create_order()
        self.razorpay_mock(order_id="order_TS")
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()

        first = self.client.post(
            "/api/orders/payment/verify/", self._verify_payload(order), format="json"
        )
        self.assertEqual(first.status_code, 200, first.data)
        order.refresh_from_db()
        self.assertIsNotNone(order.paid_at)
        self.assertGreaterEqual(order.paid_at, order.created_at)
        first_paid_at = order.paid_at

        # the already-processed gate rejects the replay; paid_at must keep
        # the exact first value — a re-verify never re-stamps the event
        replay = self.client.post(
            "/api/orders/payment/verify/", self._verify_payload(order), format="json"
        )
        self.assertEqual(replay.status_code, 400, replay.data)
        order.refresh_from_db()
        self.assertEqual(order.paid_at, first_paid_at)

    def test_failed_verify_leaves_paid_at_null(self):
        order = self.create_order()
        client_mock = self.razorpay_mock(order_id="order_TSFAIL")
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()
        self.razorpay_fail_signature(client_mock)

        res = self.client.post(
            "/api/orders/payment/verify/", self._verify_payload(order), format="json"
        )

        self.assertEqual(res.status_code, 400, res.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertIsNone(order.paid_at)

    def test_admin_cancel_bulk_stamps_cancelled_at_once(self):
        order = self.create_order()  # pending, unpaid

        res = self._run_admin_action("cancel_pending", [order])
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertIsNotNone(order.cancelled_at)
        # cancelling before payment writes exactly one event timestamp:
        # a cancelled order was never paid, shipped or refunded
        self.assertIsNone(order.paid_at)
        self.assertIsNone(order.fulfilled_at)
        self.assertIsNone(order.shipped_at)
        self.assertIsNone(order.delivered_at)
        self.assertIsNone(order.refunded_at)

        # the pending filter skips already-cancelled rows, so a re-run of
        # the action never mutates the stamp
        first_cancelled_at = order.cancelled_at
        res = self._run_admin_action("cancel_pending", [order])
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.cancelled_at, first_cancelled_at)

    def test_admin_change_form_cancel_stamps_cancelled_at(self):
        """The single-object path: pending -> cancelled through the change
        form is a legal transition, so it must stamp cancelled_at too."""
        self._admin_login()
        order = Order.objects.create(
            user=self.buyer,
            full_name="TS Buyer",
            phone="9876543210",
            address="1 Timeline Way",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal("10.00"),
        )
        res = self.client.post(
            f"/admin/orders/order/{order.id}/change/",
            {
                "user": order.user_id,
                "full_name": order.full_name,
                "phone": order.phone,
                "address": order.address,
                "city": order.city,
                "state": order.state,
                "pincode": order.pincode,
                "status": "cancelled",
                "_save": "Save",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertIsNotNone(order.cancelled_at)

    def test_business_timestamps_exposed_read_only_everywhere(self):
        order = self.create_order()

        for field in self.BUSINESS_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, OrderSerializer.Meta.fields)
                self.assertIn(field, OrderSerializer.Meta.read_only_fields)
                self.assertIn(field, OrderAdmin.readonly_fields)
                self.assertIn(
                    field, dict(OrderAdmin.fieldsets)["Timestamps"]["fields"]
                )

        # the changelist carries the two live event stamps beside the row
        self.assertIn("paid_at", OrderAdmin.list_display)
        self.assertIn("cancelled_at", OrderAdmin.list_display)

        listing = self.client.get("/api/orders/")
        self.assertEqual(listing.status_code, 200, listing.data)
        row = listing.data["results"][0]
        self.assertEqual(row["id"], order.id)
        for field in self.BUSINESS_FIELDS:
            self.assertIsNone(row[field])  # NULL until the event


@tag("orders")
class OrderIndexSchemaTests(ApiTestCase):
    """SPEC-8-05: spec 8.3 "Indexes" starting set for orders_order.

    The two prescribed composites (2557 "Customer ID and order creation
    date", 2567 "Frequently queried status/date combinations") land as
    explicit Meta.indexes; the order-number (2555) and payment-provider
    reference (2559) prescriptions stay satisfied by their UNIQUE
    constraints — a unique constraint already implies a backing index, so
    minting a second index on those columns would be a duplicate. These
    pins make both halves conscious: the composites must exist at the DB
    level, and the constraint-covered columns must not grow duplicates.
    """

    def _order_constraints(self):
        with connection.cursor() as cursor:
            return connection.introspection.get_constraints(
                cursor, Order._meta.db_table
            )

    def _explicit_index_columns(self):
        return [
            info["columns"]
            for info in self._order_constraints().values()
            if info["index"]
        ]

    def test_meta_declares_exactly_the_spec_starting_composites(self):
        """The model-level starting set: newest-first per customer (2557)
        and the status/date combination (2567). Later indexes must come
        from measured query patterns (2569), i.e. as a conscious edit to
        this set, never by silent accretion."""
        declared = {tuple(index.fields) for index in Order._meta.indexes}
        self.assertEqual(
            declared,
            {("user", "-created_at"), ("status", "created_at")},
        )

    def test_customer_and_creation_date_composite_exists_at_db_level(self):
        matching = [
            columns
            for columns in self._explicit_index_columns()
            if columns == ["user_id", "created_at"]
        ]
        self.assertTrue(
            matching, "no (user_id, created_at) index on orders_order"
        )

    def test_status_and_creation_date_composite_exists_at_db_level(self):
        matching = [
            columns
            for columns in self._explicit_index_columns()
            if columns == ["status", "created_at"]
        ]
        self.assertTrue(
            matching, "no (status, created_at) index on orders_order"
        )

    def test_order_number_stays_satisfied_by_its_unique_constraint(self):
        """2555: order_number's covering constraint(s) are all UNIQUE —
        unique=True already provides the lookup index (and doubles as the
        IntegrityError concurrency authority for number minting) — so no
        duplicate explicit index is stacked on top."""
        covering = [
            info
            for info in self._order_constraints().values()
            if info["columns"] == ["order_number"]
        ]
        self.assertTrue(covering, "no constraint on order_number at all")
        self.assertTrue(
            all(info["unique"] for info in covering),
            f"order_number grew a non-unique duplicate index: {covering}",
        )

    def test_payment_provider_references_stay_constraint_covered(self):
        """2559: no Payment model exists — the provider references live on
        Order (razorpay_order_id / razorpay_payment_id), each unique=True,
        whose backing unique indexes are the prescribed starting indexes."""
        for column in ("razorpay_order_id", "razorpay_payment_id"):
            with self.subTest(column=column):
                covering = [
                    info
                    for info in self._order_constraints().values()
                    if info["columns"] == [column]
                ]
                self.assertTrue(covering, f"no constraint on {column}")
                self.assertTrue(
                    all(info["unique"] for info in covering),
                    f"{column} grew a non-unique duplicate index: {covering}",
                )


@tag("orders")
class AdminOrdersApiTests(ApiTestCase):
    """SPEC-9-07 [R-9.4.8]-[R-9.4.11] (spec lines 3285-3317, Orders module):
    the admin orders JSON seam. Reads serve every order under ``orders.read``;
    writes reuse the admin state machine (``transition_allowed``) and the
    capability permission classes that were defined but unmounted. Errors
    land in the uniform envelope (SPEC-9-03) — every error assertion pins
    the ``error``/``code``/``details`` shape."""

    LIST_URL = "/api/admin/orders/"
    V1_LIST_URL = "/api/v1/admin/orders/"

    def setUp(self):
        self.buyer = self.make_user("buyer")
        self.order = self._make_order(self.buyer)

    @staticmethod
    def _make_order(user, status="pending", **overrides):
        fields = dict(
            user=user,
            full_name="Seam Buyer",
            phone="9999999999",
            address="1 Test Lane",
            city="Indore",
            state="MP",
            pincode="452001",
            total_amount=Decimal("750.00"),
        )
        fields.update(overrides)
        order = Order.objects.create(**fields)
        if status != "pending":
            Order.objects.filter(pk=order.pk).update(status=status)
            order.refresh_from_db()
        return order

    @staticmethod
    def _user_with_role(username, role):
        from django.contrib.auth.models import Group

        from common.roles import ROLE_ADMIN

        user = User.objects.create_user(
            username, f"{username}@example.com", "S3cure-Passphrase!"
        )
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    # permission matrix -------------------------------------------------------
    def test_anonymous_list_is_403_in_the_uniform_envelope(self):
        res = self.client.get(self.LIST_URL)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(
            res.data["error"], "You do not have permission to perform this action."
        )
        self.assertEqual(res.data["code"], "permission_denied")
        self.assertIn("details", res.data)

    def test_customer_without_staff_role_is_403_on_list(self):
        self.client.force_authenticate(self.buyer)
        res = self.client.get(self.LIST_URL)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "permission_denied")

    def test_finance_role_reads_every_order_unscoped(self):
        """orders.read = support/finance/admin: a non-owner staff role still
        sees the order — this seam is the unscoped twin of the customer list."""
        other_buyer = self.make_user("other_buyer")
        self._make_order(other_buyer)
        finance = self._user_with_role("finmgr", ROLE_FINANCE)
        self.client.force_authenticate(finance)
        res = self.client.get(self.LIST_URL)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 2)

    def test_detail_anonymous_and_unprivileged_are_403(self):
        for client_user in (None, self.buyer):
            client = self.fresh_client()
            if client_user is not None:
                client.force_authenticate(client_user)
            res = client.get(f"{self.LIST_URL}{self.order.id}/")
            self.assertEqual(res.status_code, 403)
            self.assertEqual(res.data["code"], "permission_denied")

    def test_finance_role_reads_detail(self):
        finance = self._user_with_role("finmgr", ROLE_FINANCE)
        self.client.force_authenticate(finance)
        res = self.client.get(f"{self.LIST_URL}{self.order.id}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], self.order.id)
        self.assertEqual(res.data["order_number"], self.order.order_number)
        self.assertEqual(res.data["status"], "pending")

    def test_write_endpoints_deny_orders_read_only_role(self):
        """The least-privilege split: finance may read but may neither fulfil
        nor cancel — and nothing moves when they try."""
        from django.contrib.admin.models import LogEntry

        finance = self._user_with_role("finmgr", ROLE_FINANCE)
        self.client.force_authenticate(finance)
        for path in (
            f"{self.LIST_URL}{self.order.id}/fulfill/",
            f"{self.LIST_URL}{self.order.id}/cancel/",
        ):
            res = self.client.post(path)
            self.assertEqual(res.status_code, 403, res.data)
            self.assertEqual(res.data["code"], "permission_denied")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "pending")
        self.assertEqual(LogEntry.objects.count(), 0)

    # list + detail -------------------------------------------------------------
    def test_list_uses_the_house_page_number_envelope(self):
        admin = self.make_staff()
        self.client.force_authenticate(admin)
        for i in range(3):
            self._make_order(self.buyer)
        res = self.client.get(self.LIST_URL)
        self.assertEqual(res.status_code, 200, res.data)
        for key in ("count", "total_pages", "current_page", "results"):
            self.assertIn(key, res.data)
        self.assertEqual(res.data["count"], 4)
        self.assertEqual(len(res.data["results"]), 4)

    def test_detail_unknown_id_is_uniform_404(self):
        admin = self.make_staff()
        self.client.force_authenticate(admin)
        res = self.client.get(f"{self.LIST_URL}999999/")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["error"], "Order not found")
        self.assertEqual(res.data["code"], "not_found")

    def test_list_and_detail_served_on_v1_admin_mirror(self):
        admin = self.make_staff()
        self.client.force_authenticate(admin)
        res = self.client.get(self.V1_LIST_URL)
        self.assertEqual(res.status_code, 200, res.data)
        res = self.client.get(f"{self.V1_LIST_URL}{self.order.id}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], self.order.id)

    # fulfilment ------------------------------------------------------------------
    def test_fulfill_advances_pending_to_confirmed_and_audits(self):
        from django.contrib.admin.models import CHANGE, LogEntry

        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        res = self.client.post(f"{self.LIST_URL}{self.order.id}/fulfill/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], "confirmed")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")
        entry = LogEntry.objects.get(object_id=str(self.order.id))
        self.assertEqual(entry.user, support)
        self.assertEqual(entry.action_flag, CHANGE)
        self.assertIn("Fulfilled via API", entry.change_message)

    def test_fulfill_walks_one_step_per_call(self):
        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        for expected in ("confirmed", "shipped", "delivered"):
            res = self.client.post(f"{self.LIST_URL}{self.order.id}/fulfill/")
            self.assertEqual(res.status_code, 200, res.data)
            self.assertEqual(res.data["status"], expected)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "delivered")

    def test_fulfill_rejects_terminal_states_in_the_envelope(self):
        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        for status_value in ("delivered", "cancelled"):
            order = self._make_order(self.buyer, status=status_value)
            res = self.client.post(f"{self.LIST_URL}{order.id}/fulfill/")
            self.assertEqual(res.status_code, 409, res.data)
            self.assertEqual(res.data["code"], "conflict")
            self.assertIn(status_value, res.data["error"])
            self.assertIn("allowed", res.data["details"])
            order.refresh_from_db()
            self.assertEqual(order.status, status_value)

    def test_fulfill_unknown_order_is_404(self):
        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        res = self.client.post(f"{self.LIST_URL}999999/fulfill/")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "not_found")

    def test_fulfill_served_on_v1_admin_mirror(self):
        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        res = self.client.post(f"{self.V1_LIST_URL}{self.order.id}/fulfill/")
        self.assertEqual(res.status_code, 200, res.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")

    # cancellation ----------------------------------------------------------------
    def test_cancel_marks_pending_cancelled_and_stamps_cancelled_at(self):
        from django.contrib.admin.models import CHANGE, LogEntry

        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        res = self.client.post(f"{self.LIST_URL}{self.order.id}/cancel/")
        self.assertEqual(res.status_code, 200, res.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        self.assertIsNotNone(self.order.cancelled_at)
        entry = LogEntry.objects.get(object_id=str(self.order.id))
        self.assertEqual(entry.action_flag, CHANGE)
        self.assertIn("Cancelled via API", entry.change_message)

    def test_cancel_rejects_paid_order_with_refund_hint(self):
        paid = self._make_order(self.buyer, status="confirmed")
        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        res = self.client.post(f"{self.LIST_URL}{paid.id}/cancel/")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data["code"], "conflict")
        self.assertIn("refund", res.data["error"])
        paid.refresh_from_db()
        self.assertEqual(paid.status, "confirmed")
        self.assertIsNone(paid.cancelled_at)

    def test_cancel_rejects_delivered_order(self):
        """The pinned illegal transition: delivered has no outgoing edges in
        the machine, so the cancel endpoint can never sweep it."""
        delivered = self._make_order(self.buyer, status="delivered")
        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        res = self.client.post(f"{self.LIST_URL}{delivered.id}/cancel/")
        self.assertEqual(res.status_code, 409, res.data)
        delivered.refresh_from_db()
        self.assertEqual(delivered.status, "delivered")

    def test_cancel_is_idempotent_on_an_already_cancelled_order(self):
        from django.contrib.admin.models import LogEntry

        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        first = self.client.post(f"{self.LIST_URL}{self.order.id}/cancel/")
        self.assertEqual(first.status_code, 200, first.data)
        self.order.refresh_from_db()
        stamped = self.order.cancelled_at
        second = self.client.post(f"{self.LIST_URL}{self.order.id}/cancel/")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["message"], "Order is already cancelled")
        self.order.refresh_from_db()
        self.assertEqual(self.order.cancelled_at, stamped)
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_cancel_unknown_order_is_404(self):
        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        res = self.client.post(f"{self.LIST_URL}999999/cancel/")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "not_found")

    def test_cancel_served_on_v1_admin_mirror(self):
        support = self._user_with_role("supp", ROLE_SUPPORT)
        self.client.force_authenticate(support)
        res = self.client.post(f"{self.V1_LIST_URL}{self.order.id}/cancel/")
        self.assertEqual(res.status_code, 200, res.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")


@tag("orders")
class BulkSetStatusRaceGuardTests(ApiTestCase):
    """SPEC-6-04 audit advisory, hardened in SPEC-9-07: the bulk updater's
    UPDATE must re-apply the status__in predicate so an out-of-set row can
    never be swept. The race is simulated deterministically: the pk snapshot
    materialises first, then a concurrent actor legally cancels one matched
    pending order, and only then does the UPDATE run."""

    def test_out_of_set_row_is_never_swept_by_the_bulk_update(self):
        from django.contrib.admin import site as admin_site
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.db import models as django_models
        from django.test import RequestFactory

        admin = OrderAdmin(Order, admin_site)
        staff = self.make_staff()
        request = RequestFactory().post("/admin/orders/order/")
        request.user = staff
        request.session = {}
        request._messages = FallbackStorage(request)

        victim = Order.objects.create(
            user=self.make_user("race_buyer"),
            full_name="V", phone="1", address="a", city="c", state="s",
            pincode="1", total_amount=Decimal("10.00"),
        )
        control = Order.objects.create(
            user=victim.user,
            full_name="C", phone="1", address="a", city="c", state="s",
            pincode="1", total_amount=Decimal("10.00"),
        )

        class RacingQuerySet(django_models.QuerySet):
            raced = False

            def values_list(self, *args, **kwargs):
                pks = list(super().values_list(*args, **kwargs))
                if not RacingQuerySet.raced:
                    RacingQuerySet.raced = True
                    # The interleave: the snapshot above already saw both
                    # rows pending; the victim flips before the UPDATE runs.
                    Order.objects.filter(pk=victim.pk).update(status="cancelled")
                return pks

        admin._bulk_set_status(request, RacingQuerySet(Order), "confirmed")

        victim.refresh_from_db()
        control.refresh_from_db()
        self.assertEqual(victim.status, "cancelled")
        self.assertEqual(control.status, "confirmed")

        # [R-10.12] SPEC-10-02: the reworked per-row path keeps the same
        # skip semantics at the audit layer — the row it skipped gets NO
        # event for the sweep it never underwent, the swept row gets
        # exactly one.
        self.assertFalse(
            OrderStatusEvent.objects.filter(
                order=victim, to_status="confirmed"
            ).exists()
        )
        swept_event = OrderStatusEvent.objects.get(order=control)
        self.assertEqual(swept_event.from_status, "pending")
        self.assertEqual(swept_event.to_status, "confirmed")


# ==================================
# [R-10.1] SPEC-10-01a: order lifecycle dimensions
# ==================================

@tag("orders")
class OrderStateSourceTests(SimpleTestCase):
    """orders.state is the single source of truth for the order machine:
    the constants that used to live in models/admin/views are aliases of
    the same objects, and the machine's current edges are pinned here so a
    consolidation slip (a redefined copy anywhere) cannot go unnoticed."""

    def test_machine_constants_are_single_sourced(self):
        from orders import admin as orders_admin
        from orders import views as orders_views

        self.assertIs(Order.STATUS_CHOICES, order_state.STATUS_CHOICES)
        self.assertIs(orders_admin.ALLOWED_TRANSITIONS, order_state.ALLOWED_TRANSITIONS)
        self.assertIs(orders_admin.transition_allowed, order_state.transition_allowed)
        self.assertIs(orders_views.ALLOWED_TRANSITIONS, order_state.ALLOWED_TRANSITIONS)
        self.assertIs(orders_views.transition_allowed, order_state.transition_allowed)
        self.assertIs(orders_views.ADMIN_FULFILMENT_NEXT, order_state.ADMIN_FULFILMENT_NEXT)

    def test_transition_allowed_pins_the_current_machine(self):
        # The legal edges (exactly one step forward, cancel from pending).
        for old, new in [
            ("pending", "confirmed"),
            ("pending", "cancelled"),
            ("confirmed", "shipped"),
            ("shipped", "delivered"),
        ]:
            with self.subTest(old=old, new=new):
                self.assertTrue(order_state.transition_allowed(old, new))
        # Self-transitions (idempotent replays) and everything else are not.
        for old, new in [
            ("pending", "shipped"),
            ("confirmed", "cancelled"),
            ("confirmed", "delivered"),
            ("shipped", "confirmed"),
            ("delivered", "pending"),
            ("cancelled", "pending"),
            ("unknown", "confirmed"),
        ]:
            with self.subTest(old=old, new=new):
                self.assertFalse(order_state.transition_allowed(old, new))
        for old in ("pending", "confirmed", "shipped", "delivered", "cancelled"):
            with self.subTest(old=old):
                self.assertTrue(order_state.transition_allowed(old, old))

    def test_admin_fulfilment_next_pins_the_one_step_map(self):
        self.assertEqual(
            order_state.ADMIN_FULFILMENT_NEXT,
            {"pending": "confirmed", "confirmed": "shipped", "shipped": "delivered"},
        )

    def test_backfill_mapping_is_total_over_legacy_statuses(self):
        self.assertEqual(
            set(order_state.LEGACY_STATUS_DIMENSIONS),
            {s for s, _ in Order.STATUS_CHOICES},
        )


@tag("orders")
class LifecycleDimensionsFieldTests(OrderTestBase):
    """[R-10.1] The additive dimension fields: initial states, the
    legacy→dimensions mapping table, and read-only serializer exposure."""

    EXPECTED_DIMENSIONS = {
        "pending": ("pending", "unfulfilled"),
        "confirmed": ("captured", "unfulfilled"),
        "shipped": ("captured", "fulfilled"),
        "delivered": ("captured", "fulfilled"),
        "cancelled": ("pending", "unfulfilled"),
    }

    def test_every_legacy_status_maps_to_both_dimensions(self):
        payment_values = {v for v, _ in order_state.PAYMENT_STATUS_CHOICES}
        fulfilment_values = {v for v, _ in order_state.FULFILMENT_STATUS_CHOICES}
        for legacy_status in [s for s, _ in Order.STATUS_CHOICES]:
            with self.subTest(legacy_status=legacy_status):
                payment, fulfilment = order_state.LEGACY_STATUS_DIMENSIONS[legacy_status]
                self.assertEqual(
                    (payment, fulfilment),
                    self.EXPECTED_DIMENSIONS[legacy_status],
                )
                self.assertIn(payment, payment_values)
                self.assertIn(fulfilment, fulfilment_values)

    def test_dimension_helpers_agree_with_the_mapping(self):
        for legacy_status, (payment, fulfilment) in self.EXPECTED_DIMENSIONS.items():
            with self.subTest(legacy_status=legacy_status):
                self.assertEqual(order_state.payment_for_status(legacy_status), payment)
                self.assertEqual(order_state.fulfilment_for_status(legacy_status), fulfilment)

    def test_new_orders_start_pending_and_unfulfilled(self):
        order = self.create_order()

        self.assertEqual(order.payment_status, "pending")
        self.assertEqual(order.fulfilment_status, "unfulfilled")

    def test_dimensions_exposed_read_only_in_order_serializer(self):
        self.assertIn("payment_status", OrderSerializer.Meta.fields)
        self.assertIn("fulfilment_status", OrderSerializer.Meta.fields)
        self.assertIn("payment_status", OrderSerializer.Meta.read_only_fields)
        self.assertIn("fulfilment_status", OrderSerializer.Meta.read_only_fields)

        order = self.create_order()

        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 200, res.data)
        row = next(r for r in res.data["results"] if r["id"] == order.id)
        self.assertEqual(row["payment_status"], "pending")
        self.assertEqual(row["fulfilment_status"], "unfulfilled")


@tag("orders")
class LifecycleBackfillMigrationTests(TransactionTestCase):
    """[R-10.1] The 0012 backfill is total: a row with any legacy status
    value migrates to BOTH dimensions per the state.py mapping. Same shape
    as CurrencyBackfillMigrationTests: drop to 0011, raw-insert rows the
    way a pre-dimension store had them, migrate to head, assert."""

    def test_0012_backfills_both_dimensions_for_every_legacy_status(self):
        call_command("migrate", "orders", "0011", verbosity=0, interactive=False)
        user_id = User.objects.create_user(
            "backfill10", "backfill10@example.com", "S3cure-Passphrase!"
        ).id
        # The dimension columns do not exist at 0011, so raw SQL is the
        # only honest way to reproduce a pre-dimension row per legacy status.
        with connection.cursor() as cursor:
            for legacy_status in [s for s, _ in order_state.STATUS_CHOICES]:
                cursor.execute(
                    "INSERT INTO orders_order (user_id, full_name, phone, address,"
                    " city, state, pincode, status, currency, discount_amount,"
                    " total_amount, created_at, updated_at)"
                    " VALUES (%s, %s, '1', 'a', 'c', 's', '1', %s, 'INR',"
                    " 0, 100.00, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    [user_id, f"Backfill-{legacy_status}", legacy_status],
                )

        call_command("migrate", "orders", verbosity=0, interactive=False)

        for legacy_status, (payment, fulfilment) in (
            order_state.LEGACY_STATUS_DIMENSIONS.items()
        ):
            with self.subTest(legacy_status=legacy_status):
                order = Order.objects.get(full_name=f"Backfill-{legacy_status}")
                self.assertEqual(order.payment_status, payment)
                self.assertEqual(order.fulfilment_status, fulfilment)


@tag("orders")
class LifecycleWiringTests(OrderTestBase):
    """[R-10.1] SPEC-10-01b: the writers keep the two dimension fields in
    sync with the legacy status. The payment dimension moves ONLY with
    payment events (verify_payment — COD/failure writers are SPEC-10-04);
    the admin surface and the 9-07 JSON seam move the fulfilment dimension
    with every fulfilment transition, and admin never touches payment."""

    def _verify_payload(self, order):
        return {
            "order_id": order.id,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": "pay_DIM001",
            "razorpay_signature": "sig",
        }

    def _admin_client(self):
        if not User.objects.filter(username="dimboss").exists():
            User.objects.create_superuser(
                "dimboss", "dimboss@example.com", "S3cure-Passphrase!"
            )
        admin_client = self.fresh_client()
        self.assertTrue(
            admin_client.login(username="dimboss", password="S3cure-Passphrase!")
        )
        return admin_client

    def _run_admin_action(self, action, orders):
        """Bulk action through the real admin UI (mirrors the 8-16 tests)."""
        admin_client = self._admin_client()
        return admin_client.post(
            "/admin/orders/order/",
            {
                "action": action,
                "_selected_action": [str(order.id) for order in orders],
                "select_across": "0",
            },
            follow=True,
        )

    def _staff_client(self, username):
        from django.contrib.auth.models import Group

        from common.roles import ROLE_SUPPORT

        user = User.objects.create_user(
            username, f"{username}@example.com", "S3cure-Passphrase!"
        )
        user.groups.add(Group.objects.get_or_create(name=ROLE_SUPPORT)[0])
        client = self.fresh_client()
        client.force_authenticate(user)
        return client

    def _order_with_status(self, user, status):
        """A row exactly as the writers would have left it: the status AND
        both dimension columns per the state.py mapping (a raw status-only
        update would strand the dimensions stale, a state no writer makes)."""
        order = Order.objects.create(
            user=user,
            full_name="Dim Buyer",
            phone="1",
            address="a",
            city="c",
            state="s",
            pincode="1",
            total_amount=Decimal("10.00"),
        )
        Order.objects.filter(pk=order.pk).update(
            status=status,
            payment_status=order_state.payment_for_status(status),
            fulfilment_status=order_state.fulfilment_for_status(status),
        )
        order.refresh_from_db()
        return order

    # ——— payment dimension: verify_payment only ————————————————

    def test_verify_payment_captures_the_payment_dimension(self):
        order = self.create_order()
        self.razorpay_mock(order_id="order_DIM")
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()  # picks up the minted razorpay_order_id

        res = self.client.post(
            "/api/orders/payment/verify/", self._verify_payload(order), format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()
        # the legacy status semantics are untouched (8-04 pins stay green)
        self.assertEqual(order.status, "confirmed")
        self.assertIsNotNone(order.paid_at)
        # ...and the payment dimension moved with the payment event alone
        self.assertEqual(order.payment_status, "captured")
        self.assertEqual(order.fulfilment_status, "unfulfilled")

    def test_verify_rollback_leaves_the_payment_dimension_pending(self):
        """The dimension write sits inside verify_payment's atomic block: an
        audit failure after it must roll the captured dimension back with
        everything else — effect and trail never disagree."""
        order = self.create_order()
        self.razorpay_mock(order_id="order_DIMRB")
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()  # picks up the minted razorpay_order_id
        self.client.raise_request_exception = False
        with mock.patch.object(
            AuditEvent, "record", side_effect=RuntimeError("down")
        ):
            res = self.client.post(
                "/api/orders/payment/verify/",
                self._verify_payload(order),
                format="json",
            )
        self.assertEqual(res.status_code, 500)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertIsNone(order.paid_at)
        self.assertEqual(order.payment_status, "pending")
        self.assertEqual(order.fulfilment_status, "unfulfilled")

    # ——— fulfilment dimension: admin surface ————————————————————
    # Admin syncs fulfilment_status and must NEVER set payment_status.

    def test_admin_change_form_moves_dimension_with_the_status(self):
        order = self._order_with_status(self.buyer, "confirmed")
        admin_client = self._admin_client()

        res = admin_client.post(
            f"/admin/orders/order/{order.id}/change/",
            {
                "user": order.user_id,
                "full_name": order.full_name,
                "phone": order.phone,
                "address": order.address,
                "city": order.city,
                "state": order.state,
                "pincode": order.pincode,
                "status": "shipped",
                "_save": "Save",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(order.fulfilment_status, "fulfilled")
        # admin never touches the payment dimension: the captured value the
        # row carried in stays exactly as it was
        self.assertEqual(order.payment_status, "captured")

    def test_admin_change_form_illegal_transition_moves_nothing(self):
        order = self._order_with_status(self.buyer, "shipped")
        admin_client = self._admin_client()

        res = admin_client.post(
            f"/admin/orders/order/{order.id}/change/",
            {
                "user": order.user_id,
                "full_name": order.full_name,
                "phone": order.phone,
                "address": order.address,
                "city": order.city,
                "state": order.state,
                "pincode": order.pincode,
                "status": "cancelled",
                "_save": "Save",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(order.fulfilment_status, "fulfilled")

    def test_bulk_ship_syncs_dimension_per_row_and_skips_out_of_set(self):
        confirmed = self._order_with_status(self.buyer, "confirmed")
        pending = self._order_with_status(self.buyer, "pending")

        res = self._run_admin_action("mark_shipped", [confirmed, pending])
        self.assertEqual(res.status_code, 200)
        confirmed.refresh_from_db()
        pending.refresh_from_db()
        # the in-set row sweeps both columns in the SAME UPDATE...
        self.assertEqual(confirmed.status, "shipped")
        self.assertEqual(confirmed.fulfilment_status, "fulfilled")
        # ...and admin never touches the payment dimension, paid row included
        self.assertEqual(confirmed.payment_status, "captured")
        # ...the out-of-set row is only counted as skipped (9-07 hardening)
        self.assertEqual(pending.status, "pending")
        self.assertEqual(pending.fulfilment_status, "unfulfilled")

    def test_admin_bulk_cancel_keeps_the_payment_dimension_untouched(self):
        order = self.create_order()  # pending, unpaid

        res = self._run_admin_action("cancel_pending", [order])
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(order.fulfilment_status, "unfulfilled")
        self.assertEqual(order.payment_status, "pending")

    # ——— fulfilment dimension: the 9-07 JSON seam ———————————————

    def test_json_seam_fulfil_walk_syncs_fulfilment_dimension(self):
        order = self.create_order()
        support = self._staff_client("dimsupp")

        for expected_status, expected_fulfilment in (
            ("confirmed", "unfulfilled"),
            ("shipped", "fulfilled"),
            ("delivered", "fulfilled"),
        ):
            res = support.post(f"/api/admin/orders/{order.id}/fulfill/")
            self.assertEqual(res.status_code, 200, res.data)
            self.assertEqual(res.data["status"], expected_status)
            order.refresh_from_db()
            self.assertEqual(order.status, expected_status)
            self.assertEqual(order.fulfilment_status, expected_fulfilment)

    def test_json_seam_cancel_syncs_fulfilment_dimension(self):
        order = self.create_order()
        support = self._staff_client("dimcancel")

        res = support.post(f"/api/admin/orders/{order.id}/cancel/")
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(order.fulfilment_status, "unfulfilled")
        self.assertEqual(order.payment_status, "pending")

    # ——— the dimensions surface through the serializer —————————

    def test_serializer_shows_updated_dimensions_after_transitions(self):
        order = self.create_order()
        self.razorpay_mock(order_id="order_DIMSER")
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()  # picks up the minted razorpay_order_id
        res = self.client.post(
            "/api/orders/payment/verify/", self._verify_payload(order), format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)

        support = self._staff_client("dimser")
        for _ in range(2):  # confirmed -> shipped
            res = support.post(f"/api/admin/orders/{order.id}/fulfill/")
            self.assertEqual(res.status_code, 200, res.data)

        detail = support.get(f"/api/admin/orders/{order.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["payment_status"], "captured")
        self.assertEqual(detail.data["fulfilment_status"], "fulfilled")

        listing = self.client.get("/api/orders/")
        self.assertEqual(listing.status_code, 200, listing.data)
        row = next(r for r in listing.data["results"] if r["id"] == order.id)
        self.assertEqual(row["payment_status"], "captured")
        self.assertEqual(row["fulfilment_status"], "fulfilled")


# ==================================
# [R-10.12]/[R-10.17]/[R-10.18] SPEC-10-02: transition audit trail
# ==================================

@tag("orders")
class TransitionAuditTrailTests(OrderTestBase):
    """[R-10.12] Every legal order-status transition appends one immutable
    OrderStatusEvent row (from→to, actor, trigger, timestamp) inside the
    SAME transaction as the transition it records ([R-10.18] append-only:
    a rolled-back writer leaves no event, a committed event can never lack
    its transition, and no writer can mutate a row afterwards). The
    privileged-action LogEntry trail (SPEC-7-01) is a separate record and
    is not touched here."""

    def _verify_payload(self, order):
        return {
            "order_id": order.id,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": "pay_AUD001",
            "razorpay_signature": "sig",
        }

    def _admin_login(self):
        if not User.objects.filter(username="auditboss").exists():
            User.objects.create_superuser(
                "auditboss", "auditboss@example.com", "S3cure-Passphrase!"
            )
        self.assertTrue(
            self.client.login(username="auditboss", password="S3cure-Passphrase!")
        )

    def _admin_user(self):
        return User.objects.get(username="auditboss")

    def _run_admin_action(self, action, orders):
        """Bulk action through the real admin UI (mirrors the 8-16 tests)."""
        self._admin_login()
        return self.client.post(
            "/admin/orders/order/",
            {
                "action": action,
                "_selected_action": [str(order.id) for order in orders],
                "select_across": "0",
            },
            follow=True,
        )

    def _staff_client(self, username):
        from django.contrib.auth.models import Group

        from common.roles import ROLE_SUPPORT

        user = User.objects.create_user(
            username, f"{username}@example.com", "S3cure-Passphrase!"
        )
        user.groups.add(Group.objects.get_or_create(name=ROLE_SUPPORT)[0])
        client = self.fresh_client()
        client.force_authenticate(user)
        return client

    def _order_with_status(self, user, status):
        """A row exactly as the writers would have left it (status plus the
        dimension mapping). The .update() deliberately bypasses auto_now —
        which is the staleness the bulk path's per-row rework fixes."""
        order = Order.objects.create(
            user=user,
            full_name="Audit Buyer",
            phone="1",
            address="a",
            city="c",
            state="s",
            pincode="1",
            total_amount=Decimal("10.00"),
        )
        Order.objects.filter(pk=order.pk).update(
            status=status,
            payment_status=order_state.payment_for_status(status),
            fulfilment_status=order_state.fulfilment_for_status(status),
        )
        order.refresh_from_db()
        return order

    # ——— checkout: the trail opens with the creation event ———

    def test_checkout_appends_the_creation_event(self):
        """[R-10.12] Creation is the lifecycle's first transition (no source
        state → pending), so the trail opens at checkout."""
        order = self.create_order()
        events = list(order.status_events.all())
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIsNone(event.from_status)
        self.assertEqual(event.to_status, "pending")
        self.assertEqual(event.actor, self.buyer)
        self.assertEqual(event.trigger, order_state.TRIGGER_ORDER_CREATE)
        self.assertIsNotNone(event.created_at)

    # ——— verify_payment ———

    def test_verify_payment_appends_the_transition_event(self):
        order = self.create_order()
        self.razorpay_mock(order_id="order_AUDV")
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()  # picks up the minted razorpay_order_id

        res = self.client.post(
            "/api/orders/payment/verify/", self._verify_payload(order), format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        events = list(order.status_events.order_by("created_at", "id"))
        self.assertEqual([e.to_status for e in events], ["pending", "confirmed"])
        transition = events[-1]
        self.assertEqual(transition.from_status, "pending")
        self.assertEqual(transition.to_status, "confirmed")
        # [R-10.17] No admin acts on this path: the trigger records the
        # source and the actor stays NULL — "actor OR triggering event".
        self.assertIsNone(transition.actor)
        self.assertEqual(transition.trigger, order_state.TRIGGER_PAYMENT_VERIFY)

    def test_failed_verify_appends_no_event(self):
        order = self.create_order()
        client_mock = self.razorpay_mock(order_id="order_AUDFAIL")
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()
        self.razorpay_fail_signature(client_mock)

        res = self.client.post(
            "/api/orders/payment/verify/", self._verify_payload(order), format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        # no transition, no event: the trail still holds creation only
        self.assertEqual(
            list(order.status_events.values_list("to_status", flat=True)),
            ["pending"],
        )

    def test_verify_rollback_takes_the_event_with_it(self):
        """[R-10.18] Rollback-together pin: the event rides verify_payment's
        atomic block, so an audit-layer failure after it reverts both."""
        order = self.create_order()
        self.razorpay_mock(order_id="order_AUDRB")
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()
        self.client.raise_request_exception = False
        with mock.patch.object(
            AuditEvent, "record", side_effect=RuntimeError("down")
        ):
            res = self.client.post(
                "/api/orders/payment/verify/",
                self._verify_payload(order),
                format="json",
            )
        self.assertEqual(res.status_code, 500)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.status_events.count(), 1)  # creation only

    # ——— admin change form ———

    def _change_form_post(self, order, status):
        return self.client.post(
            f"/admin/orders/order/{order.id}/change/",
            {
                "user": order.user_id,
                "full_name": order.full_name,
                "phone": order.phone,
                "address": order.address,
                "city": order.city,
                "state": order.state,
                "pincode": order.pincode,
                "status": status,
                "_save": "Save",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
            follow=True,
        )

    def test_admin_change_form_appends_one_event_per_legal_transition(self):
        self._admin_login()
        order = self._order_with_status(self.buyer, "pending")

        res = self._change_form_post(order, "cancelled")
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")

        event = order.status_events.exclude(
            trigger=order_state.TRIGGER_ORDER_CREATE
        ).get()
        self.assertEqual(event.from_status, "pending")
        self.assertEqual(event.to_status, "cancelled")
        self.assertEqual(event.actor, self._admin_user())
        self.assertEqual(event.trigger, order_state.TRIGGER_ADMIN_CHANGE_FORM)

    def test_admin_change_form_skips_noop_saves_and_illegal_moves(self):
        self._admin_login()
        confirmed = self._order_with_status(self.buyer, "confirmed")
        shipped = self._order_with_status(self.buyer, "shipped")

        # no-op: the self-transition is not a transition, so no audit row
        res = self._change_form_post(confirmed, "confirmed")
        self.assertEqual(res.status_code, 200)
        # illegal: the machine gate aborts the save entirely
        res = self._change_form_post(shipped, "cancelled")
        self.assertEqual(res.status_code, 200)

        self.assertEqual(confirmed.status_events.count(), 0)
        self.assertEqual(shipped.status_events.count(), 0)

    def test_admin_change_form_failed_audit_write_reverts_the_transition(self):
        """[R-10.18] Rollback-together pin for the change form: a failing
        event write rolls the whole save back with it."""
        self._admin_login()
        self.client.raise_request_exception = False
        order = self._order_with_status(self.buyer, "pending")
        with mock.patch.object(
            OrderStatusEvent.objects,
            "create",
            side_effect=RuntimeError("audit down"),
        ):
            res = self._change_form_post(order, "confirmed")
        self.assertEqual(res.status_code, 500)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.status_events.count(), 0)

    # ——— admin bulk actions ———

    def test_bulk_action_appends_per_row_events_with_fresh_updated_at(self):
        confirmed = self._order_with_status(self.buyer, "confirmed")
        pending = self._order_with_status(self.buyer, "pending")
        # the pre-fix state: a bulk .update() bypasses auto_now, so
        # updated_at is stranded at its insert-time value — exactly the
        # staleness the per-row rework fixes
        stale_updated_at = confirmed.updated_at

        res = self._run_admin_action("mark_shipped", [confirmed, pending])
        self.assertEqual(res.status_code, 200)
        confirmed.refresh_from_db()
        pending.refresh_from_db()

        self.assertEqual(confirmed.status, "shipped")
        # [staleness pin] per-row saves fire auto_now: updated_at is fresh
        self.assertGreater(confirmed.updated_at, stale_updated_at)

        event = confirmed.status_events.get()
        self.assertEqual(event.from_status, "confirmed")
        self.assertEqual(event.to_status, "shipped")
        self.assertEqual(event.actor, self._admin_user())
        self.assertEqual(event.trigger, order_state.TRIGGER_ADMIN_BULK_ACTION)

        # the out-of-set row is skipped with no event (9-07 semantics)
        self.assertEqual(pending.status, "pending")
        self.assertEqual(pending.status_events.count(), 0)

    def test_bulk_cancel_appends_events(self):
        order = self.create_order()  # pending, unpaid, has creation event

        res = self._run_admin_action("cancel_pending", [order])
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")

        cancel_event = order.status_events.exclude(
            trigger=order_state.TRIGGER_ORDER_CREATE
        ).get()
        self.assertEqual(cancel_event.from_status, "pending")
        self.assertEqual(cancel_event.to_status, "cancelled")
        self.assertEqual(cancel_event.actor, self._admin_user())
        self.assertEqual(cancel_event.trigger, order_state.TRIGGER_ADMIN_BULK_ACTION)

    # ——— the 9-07 admin JSON seam ———

    def test_json_seam_fulfil_appends_events_with_the_staff_actor(self):
        order = self.create_order()
        support = self._staff_client("auditsupp")

        res = support.post(f"/api/admin/orders/{order.id}/fulfill/")
        self.assertEqual(res.status_code, 200, res.data)
        res = support.post(f"/api/admin/orders/{order.id}/fulfill/")
        self.assertEqual(res.status_code, 200, res.data)

        events = order.status_events.filter(
            trigger=order_state.TRIGGER_ADMIN_API_FULFIL
        ).order_by("created_at", "id")
        self.assertEqual(
            [(e.from_status, e.to_status) for e in events],
            [("pending", "confirmed"), ("confirmed", "shipped")],
        )
        for event in events:
            self.assertEqual(event.actor, User.objects.get(username="auditsupp"))

    def test_json_seam_cancel_appends_event_and_replay_appends_nothing(self):
        order = self.create_order()
        support = self._staff_client("auditcancel")

        res = support.post(f"/api/admin/orders/{order.id}/cancel/")
        self.assertEqual(res.status_code, 200, res.data)
        res = support.post(f"/api/admin/orders/{order.id}/cancel/")
        self.assertEqual(res.status_code, 200, res.data)  # idempotent replay

        cancel_events = order.status_events.filter(
            trigger=order_state.TRIGGER_ADMIN_API_CANCEL
        )
        self.assertEqual(cancel_events.count(), 1)
        event = cancel_events.get()
        self.assertEqual(event.from_status, "pending")
        self.assertEqual(event.to_status, "cancelled")
        self.assertEqual(event.actor, User.objects.get(username="auditcancel"))

    def test_cancel_pending_skips_a_row_flipped_after_the_snapshot(self):
        """Deterministic cancel twin of the 9-07 race guard: a row that
        flips pending→confirmed between the pk snapshot and the per-row
        write is a PAID order by then — it must be skipped (cancelling a
        paid order is impossible), with no audit row for a cancel that
        never happened."""
        from django.contrib.admin import site as admin_site
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.db import models as django_models
        from django.test import RequestFactory

        admin = OrderAdmin(Order, admin_site)
        staff = self.make_staff()
        request = RequestFactory().post("/admin/orders/order/")
        request.user = staff
        request.session = {}
        request._messages = FallbackStorage(request)

        victim = Order.objects.create(
            user=self.buyer,
            full_name="V", phone="1", address="a", city="c", state="s",
            pincode="1", total_amount=Decimal("10.00"),
        )
        control = Order.objects.create(
            user=self.buyer,
            full_name="C", phone="1", address="a", city="c", state="s",
            pincode="1", total_amount=Decimal("10.00"),
        )

        class CancellingQuerySet(django_models.QuerySet):
            raced = False

            def values_list(self, *args, **kwargs):
                pks = list(super().values_list(*args, **kwargs))
                if not CancellingQuerySet.raced:
                    CancellingQuerySet.raced = True
                    # The interleave: the snapshot above already saw both
                    # rows pending; the victim gets paid before the write
                    # runs.
                    Order.objects.filter(pk=victim.pk).update(status="confirmed")
                return pks

        admin.cancel_pending(request, CancellingQuerySet(Order))

        victim.refresh_from_db()
        control.refresh_from_db()
        self.assertEqual(victim.status, "confirmed")
        self.assertEqual(control.status, "cancelled")
        self.assertFalse(
            OrderStatusEvent.objects.filter(
                order=victim, to_status="cancelled"
            ).exists()
        )
        self.assertTrue(
            OrderStatusEvent.objects.filter(
                order=control, to_status="cancelled"
            ).exists()
        )

    # ——— immutability ([R-10.18]) ———

    def test_events_are_append_only(self):
        order = self.create_order()
        event = order.status_events.get()
        event.to_status = "confirmed"
        with self.assertRaisesRegex(TypeError, "append-only"):
            event.save()
        event.refresh_from_db()
        self.assertEqual(event.to_status, "pending")

    def test_admin_surface_is_view_only(self):
        from django.contrib.admin import site as admin_site
        from django.test import RequestFactory

        from orders.admin import OrderStatusEventAdmin

        order = self.create_order()
        event = order.status_events.get()
        event_admin = OrderStatusEventAdmin(OrderStatusEvent, admin_site)

        # staff (orders.read via the roles map): view yes, mutate no
        staff = self.make_staff()
        request = RequestFactory().post("/admin/")
        request.user = staff
        self.assertTrue(event_admin.has_view_permission(request))
        self.assertFalse(event_admin.has_add_permission(request))
        self.assertFalse(event_admin.has_change_permission(request, event))
        self.assertFalse(event_admin.has_delete_permission(request, event))

        # even the superuser bypass cannot add or delete: append-only
        self._admin_login()
        request.user = self._admin_user()
        self.assertFalse(event_admin.has_add_permission(request))
        self.assertFalse(event_admin.has_delete_permission(request))

        # every field renders read-only: the change form is a view
        for field in ("order", "from_status", "to_status", "actor", "trigger",
                      "created_at"):
            self.assertIn(field, OrderStatusEventAdmin.readonly_fields)

    def test_event_model_pins(self):
        """Meta pins + the trigger choices stay single-sourced in state.py."""
        self.assertEqual(
            OrderStatusEvent._meta.get_field("trigger").choices,
            order_state.STATUS_EVENT_TRIGGERS,
        )
        self.assertEqual(OrderStatusEvent._meta.ordering, ("-created_at", "-id"))
        self.assertEqual(
            OrderStatusEvent._meta.verbose_name, "order status event"
        )
        order = self.create_order()
        event = order.status_events.get()
        self.assertEqual(
            str(event),
            f"{order.id}: None->pending ({order_state.TRIGGER_ORDER_CREATE})",
        )

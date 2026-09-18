"""Orders unit tests - docs/test-gaps.md items 30-39.

Razorpay is always mocked (``self.razorpay_mock``); no test touches the
network or the real keys from ``.env`` (V-01 containment).
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import tag
from django.utils import timezone
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle

from cart.models import Cart, CartItem
from common.testing import TEST_RAZORPAY_KEY_ID, ApiTestCase
from orders.models import Coupon, Order, OrderItem
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
                # (e.g. minimum_order_amount) may ride along
                self.assertEqual(res.data, {"error": "Invalid coupon code"})

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
            res.data["products"],
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
            res.data["products"],
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
            {row["name"] for row in res.data["products"]},
            {"Rose Aurum", "Oud Royale"},
        )
        self.assertEqual(Order.objects.count(), 0)


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
        ids = [row["id"] for row in res.data]
        self.assertEqual(ids, [mine.id])
        self.assertNotIn(theirs.id, ids)
        # serializer shape: coupon rendered as its code, items embedded
        self.assertEqual(res.data[0]["coupon"], "PCT10")
        self.assertEqual(res.data[0]["items"][0]["price"], "500.00")

    def test_model_string_representations(self):
        order = self.create_order()
        item = order.items.first()
        self.assertEqual(str(order), f"Order #{order.id} - buyer")
        self.assertEqual(str(item), "Rose Aurum x 2")

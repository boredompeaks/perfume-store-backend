"""E2E purchase-flow tests - docs/test-gaps.md e2e items 1, 8, 10, 11."""
import unittest

from django.test import tag

from cart.models import CartItem
from common.testing import ApiTestCase
from orders.models import Order


@tag("e2e")
class FullPurchaseHappyPathTests(ApiTestCase):
    # e2e 1. register -> verify -> login -> product (staff) -> cart -> checkout
    # -> payment (mocked Razorpay) -> verify -> stock/coupon/cart assertions
    def test_full_purchase_happy_path(self):
        # --- registration + email verification through the real flow ------
        self.register_and_verify(username="happybuyer", email="happy@example.com")
        _, token = self.api_login("happybuyer")
        self.assertTrue(token)

        # --- staff creates a product through the API -----------------------
        self.make_staff()
        staff_client = self.fresh_client()
        self.api_login("staff", client=staff_client)
        res = staff_client.post(
            "/api/products/",
            {
                "name": "Happy Path Eau de Parfum",
                "description": "End-to-end fragrance.",
                "price": "500.00",
                "size": 50,
                "stock": 10,
                "category": "Floral",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        product_id = res.data["id"]
        slug = res.data["slug"]

        # --- public listing exposes it --------------------------------------
        res = self.client.get(f"/api/products/{slug}/")
        self.assertEqual(res.status_code, 200, res.data)

        # --- anonymous session cart (same cookie jar as the JWT below) ------
        res = self.client.post("/api/cart/", {"product_id": product_id, "quantity": 2}, format="json")
        self.assertEqual(res.status_code, 201, res.data)

        # --- checkout with a coupon ------------------------------------------
        coupon = self.make_coupon(code="PCT10", discount_value="10")
        res = self.checkout(coupon_code="PCT10")
        self.assertEqual(res.status_code, 201, res.data)
        order_id = res.data["id"]
        self.assertEqual(res.data["total_amount"], "900.00")  # 1000.00 - 10%
        self.assertEqual(res.data["coupon"], "PCT10")
        self.assertEqual(res.data["status"], "pending")

        # --- payment: razorpay order created (mocked) ------------------------
        client_mock = self.razorpay_mock(order_id="order_HAPPY")
        res = self.client.post("/api/orders/payment/", {"order_id": order_id}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["razorpay_order_id"], "order_HAPPY")
        self.assertEqual(res.data["amount"], 90000)  # paise

        # --- payment verification: the whole store state settles -------------
        res = self.client.post(
            "/api/orders/payment/verify/",
            {
                "order_id": order_id,
                "razorpay_order_id": "order_HAPPY",
                "razorpay_payment_id": "pay_HAPPY",
                "razorpay_signature": "sig",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], "confirmed")

        # stock decremented by the ordered quantity
        res = self.client.get(f"/api/products/{slug}/")
        self.assertEqual(res.data["stock"], 8)

        # coupon usage counted exactly once
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)

        # paid items removed from the session cart
        res = self.client.get("/api/cart/")
        self.assertEqual(res.data["items"], [])

        # order visible to its owner with the confirmed status
        res = self.client.get("/api/orders/")
        self.assertEqual([row["id"] for row in res.data], [order_id])
        self.assertEqual(res.data[0]["status"], "confirmed")
        self.assertEqual(res.data[0]["items"][0]["quantity"], 2)


@tag("e2e")
class CartPersistenceTests(ApiTestCase):
    # e2e 8. cart persists across requests with the same session cookie;
    # cleared after the paid verify
    def test_cart_persists_across_requests_and_clears_after_paid_verify(self):
        user = self.make_user("buyer")
        product = self.make_product(price="100.00", stock=5)

        res = self.client.get("/api/cart/")
        cart_id = res.data["id"]
        self.client.post("/api/cart/", {"product_id": product.id, "quantity": 3}, format="json")

        # same session cookie -> same cart, contents intact
        res = self.client.get("/api/cart/")
        self.assertEqual(res.data["id"], cart_id)
        self.assertEqual(res.data["items"][0]["quantity"], 3)

        self.api_login("buyer")
        res = self.checkout()
        order_id = res.data["id"]

        self.razorpay_mock(order_id="order_PERSIST")
        self.client.post("/api/orders/payment/", {"order_id": order_id}, format="json")
        res = self.client.post(
            "/api/orders/payment/verify/",
            {
                "order_id": order_id,
                "razorpay_order_id": "order_PERSIST",
                "razorpay_payment_id": "pay_PERSIST",
                "razorpay_signature": "sig",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        res = self.client.get("/api/cart/")
        self.assertEqual(res.data["id"], cart_id)  # cart itself remains
        self.assertEqual(res.data["items"], [])    # but the paid line is gone
        product.refresh_from_db()
        self.assertEqual(product.stock, 2)


@tag("e2e")
class UnverifiedUserChainTests(ApiTestCase):
    # e2e 10. unverified user: login blocked -> no JWT -> checkout 401
    def test_unverified_user_cannot_login_and_cannot_checkout(self):
        res = self.client.post(
            "/api/accounts/register/",
            {"username": "ghostbuyer", "email": "ghost@example.com", "password": "S3cure-Passphrase!"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

        # login is blocked: no JWT pair is ever issued
        res, token = self.api_login("ghostbuyer")
        self.assertEqual(res.status_code, 401, res.data)
        self.assertIsNone(token)

        # without a JWT the order endpoints refuse the request outright
        res = self.client.post("/api/orders/checkout/", self.checkout_payload(), format="json")
        self.assertEqual(res.status_code, 401, res.data)
        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(Order.objects.count(), 0)


@tag("e2e")
class HybridAuthContractTests(ApiTestCase):
    # e2e 11. anonymous cart + JWT checkout without the session cookie
    def test_jwt_checkout_without_session_cookie_returns_documented_404(self):
        self.make_user("buyer")
        product = self.make_product()

        # the cart is built on one client (its own session cookie jar)
        cart_client = self.fresh_client()
        cart_client.post("/api/cart/", {"product_id": product.id, "quantity": 1}, format="json")

        # a second client holds the JWT but no session cookie -> checkout
        # cannot find the cart (documented 404 until F-18 adds ownership)
        jwt_client = self.fresh_client()
        self.api_login("buyer", client=jwt_client)
        res = jwt_client.post("/api/orders/checkout/", self.checkout_payload(), format="json")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")
        self.assertEqual(Order.objects.count(), 0)

    @unittest.expectedFailure
    def test_f18_jwt_checkout_without_session_should_guide_the_client(self):
        """F-18 flip test: once carts belong to users (or the 404 becomes a
        400 with guidance), this contract replaces the raw 404 above."""
        self.make_user("buyer")
        product = self.make_product()
        cart_client = self.fresh_client()
        cart_client.post("/api/cart/", {"product_id": product.id, "quantity": 1}, format="json")

        jwt_client = self.fresh_client()
        self.api_login("buyer", client=jwt_client)
        res = jwt_client.post("/api/orders/checkout/", self.checkout_payload(), format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("session", str(res.data).lower())

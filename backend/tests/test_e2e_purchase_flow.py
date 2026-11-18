import unittest

from django.test import tag

from common.testing import ApiTestCase
from orders.models import Order


@tag("e2e")
class FullPurchaseHappyPathTests(ApiTestCase):
    def test_full_purchase_happy_path(self):
        self.register_and_verify(username="happybuyer", email="happy@example.com")
        _, token = self.api_login("happybuyer")
        self.assertTrue(token)

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

        res = self.client.get(f"/api/products/{slug}/")
        self.assertEqual(res.status_code, 200, res.data)

        res = self.client.post(
            "/api/cart/", {"product_id": product_id, "quantity": 2}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)

        coupon = self.make_coupon(code="PCT10", discount_value="10")
        res = self.checkout(coupon_code="PCT10")
        self.assertEqual(res.status_code, 201, res.data)
        order_id = res.data["id"]
        self.assertEqual(res.data["total_amount"], "900.00")
        self.assertEqual(res.data["coupon"], "PCT10")
        self.assertEqual(res.data["status"], "pending")

        self.razorpay_mock(order_id="order_HAPPY")
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order_id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["razorpay_order_id"], "order_HAPPY")
        self.assertEqual(res.data["amount"], 90000)

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

        res = self.client.get(f"/api/products/{slug}/")
        self.assertEqual(res.data["stock"], 8)

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)

        res = self.client.get("/api/cart/")
        self.assertEqual(res.data["items"], [])

        res = self.client.get("/api/orders/")
        self.assertEqual([row["id"] for row in res.data["results"]], [order_id])
        self.assertEqual(res.data["results"][0]["status"], "confirmed")
        self.assertEqual(res.data["results"][0]["items"][0]["quantity"], 2)


@tag("e2e")
class CartPersistenceTests(ApiTestCase):
    def test_cart_persists_across_requests_and_clears_after_paid_verify(self):
        self.make_user("buyer")
        self.api_login("buyer")
        product = self.make_product(price="100.00", stock=5)

        res = self.client.get("/api/cart/")
        cart_id = res.data["id"]
        self.client.post(
            "/api/cart/", {"product_id": product.id, "quantity": 3}, format="json"
        )

        res = self.client.get("/api/cart/")
        self.assertEqual(res.data["id"], cart_id)
        self.assertEqual(res.data["items"][0]["quantity"], 3)

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
        self.assertEqual(res.data["id"], cart_id)
        self.assertEqual(res.data["items"], [])
        product.refresh_from_db()
        self.assertEqual(product.stock, 2)


@tag("e2e")
class UnverifiedUserChainTests(ApiTestCase):
    def test_unverified_user_cannot_login_and_cannot_checkout(self):
        res = self.client.post(
            "/api/accounts/register/",
            {
                "username": "ghostbuyer",
                "email": "ghost@example.com",
                "password": "S3cure-Passphrase!",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

        res, token = self.api_login("ghostbuyer")
        self.assertEqual(res.status_code, 401, res.data)
        self.assertIsNone(token)

        res = self.client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(
            res.data["error"],
            "Guest checkout is not available. "
            "Sign in or create an account to place an order.",
        )

        res = self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(guest_email="ghost@example.com"),
            format="json",
        )
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(
            res.data["error"],
            "Guest checkout is not available. "
            "Sign in or create an account to place an order.",
        )

        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(Order.objects.count(), 0)
        self.assertFalse(Order.objects.filter(user__username="ghostbuyer").exists())


@tag("e2e")
class HybridAuthContractTests(ApiTestCase):
    def test_jwt_checkout_without_session_cookie_returns_documented_404(self):
        self.make_user("buyer")
        product = self.make_product()

        cart_client = self.fresh_client()
        cart_client.post(
            "/api/cart/", {"product_id": product.id, "quantity": 1}, format="json"
        )

        jwt_client = self.fresh_client()
        self.api_login("buyer", client=jwt_client)
        res = jwt_client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")
        self.assertEqual(Order.objects.count(), 0)

    @unittest.expectedFailure
    def test_f18_jwt_checkout_without_session_should_guide_the_client(self):
        self.make_user("buyer")
        product = self.make_product()
        cart_client = self.fresh_client()
        cart_client.post(
            "/api/cart/", {"product_id": product.id, "quantity": 1}, format="json"
        )

        jwt_client = self.fresh_client()
        self.api_login("buyer", client=jwt_client)
        res = jwt_client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("session", str(res.data).lower())

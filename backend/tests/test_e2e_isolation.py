from decimal import Decimal

from django.test import tag

from cart.models import CartItem
from common.testing import ApiTestCase
from orders.models import Order
from products.models import products as Product


@tag("e2e")
class MultiUserIsolationTests(ApiTestCase):
    def setUp(self):
        self.a = self.make_user("alice")
        self.b = self.make_user("bob")
        self.client_a = self.fresh_client()
        self.client_b = self.fresh_client()
        self.api_login("alice", client=self.client_a)
        self.api_login("bob", client=self.client_b)

    def test_users_cannot_see_or_mutate_each_others_orders_or_carts(self):
        product = self.make_product(price="100.00", stock=10)

        self.seed_session_cart([(product, 2)], client=self.client_a)
        res = self.client_a.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        alice_order_id = res.data["id"]

        res = self.client_b.get("/api/orders/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["results"], [])

        self.razorpay_mock(order_id="order_SECRET")
        res = self.client_b.post(
            "/api/orders/payment/", {"order_id": alice_order_id}, format="json"
        )
        self.assertEqual(res.status_code, 404, res.data)

        res = self.client_b.post(
            "/api/orders/payment/verify/",
            {
                "order_id": alice_order_id,
                "razorpay_order_id": "order_SECRET",
                "razorpay_payment_id": "pay_SECRET",
                "razorpay_signature": "sig",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404, res.data)

        alice_item = CartItem.objects.get()
        res = self.client_b.patch(
            f"/api/cart/{alice_item.id}/", {"quantity": 1}, format="json"
        )
        self.assertEqual(res.status_code, 404, res.data)
        res = self.client_b.delete(f"/api/cart/{alice_item.id}/")
        self.assertEqual(res.status_code, 404, res.data)

        res = self.client_b.get("/api/cart/")
        self.assertEqual(res.data["items"], [])
        res = self.client_b.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Cart is empty")

        alice_item.refresh_from_db()
        self.assertEqual(alice_item.quantity, 2)
        self.assertEqual(Order.objects.get(id=alice_order_id).user, self.a)
        self.assertEqual(Order.objects.count(), 1)


@tag("e2e")
class ProductLifecycleTests(ApiTestCase):
    def test_order_snapshot_survives_price_change_and_product_delete(self):
        self.make_staff()
        staff_client = self.fresh_client()
        self.api_login("staff", client=staff_client)

        res = staff_client.post(
            "/api/products/",
            {
                "name": "Limited Edition",
                "description": "Small batch.",
                "price": "99.00",
                "size": 30,
                "stock": 5,
                "category": "Limited",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        slug = res.data["slug"]
        product = Product.objects.get(slug=slug)

        self.make_user("collector")
        self.api_login("collector")
        self.seed_session_cart([(product, 2)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order_id = res.data["id"]
        self.assertEqual(res.data["items"][0]["price"], "99.00")

        res = staff_client.patch(
            f"/api/products/{slug}/", {"price": "149.00"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["price"], "149.00")

        res = self.client.get("/api/orders/")
        self.assertEqual(res.data["results"][0]["items"][0]["price"], "99.00")

        res = staff_client.delete(f"/api/products/{slug}/")
        self.assertEqual(res.status_code, 204)
        res = self.client.get(f"/api/products/{slug}/")
        self.assertEqual(res.status_code, 404)

        res = self.client.get("/api/orders/")
        row = res.data["results"][0]["items"][0]
        self.assertEqual(row["product_name"], "Limited Edition")
        self.assertEqual(row["price"], "99.00")
        self.assertEqual(row["quantity"], 2)
        self.assertIsNone(row["product"])
        order = Order.objects.get(id=order_id)
        self.assertEqual(order.total_amount, Decimal("198.00"))

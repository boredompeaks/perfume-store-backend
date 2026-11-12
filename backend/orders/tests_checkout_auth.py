from decimal import Decimal

from django.test import tag

from cart.models import Cart
from common.testing import ApiTestCase
from orders.models import Order
from shipping.models import ShippingMethod, ShippingRate


@tag("orders")
class AuthenticatedCheckoutE2ETests(ApiTestCase):
    def setUp(self):
        self.product = self.make_product(
            name="Rose Aurum", price="500.00", stock=10, category="Floral"
        )
        self.buyer = self.make_user("buyer")
        self.standard = ShippingMethod.objects.create(code="standard", name="Standard")
        # A national rate, so "shipping_amount 0.00" can only be the coupon's waiver,
        # not an unconfigured store.
        ShippingRate.objects.create(method=self.standard, amount=Decimal("49.00"))
        self.freeship = self.make_coupon(
            code="FREESHIP", discount_type="free_shipping", discount_value="0"
        )

    def _login_and_fill_cart(self, quantity=1):
        _, token = self.api_login("buyer")
        self.assertTrue(token)
        res = self.client.get("/api/cart/")
        self.assertEqual(res.status_code, 200, res.data)
        user_cart_id = res.data["id"]
        res = self.client.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": quantity},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        return user_cart_id

    def test_freeship_checkout_uses_the_user_cart_and_charges_no_shipping(self):
        user_cart_id = self._login_and_fill_cart(quantity=1)

        res = self.client.post(
            "/api/cart/coupon/", {"code": self.freeship.code}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)

        res = self.checkout(shipping_method="standard")

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertEqual(order.user, self.buyer)
        self.assertEqual(order.shipping_amount, Decimal("0.00"))
        self.assertEqual(res.data["shipping_amount"], "0.00")
        self.assertEqual(order.discount_amount, Decimal("0.00"))
        self.assertEqual(order.total_amount, Decimal("500.00"))
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.shipping_method, self.standard)
        item = order.items.get()
        self.assertEqual(item.product, self.product)
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.subtotal, Decimal("500.00"))

        cart = Cart.objects.get(pk=user_cart_id)
        self.assertEqual(cart.user, self.buyer)
        self.assertIsNone(cart.session_id)
        self.assertEqual(cart.order, order)
        self.assertEqual(order.cart, cart)
        self.assertEqual(Cart.objects.filter(user=self.buyer).count(), 1)
        self.assertFalse(Cart.objects.filter(session_id__isnull=False).exists())

    def test_the_same_account_cart_is_charged_shipping_without_the_coupon(self):
        self._login_and_fill_cart(quantity=1)

        res = self.checkout(shipping_method="standard")

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertEqual(order.shipping_amount, Decimal("49.00"))
        self.assertEqual(order.shipping_method, self.standard)
        self.assertEqual(order.total_amount, Decimal("549.00"))

    def test_checkout_reuses_the_cart_the_cart_endpoints_built(self):
        user_cart_id = self._login_and_fill_cart(quantity=2)

        res = self.checkout(shipping_method="standard")

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        cart = Cart.objects.get(id=user_cart_id)
        self.assertEqual(cart.order, order)
        self.assertEqual(order.items.get().quantity, 2)
        self.assertEqual(order.shipping_amount, Decimal("49.00"))
        self.assertEqual(order.total_amount, Decimal("1049.00"))

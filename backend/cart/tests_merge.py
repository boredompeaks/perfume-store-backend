"""D-04b: the guest basket follows the account it signs into."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import tag
from django.utils import timezone

from cart.models import Cart, CartItem
from common.testing import ApiTestCase
from orders.models import Order

PASSWORD = "S3cure-Passphrase!"


@tag("cart")
class CartMergeOnLoginTests(ApiTestCase):
    def setUp(self):
        self.product = self.make_product(name="Rose Aurum", price="500.00", stock=10)
        self.buyer = self.make_user("buyer", password=PASSWORD)

    def _guest_cart(self, quantity=2, client=None):
        """A guest basket built through the public cart API."""
        client = client or self.client
        res = client.get("/api/cart/")
        self.assertEqual(res.status_code, 200, res.data)
        cart_id = res.data["id"]
        res = client.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": quantity},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        return cart_id

    def _storefront_login(self, username="buyer", password=PASSWORD):
        res = self.client.post(
            "/api/accounts/storefront/login/",
            {"username": username, "password": password},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.auth(res.data["access"])
        return res

    def test_guest_basket_is_adopted_by_the_account_on_login(self):
        source_id = self._guest_cart(quantity=2)
        source_item = CartItem.objects.get(cart_id=source_id)

        res = self._storefront_login()

        cart = Cart.objects.get(pk=source_id)
        self.assertEqual(cart.user, self.buyer)
        self.assertIsNone(cart.session_id)
        self.assertEqual(res.data["cart_id"], cart.id)
        # ADOPT keeps the rows: same cart, same line, still quantity 2.
        self.assertEqual(CartItem.objects.get(cart=cart).pk, source_item.pk)
        self.assertEqual(cart.items.get().quantity, 2)
        self.assertFalse(Cart.objects.filter(user__isnull=True).exists())

    def test_existing_account_cart_sums_quantities_without_duplicate_lines(self):
        _, token = self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])  # account cart
        self.client.credentials()
        source_id = self._guest_cart(quantity=2)

        self._storefront_login()

        cart = Cart.objects.get(user=self.buyer)
        self.assertEqual(cart.items.count(), 1)
        self.assertEqual(cart.items.get().quantity, 3)
        self.assertFalse(Cart.objects.filter(pk=source_id).exists())

    def test_merge_adds_products_only_the_guest_cart_holds(self):
        other = self.make_product(name="Oud Royale", price="250.00", stock=4)
        _, token = self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])
        self.client.credentials()
        self.client.get("/api/cart/")  # guest cart
        res = self.client.post(
            "/api/cart/", {"product_id": other.id, "quantity": 3}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)

        self._storefront_login()

        cart = Cart.objects.get(user=self.buyer)
        self.assertEqual(cart.items.count(), 2)
        self.assertEqual(cart.items.get(product=other).quantity, 3)
        self.assertEqual(cart.items.get(product=self.product).quantity, 1)

    def test_merge_caps_quantity_at_stock(self):
        self.product.stock = 3
        self.product.save(update_fields=["stock"])
        _, token = self.api_login("buyer")
        self.seed_session_cart([(self.product, 2)])
        self.client.credentials()
        self._guest_cart(quantity=2)

        self._storefront_login()

        cart = Cart.objects.get(user=self.buyer)
        self.assertEqual(cart.items.get().quantity, 3)  # capped, not 4

    def test_account_coupon_wins_over_the_guest_coupon(self):
        account_coupon = self.make_coupon(code="ACCOUNT5", discount_value="5")
        guest_coupon = self.make_coupon(code="GUEST5", discount_value="5")
        _, token = self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])
        res = self.client.post(
            "/api/cart/coupon/", {"code": account_coupon.code}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.client.credentials()
        self._guest_cart(quantity=1)
        res = self.client.post(
            "/api/cart/coupon/", {"code": guest_coupon.code}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)

        self._storefront_login()

        cart = Cart.objects.get(user=self.buyer)
        self.assertEqual(cart.coupon, account_coupon)

    def test_guest_coupon_is_adopted_when_the_account_cart_has_none(self):
        guest_coupon = self.make_coupon(code="GUEST5", discount_value="5")
        _, token = self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])
        self.client.credentials()
        self._guest_cart(quantity=1)
        res = self.client.post(
            "/api/cart/coupon/", {"code": guest_coupon.code}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)

        self._storefront_login()

        cart = Cart.objects.get(user=self.buyer)
        self.assertEqual(cart.coupon, guest_coupon)

    def test_merge_drops_lines_whose_stock_cap_is_zero(self):
        """Stock can reach zero between the two baskets: the summed line is
        capped at it, and a cap of zero must remove the line rather than
        store an unpurchasable one."""
        sold_out = self.make_product(name="Sold Out", price="100.00", stock=0)
        _, token = self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])  # account line, cap 0 later
        self.product.stock = 0
        self.product.save(update_fields=["stock"])
        self.client.credentials()
        res = self.client.get("/api/cart/")
        self.assertEqual(res.status_code, 200, res.data)
        guest_cart = res.data["id"]
        # The add endpoint refuses quantity > stock, so both zero-stock
        # lines are placed directly to model stock dropping after the add.
        CartItem.objects.create(cart_id=guest_cart, product=self.product, quantity=2)
        CartItem.objects.create(cart_id=guest_cart, product=sold_out, quantity=1)

        self._storefront_login()

        cart = Cart.objects.get(user=self.buyer)
        self.assertEqual(cart.items.count(), 0)

    def test_a_login_with_nothing_to_merge_mints_no_cart(self):
        res = self._storefront_login()

        self.assertNotIn("cart_id", res.data)
        self.assertFalse(Cart.objects.filter(user=self.buyer).exists())

    def test_registration_carries_the_guest_basket_into_the_new_account(self):
        source_id = self._guest_cart(quantity=2)

        res = self.client.post(
            "/api/accounts/register/",
            {
                "username": "newbuyer",
                "email": "newbuyer@example.com",
                "password": PASSWORD,
            },
            format="json",
        )

        self.assertEqual(res.status_code, 201, res.data)
        cart = Cart.objects.get(user__username="newbuyer")
        self.assertEqual(cart.id, source_id)  # adopted, rows kept
        self.assertEqual(cart.items.get().quantity, 2)
        self.assertEqual(res.data["cart_id"], cart.id)
        self.assertFalse(Cart.objects.filter(user__isnull=True).exists())


@tag("cart")
class PruneCartsCommandTests(ApiTestCase):
    def setUp(self):
        self.product = self.make_product()
        self.user = self.make_user("buyer")

    def _cart(self, session_id, days_old=None, user=None, order=None):
        cart = Cart.objects.create(session_id=session_id, user=user, order=order)
        if days_old is not None:
            Cart.objects.filter(pk=cart.pk).update(
                updated_at=timezone.now() - timedelta(days=days_old)
            )
        return cart

    def _prune(self, *args):
        out = StringIO()
        call_command("prune_carts", *args, stdout=out)
        return out.getvalue()

    def _order(self):
        return Order.objects.create(
            user=self.user,
            full_name="Buyer Person",
            phone="9876543210",
            address="12 Rose Lane",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal("500.00"),
        )

    def test_only_old_empty_orderless_anonymous_carts_are_deleted(self):
        stale_empty = self._cart("stale-empty", days_old=40)
        stale_with_item = self._cart("stale-item", days_old=40)
        CartItem.objects.create(cart=stale_with_item, product=self.product, quantity=1)
        stale_with_order = self._cart("stale-order", days_old=40, order=self._order())
        stale_user_cart = self._cart("stale-user", days_old=40, user=self.user)
        recent_empty = self._cart("recent-empty")

        output = self._prune("--days", "30")

        self.assertIn("Deleted 1", output)
        self.assertFalse(Cart.objects.filter(pk=stale_empty.pk).exists())
        self.assertTrue(Cart.objects.filter(pk=stale_with_item.pk).exists())
        self.assertTrue(Cart.objects.filter(pk=stale_with_order.pk).exists())
        self.assertTrue(Cart.objects.filter(pk=stale_user_cart.pk).exists())
        self.assertTrue(Cart.objects.filter(pk=recent_empty.pk).exists())

    def test_dry_run_reports_and_deletes_nothing(self):
        stale = self._cart("stale-empty", days_old=40)

        output = self._prune("--days", "30", "--dry-run")

        self.assertIn("Dry run: 1", output)
        self.assertIn("would be deleted", output)
        self.assertTrue(Cart.objects.filter(pk=stale.pk).exists())

    def test_a_negative_age_is_refused(self):
        with self.assertRaises(CommandError):
            self._prune("--days", "-1")

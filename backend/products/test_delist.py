"""MERCHANT-DELIST: a merchant can take a product off sale without destroying it.

The slice has four load-bearing properties, each pinned here:

- ``is_active`` defaults to on-sale, so the migration that adds it cannot
  silently empty a live catalogue;
- the public catalogue stops serving delisted rows -- excluded from the
  listing, and the detail GET answers the byte-identical 404 an unknown
  slug gets, so the endpoint is not an oracle for "existed but delisted";
- the buying path refuses delisted products -- the cart add answers the
  same 404 as an unknown product id, and a checkout holding a delisted
  line is refused with a 400 that NAMES the line (the line survives:
  removal is the customer's call, and a relist makes it buyable again);
- delisting is a reversible state flip, never delete: neither the stock
  column nor the StockMovement ledger moves, and the admin actions ride
  the ``products.publish`` capability through the existing RBAC gate.
"""

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Group, User
from django.test import tag

from cart.models import Cart
from common.roles import ROLE_CATALOGUE, ROLE_INVENTORY
from common.testing import ApiTestCase
from orders.models import Order, OrderItem
from products.admin import ProductAdmin
from products.models import StockMovement, StockReservation, products

ADMIN_PASSWORD = "S3cure-Passphrase!"


def role_staff(role, username):
    """A staff user holding exactly one RBAC role (test_admin_rbac shape)."""
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=ADMIN_PASSWORD,
        is_staff=True,
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def make_delisted_product(**overrides):
    overrides.setdefault("name", "Recalled Amber")
    overrides["is_active"] = False
    return products.objects.create(
        description="A fragrance with amber and vanilla notes.",
        price="499.99",
        size=50,
        stock=10,
        category="Amber",
        **overrides,
    )


@tag("products")
class DelistSchemaTests(ApiTestCase):
    """The column exists, defaults to on-sale, and leaves existing rows so."""

    def test_field_default_is_pinned_true(self):
        # Hand-written literal, never derived from the fixture: a default of
        # False would silently delist every row a migration touches.
        self.assertIs(products._meta.get_field("is_active").default, True)

    def test_new_products_are_on_sale_by_default(self):
        product = self.make_product()
        self.assertIs(product.is_active, True)


@tag("products")
class DelistPublicSurfaceTests(ApiTestCase):
    """The public catalogue listing and detail must not serve delisted rows."""

    def setUp(self):
        self.active = self.make_product(name="Live Rose", price="100.00")
        self.delisted = make_delisted_product()

    def test_listing_excludes_delisted_products(self):
        res = self.client.get("/api/products/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 1)
        self.assertEqual(
            [row["slug"] for row in res.data["results"]], [self.active.slug]
        )

    def test_listing_search_does_not_resurface_delisted(self):
        # The exclusion is the base queryset, so no downstream filter
        # (search/category/price) can reintroduce a delisted row.
        res = self.client.get("/api/products/", {"search": self.delisted.name})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 0)

    def test_detail_returns_404_for_delisted_product(self):
        res = self.client.get(f"/api/products/{self.delisted.slug}/")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Product not found")

    def test_delisted_detail_404_is_identical_to_unknown_slug(self):
        # Byte-identical bodies: a distinct answer for "delisted" would
        # confirm the product exists, which is the leak this 404 avoids.
        delisted = self.client.get(f"/api/products/{self.delisted.slug}/")
        unknown = self.client.get("/api/products/no-such-slug/")
        self.assertEqual(delisted.status_code, 404)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(delisted.data, unknown.data)

    def test_active_product_detail_still_served(self):
        res = self.client.get(f"/api/products/{self.active.slug}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["slug"], self.active.slug)

    def test_cart_add_of_delisted_product_is_refused_as_not_found(self):
        # The buying surface treats a delisted product exactly like an
        # unknown one, so the customer's cart is untouched.
        self.seed_session_cart([(self.active, 1)])
        res = self.client.post(
            "/api/cart/",
            {"product_id": self.delisted.id, "quantity": 1},
            format="json",
        )
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Product not found")
        cart = Cart.objects.get(session_id=self.client.session.session_key)
        self.assertEqual(cart.items.count(), 1)  # only the active line

    def test_cart_add_refusal_is_identical_to_unknown_product(self):
        self.client.get("/api/cart/")  # session bootstrap
        delisted = self.client.post(
            "/api/cart/", {"product_id": self.delisted.id}, format="json"
        )
        unknown = self.client.post("/api/cart/", {"product_id": 999999}, format="json")
        self.assertEqual(delisted.status_code, 404)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(delisted.data, unknown.data)

    def test_cart_add_of_active_product_still_succeeds(self):
        res = self.client.post(
            "/api/cart/",
            {"product_id": self.active.id, "quantity": 1},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)


@tag("products")
class DelistCheckoutRefusalTests(ApiTestCase):
    """A delisted product already in the cart blocks checkout, named.

    The refusal is the whole-cart 400, never a silent skip: the customer
    is told which line is not buyable and removes it themselves, and the
    line itself survives (a later relist makes it buyable again without
    the customer having to rebuild the cart).
    """

    def setUp(self):
        self.buyer = self.make_user("buyer")
        _, self.token = self.api_login("buyer")
        self.product = self.make_product(name="Rose Aurum", price="500.00", stock=10)
        self.seed_session_cart([(self.product, 2)])

    def _delist(self, product):
        products.objects.filter(pk=product.pk).update(is_active=False)

    def test_checkout_with_delisted_line_is_refused_naming_it(self):
        self._delist(self.product)
        res = self.checkout()
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(
            res.data["error"],
            'No longer available for purchase: "Rose Aurum". '
            "Remove the item from the cart to continue.",
        )
        self.assertEqual(
            res.data["details"]["products"],
            [{"id": self.product.id, "name": "Rose Aurum"}],
        )

    def test_refusal_creates_nothing_and_decrements_nothing(self):
        self._delist(self.product)
        self.checkout()
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(StockReservation.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_cart_line_survives_the_refusal(self):
        self._delist(self.product)
        self.checkout()
        cart = Cart.objects.get(session_id=self.client.session.session_key)
        line = cart.items.get(product=self.product)
        self.assertEqual(line.quantity, 2)

    def test_checkout_succeeds_again_after_relist(self):
        # Delist is reversible end to end: the same cart, the same line,
        # checks out once the product is back on sale.
        self._delist(self.product)
        refused = self.checkout()
        self.assertEqual(refused.status_code, 400, refused.data)
        products.objects.filter(pk=self.product.pk).update(is_active=True)
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)

    def test_mixed_cart_refusal_names_only_delisted_lines(self):
        other = self.make_product(name="Live Oud", price="300.00", stock=5)
        self.client.post(
            "/api/cart/", {"product_id": other.id, "quantity": 1}, format="json"
        )
        self._delist(self.product)
        res = self.checkout()
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('"Rose Aurum"', res.data["error"])
        self.assertNotIn("Live Oud", res.data["error"])
        self.assertEqual(
            res.data["details"]["products"],
            [{"id": self.product.id, "name": "Rose Aurum"}],
        )

    def test_checkout_refusal_names_every_delisted_line_in_plural_form(self):
        # Two delisted lines: the refusal names BOTH (the customer must be
        # able to find every line to remove) and switches to the plural
        # instruction.
        second = self.make_product(name="Plain Oud", price="300.00", stock=5)
        self.client.post(
            "/api/cart/", {"product_id": second.id, "quantity": 1}, format="json"
        )
        self._delist(self.product)
        self._delist(second)
        res = self.checkout()
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(
            res.data["error"],
            'No longer available for purchase: "Rose Aurum", "Plain Oud". '
            "Remove these items from the cart to continue.",
        )
        self.assertEqual(
            res.data["details"]["products"],
            [
                {"id": self.product.id, "name": "Rose Aurum"},
                {"id": second.id, "name": "Plain Oud"},
            ],
        )


@tag("products")
class DelistAdminActionTests(ApiTestCase):
    """The delist/relist bulk actions flip the state and nothing else."""

    def setUp(self):
        User.objects.create_superuser("padmin", "padmin@example.com", ADMIN_PASSWORD)
        self.assertTrue(self.client.login(username="padmin", password=ADMIN_PASSWORD))
        self.product = self.make_product(name="Plain Oud", stock=3)

    def _run_action(self, action, product):
        return self.client.post(
            "/admin/products/products/",
            {
                "action": action,
                "_selected_action": [str(product.id)],
                "select_across": "0",
            },
            follow=True,
        )

    def test_delist_action_takes_product_off_sale(self):
        res = self._run_action("delist_products", self.product)
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertIs(self.product.is_active, False)

    def test_relist_action_restores_it(self):
        self._run_action("delist_products", self.product)
        res = self._run_action("relist_products", self.product)
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertIs(self.product.is_active, True)

    def test_delist_neither_moves_stock_nor_writes_a_movement(self):
        # The point of the field: delist is not delete. The stock column
        # and the StockMovement ledger ([6.5.17]: inventory changes only)
        # must both be untouched by a state flip.
        self._run_action("delist_products", self.product)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)
        self._run_action("relist_products", self.product)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_delist_action_is_logged_in_the_audit_trail(self):
        # [6.12.6]: a privileged action without its LogEntry is a spec
        # violation -- the bulk action bypasses save_model, so the base's
        # log_bulk_action is the writer and this is its pin.
        self._run_action("delist_products", self.product)
        entry = LogEntry.objects.get(object_id=str(self.product.pk), action_flag=CHANGE)
        self.assertIn("off sale", entry.change_message)

    def test_delist_of_already_delisted_is_a_no_op(self):
        self._run_action("delist_products", self.product)
        self._run_action("delist_products", self.product)
        self.product.refresh_from_db()
        self.assertIs(self.product.is_active, False)
        # The no-op run logged nothing new: one entry, from the first run.
        self.assertEqual(
            LogEntry.objects.filter(
                object_id=str(self.product.pk), action_flag=CHANGE
            ).count(),
            1,
        )

    def test_changelist_lists_delisted_products_with_state_column(self):
        # The staff surface is the ONE place a delisted row is visible
        # (the public catalogue 404s it) -- without it there would be no
        # way to relist.
        self._run_action("delist_products", self.product)
        res = self.client.get("/admin/products/products/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Plain Oud")
        self.assertContains(res, "On sale")


@tag("products")
class DelistAdminCapabilityGateTests(ApiTestCase):
    """The actions ride ``products.publish`` through the RBAC gate."""

    def test_publish_capability_gates_both_actions(self):
        # Hand-written literal: the unpublish power is exactly what the
        # ``products.publish`` capability exists for (the admin's delete
        # already rides it for the same reason), and NOT ``products.write``
        # -- a wider set of surfaces edits the row without deciding
        # whether the store sells it.
        self.assertEqual(
            ProductAdmin.action_capabilities["delist_products"], "products.publish"
        )
        self.assertEqual(
            ProductAdmin.action_capabilities["relist_products"], "products.publish"
        )

    def test_catalogue_role_can_delist_via_direct_post(self):
        # The capability holder path, through the real admin POST: the
        # non-superuser RBAC route works end to end.
        role_staff(ROLE_CATALOGUE, "cat-delister")
        self.assertTrue(
            self.client.login(username="cat-delister", password=ADMIN_PASSWORD)
        )
        product = self.make_product(name="Gate Rose")
        res = self._post_action("delist_products", product)
        self.assertEqual(res.status_code, 200)
        product.refresh_from_db()
        self.assertIs(product.is_active, False)

    def test_non_holder_cannot_delist_via_direct_post(self):
        # Inventory staff reach the changelist (products.read) but hold no
        # products.publish: the action is filtered out of their choices, so
        # a hand-POSTed action name never runs and the state is unchanged.
        role_staff(ROLE_INVENTORY, "inv-delister")
        self.assertTrue(
            self.client.login(username="inv-delister", password=ADMIN_PASSWORD)
        )
        product = self.make_product(name="Gate Oud")
        res = self._post_action("delist_products", product)
        self.assertEqual(res.status_code, 200)
        product.refresh_from_db()
        self.assertIs(product.is_active, True)

    def test_non_holder_cannot_relist_via_direct_post(self):
        role_staff(ROLE_INVENTORY, "inv-relister")
        self.assertTrue(
            self.client.login(username="inv-relister", password=ADMIN_PASSWORD)
        )
        product = make_delisted_product(name="Gate Musk")
        res = self._post_action("relist_products", product)
        self.assertEqual(res.status_code, 200)
        product.refresh_from_db()
        self.assertIs(product.is_active, False)

    def _post_action(self, action, product):
        return self.client.post(
            "/admin/products/products/",
            {
                "action": action,
                "_selected_action": [str(product.id)],
                "select_across": "0",
            },
            follow=True,
        )

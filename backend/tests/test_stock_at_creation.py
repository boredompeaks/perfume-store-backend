"""SPEC-20-12 [R-20.36]: an opening stock set at creation PERSISTS.

Both entry surfaces accept a stock value when a product is created, and both
must store it. Creation is deliberately exempt from the SPEC-6-02 [6.5.17]
ledger rule - an opening balance is not an edit, so no StockMovement row is
expected - which makes the persisted number itself the whole contract. It is
a regression pin, not a fix: the "stock silently resets to 0 on create" shape
is not reproducible on a clean tree (the serializer's read-only guard is
correctly scoped to `if self.instance is not None`, and the admin forces
`stock` read-only only once the row exists), so the value of these tests is
that a future widening of either guard cannot quietly drop the field without
one of them going red.

Why the pins exist: the create paths were only ever asserted through the
response status and the audit log (e.g. tests/test_audit_log.py posts
`stock: 3` and checks 201 + LogEntry), so a persisted zero would have passed
every one of them.
"""

from django.contrib.auth.models import Group, User
from django.test import tag

from common.roles import ROLE_CATALOGUE
from common.testing import ApiTestCase
from products.admin import ProductAdmin
from products.models import StockMovement, products

TEST_PASSWORD = "S3cure-Passphrase!"


def make_role_user(role, username):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


@tag("e2e")
class ApiStockAtCreationTests(ApiTestCase):
    """POST a product with a non-zero stock: the row keeps it."""

    def setUp(self):
        self.scribe = make_role_user(ROLE_CATALOGUE, "opening-scribe")
        self.client = self.fresh_client()
        self.api_login("opening-scribe", client=self.client)

    def _payload(self, name, stock):
        return {
            "name": name,
            "description": "Created with an opening balance.",
            "price": "10.00",
            "size": 30,
            "stock": stock,
            "category": "Floral",
        }

    def test_api_create_persists_the_opening_stock(self):
        res = self.client.post(
            "/api/products/", self._payload("Opening Rose", 7), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["stock"], 7)
        created = products.objects.get(name="Opening Rose")
        self.assertEqual(created.stock, 7)  # the assertion the old pins missed
        self.assertEqual(StockMovement.objects.count(), 0)  # opening, not an edit

    def test_api_create_persists_zero_and_large_counts(self):
        """The two edges of the column's domain: the default 0 and a count
        wider than a single digit (a wrong-type int would truncate one)."""
        for stock in (0, 1234):
            with self.subTest(stock=stock):
                name = f"Edge Opening {stock}"
                res = self.client.post(
                    "/api/products/", self._payload(name, stock), format="json"
                )
                self.assertEqual(res.status_code, 201, res.data)
                self.assertEqual(res.data["stock"], stock)
                self.assertEqual(products.objects.get(name=name).stock, stock)

    def test_api_create_without_a_stock_field_keeps_the_model_default(self):
        """The field is writable, not mandatory: omitting it must land the
        column default, not an error and not a leftover from a prior row."""
        payload = self._payload("No Stock Given", 5)
        payload.pop("stock")
        res = self.client.post("/api/products/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["stock"], 0)
        self.assertEqual(products.objects.get(name="No Stock Given").stock, 0)


@tag("e2e")
class AdminStockAtCreationTests(ApiTestCase):
    """The admin add form takes an opening stock; the change form does not."""

    def setUp(self):
        # catalogue holds products.write, so it reaches the add form without
        # the superuser bypass.
        self.scribe = make_role_user(ROLE_CATALOGUE, "opening-catalogue")
        self.client.force_login(self.scribe)

    def _payload(self, name, stock):
        return {
            "name": name,
            "category": "Floral",
            "description": "Added through the admin with an opening balance.",
            "price": "10.00",
            "size": "30",
            "stock": str(stock),
            # the StockMovementInline is part of the add form's formset
            "stock_movements-TOTAL_FORMS": "0",
            "stock_movements-INITIAL_FORMS": "0",
            "stock_movements-MIN_NUM_FORMS": "0",
            "stock_movements-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }

    def test_add_form_renders_the_stock_input(self):
        """The guard is scoped to the change form, so the add form still
        offers the field (creating is what sets the opening balance)."""
        res = self.client.get("/admin/products/products/add/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="stock"')

    def test_admin_add_persists_the_opening_stock(self):
        res = self.client.post(
            "/admin/products/products/add/", self._payload("Admin Opening", 17)
        )
        self.assertEqual(res.status_code, 302)
        created = products.objects.get(name="Admin Opening")
        self.assertEqual(created.stock, 17)
        self.assertEqual(StockMovement.objects.count(), 0)  # opening, not an edit

    def test_add_form_forced_readonly_would_drop_the_value(self):
        """Direct unit pin on the gate that decides it: `stock` is readonly
        only once the row exists, never on the add form."""
        product_admin = ProductAdmin(products, None)
        request = self.client.get("/admin/products/products/add/").wsgi_request
        self.assertNotIn("stock", product_admin.get_readonly_fields(request, None))
        self.assertIn(
            "stock",
            product_admin.get_readonly_fields(request, self.make_product()),
        )

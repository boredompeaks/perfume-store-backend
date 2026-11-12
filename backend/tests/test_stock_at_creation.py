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
        self.assertEqual(created.stock, 7)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_api_create_persists_zero_and_large_counts(self):
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
        payload = self._payload("No Stock Given", 5)
        payload.pop("stock")
        res = self.client.post("/api/products/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["stock"], 0)
        self.assertEqual(products.objects.get(name="No Stock Given").stock, 0)


@tag("e2e")
class AdminStockAtCreationTests(ApiTestCase):
    def setUp(self):
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
            "stock_movements-TOTAL_FORMS": "0",
            "stock_movements-INITIAL_FORMS": "0",
            "stock_movements-MIN_NUM_FORMS": "0",
            "stock_movements-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }

    def test_add_form_renders_the_stock_input(self):
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
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_add_form_forced_readonly_would_drop_the_value(self):
        product_admin = ProductAdmin(products, None)
        request = self.client.get("/admin/products/products/add/").wsgi_request
        self.assertNotIn("stock", product_admin.get_readonly_fields(request, None))
        self.assertIn(
            "stock",
            product_admin.get_readonly_fields(request, self.make_product()),
        )

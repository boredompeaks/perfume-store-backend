"""Products unit tests - docs/test-gaps.md items 15-22, plus the manual
stock-adjustment feature (``adjust_stock`` / ``StockMovement``) and its
admin surface."""
import base64
import unittest
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import tag

from common.testing import ApiTestCase
from products.models import StockMovement, products

# 1x1 transparent PNG so ImageField can hold a real thumbnail
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@tag("products")
class ProductListFilterTests(ApiTestCase):
    def setUp(self):
        self.staff = self.make_staff()
        self.p_rose = self.make_product(name="Rose Water", category="Floral", price="100.00")
        self.p_oud = self.make_product(
            name="Oud Royale", category="Oriental", price="200.00",
            description="Smoky oud with saffron.",
        )
        # description matches "rose" even though the name does not
        self.p_jasmine = self.make_product(
            name="Jasmine Mist", category="Floral", price="300.00",
            description="A rose and jasmine blend.",
        )

    def _list_all(self, params=None):
        """Walk every page (page size is hardcoded 2, F-23) and return the
        combined results list."""
        results, page = [], 1
        while True:
            res = self.client.get("/api/products/", {**(params or {}), "page": page})
            self.assertEqual(res.status_code, 200, res.data)
            results.extend(res.data["results"])
            if not res.data["next_page"]:
                return results
            page += 1

    # 15. search across name/description/category ---------------------------------------
    def test_search_across_name_description_and_category(self):
        cases = {
            "Rose Water": {self.p_rose.id},          # name match
            "jasmine blend": {self.p_jasmine.id},    # description match
            "oriental": {self.p_oud.id},             # category match
            "ROSE": {self.p_rose.id, self.p_jasmine.id},  # icase across fields
        }
        for term, expected in cases.items():
            with self.subTest(term=term):
                self.assertEqual({row["id"] for row in self._list_all({"search": term})}, expected)

    def test_search_without_match_returns_empty_page(self):
        res = self.client.get("/api/products/", {"search": "zzz-nothing-like-this"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 0)
        self.assertEqual(res.data["results"], [])

    # 16. category filter (iexact) + min/max price incl. invalid decimal -> 400 -----------
    def test_category_filter_is_case_insensitive(self):
        res = self.client.get("/api/products/", {"category": "FLORAL"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual({row["id"] for row in res.data["results"]}, {self.p_rose.id, self.p_jasmine.id})

    def test_price_filters_combine_with_category(self):
        res = self.client.get("/api/products/", {"category": "floral", "min_price": "150", "max_price": "250"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 0)  # florals cost 100 and 300

        res = self.client.get("/api/products/", {"min_price": "150"})
        self.assertEqual(res.data["count"], 2)
        res = self.client.get("/api/products/", {"max_price": "250"})
        self.assertEqual(res.data["count"], 2)
        res = self.client.get("/api/products/", {"min_price": "150", "max_price": "250"})
        self.assertEqual({row["id"] for row in res.data["results"]}, {self.p_oud.id})

    def test_invalid_price_filter_returns_400(self):
        for params in ({"min_price": "abc"}, {"max_price": "1.2.3"}, {"min_price": "abc", "max_price": "5"}):
            with self.subTest(params=params):
                res = self.client.get("/api/products/", params)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data["error"], "Price filters must be valid numbers")

    # 17. ordering whitelist (invalid value ignored, not 500) ------------------------------
    def test_ordering_by_price_and_name(self):
        res = self.client.get("/api/products/", {"ordering": "-price", "page": 1})
        res2 = self.client.get("/api/products/", {"ordering": "-price", "page": 2})
        self.assertEqual(
            [row["id"] for row in res.data["results"] + res2.data["results"]],
            [self.p_jasmine.id, self.p_oud.id, self.p_rose.id],
        )

        res = self.client.get("/api/products/", {"ordering": "price"})
        res2 = self.client.get("/api/products/", {"ordering": "price", "page": 2})
        self.assertEqual(
            [row["id"] for row in res.data["results"] + res2.data["results"]],
            [self.p_rose.id, self.p_oud.id, self.p_jasmine.id],
        )

        rows = self._list_all({"ordering": "name"})
        self.assertEqual([row["name"] for row in rows], sorted(row["name"] for row in rows))

    def test_invalid_ordering_values_ignored_not_500(self):
        """Whitelist only: unknown, related-field and injection-ish values are
        ignored, never raise (no 500, no field leak)."""
        for ordering in ("user", "password", "nonexistent", "price,name", "-; DROP", "stock"):
            with self.subTest(ordering=ordering):
                res = self.client.get("/api/products/", {"ordering": ordering})
                self.assertEqual(res.status_code, 200, res.data)
                self.assertEqual(res.data["count"], 3)

    # 18. pagination envelope shape ---------------------------------------------------------
    def test_pagination_envelope_shape(self):
        res = self.client.get("/api/products/")
        self.assertEqual(res.status_code, 200, res.data)
        for key in ("count", "total_pages", "current_page", "next_page", "previous_page", "results"):
            self.assertIn(key, res.data)
        self.assertEqual(res.data["count"], 3)
        self.assertEqual(res.data["total_pages"], 2)  # page size is hardcoded 2 (F-23)
        self.assertEqual(res.data["current_page"], 1)
        self.assertTrue(res.data["next_page"])
        self.assertFalse(res.data["previous_page"])
        self.assertEqual(len(res.data["results"]), 2)

        res = self.client.get("/api/products/", {"page": 2})
        self.assertEqual(res.data["current_page"], 2)
        self.assertFalse(res.data["next_page"])
        self.assertTrue(res.data["previous_page"])
        self.assertEqual(len(res.data["results"]), 1)

    def test_out_of_range_and_non_integer_pages_clamped(self):
        res = self.client.get("/api/products/", {"page": 999})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["current_page"], 2)

        res = self.client.get("/api/products/", {"page": "abc"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["current_page"], 1)

    @unittest.expectedFailure
    def test_f12_default_listing_order_is_deterministic(self):
        """F-12/V-20: without an explicit ``ordering`` the queryset is
        unordered, so pages are not guaranteed stable. Asserts the fixed
        behaviour (Meta.ordering = ['-created_at', 'id']); remove
        @expectedFailure when Phase 3.4 lands."""
        from django.utils import timezone

        fixed = timezone.make_aware(timezone.datetime(2026, 1, 1, 12, 0, 0))
        products.objects.update(created_at=fixed)  # identical timestamps -> id must break ties
        res = self.client.get("/api/products/")
        self.assertEqual(
            [row["id"] for row in res.data["results"]],
            sorted([row["id"] for row in res.data["results"]], reverse=True),
        )


@tag("products")
class SlugGenerationTests(ApiTestCase):
    # 19. slug auto-generation + collision suffix --------------------------------------------
    def test_slug_autogeneration_and_collision_suffix(self):
        first = self.make_product(name="Rose Water")
        self.assertEqual(first.slug, "rose-water")

        second = self.make_product(name="Rose Water")
        self.assertEqual(second.slug, "rose-water-2")

        third = self.make_product(name="rose   WATER")
        self.assertEqual(third.slug, "rose-water-3")

        self.make_product(name="Oud Royale")
        self.assertEqual(self.make_product(name="Oud Royale").slug, "oud-royale-2")

    def test_symbol_only_name_falls_back_to_product_slug(self):
        product = self.make_product(name="!!! ???")
        self.assertEqual(product.slug, "product")
        # even the fallback collides safely
        self.assertEqual(self.make_product(name="###").slug, "product-2")

    def test_long_name_collision_truncated_to_slug_max_length(self):
        """The collision path truncates the base slug so base + suffix fits."""
        self.make_product(name="x" * 150)
        second = self.make_product(name="x" * 150)
        self.assertEqual(second.slug, "x" * 95 + "-2")
        self.assertLessEqual(len(second.slug), 100)

    @unittest.expectedFailure
    def test_slug_overflow_without_collision_currently_exceeds_max_length(self):
        """Latent bug (V-15 neighbour): the FIRST product with a 150-char name
        keeps a 150-char slug because truncation only happens when a suffix is
        appended. SQLite tolerates it; a varchar(100) DB would reject the
        INSERT. Flip when slug generation truncates unconditionally."""
        product = self.make_product(name="x" * 150)
        self.assertLessEqual(len(product.slug), 100)

    def test_explicit_slug_preserved_on_resave(self):
        product = self.make_product(name="Rose Water", slug="custom-slug")
        product.stock = 5
        product.save()
        product.refresh_from_db()
        self.assertEqual(product.slug, "custom-slug")
        # renaming must not silently rewrite the slug (stable URLs)
        product.name = "Renamed Product"
        product.save()
        product.refresh_from_db()
        self.assertEqual(product.slug, "custom-slug")


@tag("products")
class ProductWritePermissionTests(ApiTestCase):
    """20. create/update/delete require staff - 403 for anon + normal user."""

    def setUp(self):
        self.staff = self.make_staff()
        self.user = self.make_user("customer")
        self.product = self.make_product(name="Rose Water")

    def _forbidden_for_anon_and_customer(self, method, payload=None):
        for client_name, client in (("anon", self.fresh_client()), ("customer", self.fresh_client())):
            if client_name == "customer":
                self.api_login("customer", client=client)
            res = getattr(client, method.lower())(f"/api/products/{self.product.slug}/", payload, format="json")
            self.assertEqual(res.status_code, 403, (client_name, res.status_code))
            self.assertEqual(res.data["detail"], "Administrator access is required.")
        self.product.refresh_from_db()

    def test_create_requires_staff(self):
        payload = {
            "name": "New Perfume", "description": "desc", "price": "10.00",
            "size": 30, "stock": 1, "category": "Floral",
        }
        res = self.fresh_client().post("/api/products/", payload, format="json")
        self.assertEqual(res.status_code, 403)

        client = self.fresh_client()
        self.api_login("customer", client=client)
        res = client.post("/api/products/", payload, format="json")
        self.assertEqual(res.status_code, 403)

        client = self.fresh_client()
        self.api_login("staff", client=client)
        res = client.post("/api/products/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["slug"], "new-perfume")

    def test_put_requires_staff(self):
        payload = {
            "name": "Rose Water", "description": "updated", "price": "111.00",
            "size": 50, "stock": 9, "category": "Floral",
        }
        self._forbidden_for_anon_and_customer("PUT", payload)
        client = self.fresh_client()
        self.api_login("staff", client=client)
        res = client.put(f"/api/products/{self.product.slug}/", payload, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["price"], "111.00")

    def test_patch_requires_staff(self):
        self._forbidden_for_anon_and_customer("PATCH", {"price": "111.00"})
        client = self.fresh_client()
        self.api_login("staff", client=client)
        res = client.patch(f"/api/products/{self.product.slug}/", {"price": "111.00"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("111.00"))

    def test_delete_requires_staff(self):
        self._forbidden_for_anon_and_customer("DELETE")
        client = self.fresh_client()
        self.api_login("staff", client=client)
        res = client.delete(f"/api/products/{self.product.slug}/")
        self.assertEqual(res.status_code, 204)
        res = client.get(f"/api/products/{self.product.slug}/")
        self.assertEqual(res.status_code, 404)

    def test_staff_invalid_payload_rejected_400(self):
        client = self.fresh_client()
        self.api_login("staff", client=client)
        res = client.post("/api/products/", {"name": ""}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("name", res.data)
        res = client.post("/api/products/", {"name": "X", "price": "not-a-number"}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("price", res.data)

    def test_staff_put_and_patch_invalid_payload_rejected_400(self):
        client = self.fresh_client()
        self.api_login("staff", client=client)
        res = client.put(f"/api/products/{self.product.slug}/", {"name": ""}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("name", res.data)
        res = client.patch(f"/api/products/{self.product.slug}/", {"price": "nope"}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("price", res.data)


@tag("products")
class ProductDetailTests(ApiTestCase):
    # 21. detail 404 for unknown slug -------------------------------------------------------
    def test_unknown_slug_returns_404(self):
        res = self.client.get("/api/products/does-not-exist/")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Product not found")

    def test_detail_returns_public_representation(self):
        product = self.make_product(name="Rose Water", price="499.99")
        res = self.client.get(f"/api/products/{product.slug}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["name"], "Rose Water")
        self.assertEqual(res.data["price"], "499.99")
        self.assertEqual(res.data["slug"], "rose-water")

    # 22. serializer exposes the full model field set (intentional canary) -------------------
    def test_serializer_field_set_is_pinned(self):
        """Docs/test-gaps #22: this pins the CURRENT serializer shape. It will
        break on purpose when `fields = '__all__'` is replaced by an explicit
        whitelist (V-19), forcing a conscious review of the public surface."""
        product = self.make_product()
        res = self.client.get(f"/api/products/{product.slug}/")
        self.assertEqual(
            set(res.data.keys()),
            {"id", "name", "slug", "description", "price", "size", "stock", "category", "image", "created_at"},
        )


# =====================================================================================
# Manual stock adjustment feature (adjust_stock / StockMovement ledger)
# =====================================================================================

@tag("products")
class AdjustStockTests(ApiTestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            "invadmin", "inv@example.com", "inv-pass-123"
        )
        self.product = products.objects.create(
            name="Test oudh",
            description="test",
            price="1000.00",
            size=50,
            stock=10,
            category="test",
        )

    def test_adjust_stock_adds_and_writes_movement(self):
        self.product.adjust_stock(
            self.admin, 15, StockMovement.Reason.RESTOCK, "monthly restock"
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 25)
        movement = StockMovement.objects.get(product=self.product)
        self.assertEqual(movement.delta, 15)
        self.assertEqual(movement.stock_after, 25)
        self.assertEqual(movement.created_by, self.admin)

    def test_adjust_stock_rejects_negative_result(self):
        with self.assertRaises(ValueError):
            self.product.adjust_stock(self.admin, -11, StockMovement.Reason.DAMAGE)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)  # unchanged
        self.assertEqual(StockMovement.objects.count(), 0)  # nothing logged

    def test_negative_delta_allowed_within_stock(self):
        self.product.adjust_stock(self.admin, -4, StockMovement.Reason.CORRECTION)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 6)

    def test_adjustment_by_anonymous_actor_records_no_user(self):
        self.product.adjust_stock(None, 5, StockMovement.Reason.OTHER, "seed data")
        movement = StockMovement.objects.get()
        self.assertIsNone(movement.created_by)
        self.assertEqual(movement.stock_after, 15)

    def test_movement_string_representation(self):
        self.product.adjust_stock(self.admin, 3, StockMovement.Reason.RETURNED)
        movement = StockMovement.objects.get()
        self.assertEqual(str(movement), "Test oudh: +3 (returned)")

    def test_stock_health_property_branches(self):
        self.assertEqual(self.product.stock_health, "ok")  # 10
        self.product.adjust_stock(self.admin, -5, StockMovement.Reason.CORRECTION)
        self.assertEqual(self.product.stock_health, "low")  # 5
        self.product.adjust_stock(self.admin, -5, StockMovement.Reason.DAMAGE)
        self.assertEqual(self.product.stock_health, "out")  # 0


@tag("products")
class ProductAdminTests(ApiTestCase):
    """The customised product admin renders and its actions work."""

    def setUp(self):
        User.objects.create_superuser("padmin", "padmin@example.com", "S3cure-Passphrase!")
        self.assertTrue(self.client.login(username="padmin", password="S3cure-Passphrase!"))
        self.with_image = products.objects.create(
            name="Imaged Rose", description="d", price="50.00", size=30,
            stock=0, category="Floral",
            image=SimpleUploadedFile("rose.png", TINY_PNG, content_type="image/png"),
        )
        self.plain = products.objects.create(
            name="Plain Oud", description="d", price="60.00", size=30,
            stock=3, category="Oriental",
        )

    def _run_action(self, action, product, extra=None):
        payload = {
            "action": action,
            "_selected_action": [str(product.id)],
            "select_across": "0",
        }
        payload.update(extra or {})
        return self.client.post("/admin/products/products/", payload, follow=True)

    def test_changelist_renders_thumb_and_stock_flag_branches(self):
        res = self.client.get("/admin/products/products/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.with_image.name)
        self.assertContains(res, "out of stock")   # stock 0 -> red flag branch
        self.assertContains(res, "low (3)")        # stock 3 -> amber branch
        self.assertContains(res, "rose_")          # thumbnail from the ImageField

    def test_change_page_renders_with_movement_inline_and_preview(self):
        self.with_image.adjust_stock(None, 7, StockMovement.Reason.RESTOCK, "initial fill")

        # with an image: preview renders the stored file
        res = self.client.get(f"/admin/products/products/{self.with_image.id}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Inventory history (manual adjustments)")
        self.assertContains(res, "rose_")
        self.assertContains(res, "Imaged Rose: +7 (restock)")  # movement __str__ in the inline

        # without an image: the empty-preview branch
        res = self.client.get(f"/admin/products/products/{self.plain.id}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "No image uploaded.")

    def test_adjust_stock_action_full_flow(self):
        # step 1: choosing the action renders the intermediate form
        res = self._run_action("adjust_stock", self.plain)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Adjusting stock for the following 1 product(s)")
        self.assertContains(res, "Plain Oud")

        # step 2: applying the form writes the movement
        res = self._run_action(
            "adjust_stock",
            self.plain,
            {"apply": "1", "delta": "20", "reason": "restock", "note": "supplier delivery"},
        )
        self.assertEqual(res.status_code, 200)
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.stock, 23)
        movement = StockMovement.objects.get(product=self.plain)
        self.assertEqual(movement.delta, 20)
        self.assertEqual(movement.reason, "restock")
        self.assertEqual(movement.stock_after, 23)

    def test_adjust_stock_action_rejects_negative_result_with_message(self):
        res = self._run_action(
            "adjust_stock",
            self.plain,
            {"apply": "1", "delta": "-50", "reason": "damage", "note": ""},
        )
        self.assertEqual(res.status_code, 200)
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.stock, 3)  # untouched
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_adjust_stock_action_rejects_invalid_form(self):
        res = self._run_action(
            "adjust_stock",
            self.plain,
            {"apply": "1", "delta": "not-a-number", "reason": "restock"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Change in stock")  # form re-rendered with errors
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.stock, 3)

    def test_export_csv_streams_selected_products(self):
        res = self._run_action("export_csv", self.plain)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "text/csv")
        body = res.content.decode()
        self.assertIn("id,name,slug,category,price,size,stock,created_at", body)
        self.assertIn("Plain Oud", body)

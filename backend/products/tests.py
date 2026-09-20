"""Products unit tests - docs/test-gaps.md items 15-22, plus the manual
stock-adjustment feature (``adjust_stock`` / ``StockMovement``) and its
admin surface."""
import base64
import math
import os
import re
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.core.files.uploadedfile import SimpleUploadedFile
from config.settings import _env_int
from django.db import (
    IntegrityError,
    connection,
    models,
    transaction,
)
from django.db.models import Sum
from django.test import override_settings, tag
from django.utils import timezone
from orders.models import Order
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from common.permissions import IsAdminUserOrReadOnly
from common.roles import (
    ROLE_ADMIN,
    ROLE_CATALOGUE,
    ROLE_INVENTORY,
    ROLE_SUPPORT,
    STAFF_ROLES,
    sync_role_groups,
)
from common.testing import ApiTestCase
from products.admin import ProductAdmin
from products.models import StockMovement, StockReservation, products
from products.serializers import ProductSerializer

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
        """Walk every page and return the combined results list."""
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
    # Page 2 is requested explicitly below, so the size-2 override keeps the
    # walk genuinely multi-page instead of clamping back onto page 1.
    @override_settings(PRODUCTS_PAGE_SIZE=2)
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
    # The size-2 override pins the multi-page envelope behaviour explicitly;
    # the production default is the env-driven PRODUCTS_PAGE_SIZE (F-23).
    @override_settings(PRODUCTS_PAGE_SIZE=2)
    def test_pagination_envelope_shape(self):
        res = self.client.get("/api/products/")
        self.assertEqual(res.status_code, 200, res.data)
        for key in ("count", "total_pages", "current_page", "next_page", "previous_page", "results"):
            self.assertIn(key, res.data)
        self.assertEqual(res.data["count"], 3)
        self.assertEqual(res.data["total_pages"], 2)
        self.assertEqual(res.data["current_page"], 1)
        self.assertTrue(res.data["next_page"])
        self.assertFalse(res.data["previous_page"])
        self.assertEqual(len(res.data["results"]), 2)

        res = self.client.get("/api/products/", {"page": 2})
        self.assertEqual(res.data["current_page"], 2)
        self.assertFalse(res.data["next_page"])
        self.assertTrue(res.data["previous_page"])
        self.assertEqual(len(res.data["results"]), 1)

    def test_page_size_is_env_driven(self):
        """F-23: the page size comes from the env-driven
        ``settings.PRODUCTS_PAGE_SIZE`` (documented in .env.example), not a
        hardcoded literal — the envelope math always follows the active size
        and a deployment tunes it without a code change."""
        res = self.client.get("/api/products/")
        self.assertEqual(res.data["count"], 3)
        expected_pages = math.ceil(3 / settings.PRODUCTS_PAGE_SIZE)
        self.assertEqual(res.data["total_pages"], expected_pages)

        with override_settings(PRODUCTS_PAGE_SIZE=1):
            res = self.client.get("/api/products/")
            self.assertEqual(res.data["total_pages"], 3)
            self.assertEqual(len(res.data["results"]), 1)

    @override_settings(PRODUCTS_PAGE_SIZE=2)
    def test_out_of_range_and_non_integer_pages_clamped(self):
        res = self.client.get("/api/products/", {"page": 999})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["current_page"], 2)

        res = self.client.get("/api/products/", {"page": "abc"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["current_page"], 1)

    def test_f12_default_listing_order_is_deterministic(self):
        """F-12/V-20 regression pin: without an explicit ``ordering`` the
        listing still has a deterministic default order (newest first,
        ``-created_at``, with the unique ``-id`` as the total tiebreaker),
        so pages are stable across identical requests. The pin's mechanism
        is the view-level default order_by (the model keeps no Meta.ordering
        by design); identical timestamps force the id tiebreaker to decide
        the sequence."""
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
            self.assertEqual(res.data["error"], "Administrator access is required.")
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
        self.assertIn("name", res.data["details"])
        res = client.post("/api/products/", {"name": "X", "price": "not-a-number"}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("price", res.data["details"])

    def test_staff_put_and_patch_invalid_payload_rejected_400(self):
        client = self.fresh_client()
        self.api_login("staff", client=client)
        res = client.put(f"/api/products/{self.product.slug}/", {"name": ""}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("name", res.data["details"])
        res = client.patch(f"/api/products/{self.product.slug}/", {"price": "nope"}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("price", res.data["details"])


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

    def test_adjust_stock_re_reads_the_locked_row(self):
        """SPEC-6-02: adjust_stock runs under atomic + select_for_update, so
        it must gate on the row's current value, not the instance's stale
        snapshot — the observable consequence of serialising against the
        payment flow's locked decrements."""
        # a concurrent writer changed the row behind self's back
        products.objects.filter(pk=self.product.pk).update(stock=50)
        self.product.adjust_stock(self.admin, -5, StockMovement.Reason.CORRECTION)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 45)  # 50 - 5, not 10 - 5
        movement = StockMovement.objects.get()
        self.assertEqual(movement.stock_after, 45)

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
class InventoryAdjustApiTests(ApiTestCase):
    """SPEC-9-06 [R-9.4.7] (spec line 3280, `POST /admin/inventory/adjustments`
    under the Inventory module): a thin REST endpoint that reuses the shipped
    ``adjust_stock`` service and the ``HasInventoryAdjust`` permission. The
    ledger assertions mirror AdjustStockTests — the view adds HTTP, nothing
    else."""

    URL = "/api/products/inventory/adjustments/"

    def setUp(self):
        self.product = products.objects.create(
            name="Test oudh",
            description="test",
            price="1000.00",
            size=50,
            stock=10,
            category="test",
        )
        self.invmgr = self._user_with_role("invmgr", ROLE_INVENTORY)

    @staticmethod
    def _user_with_role(username, role):
        from django.contrib.auth.models import Group

        user = User.objects.create_user(
            username, f"{username}@example.com", "S3cure-Passphrase!"
        )
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    def _post(self, payload, client=None):
        client = client or self.client
        return client.post(self.URL, payload, format="json")

    def _payload(self, **overrides):
        payload = {
            "product_id": self.product.id,
            "delta": 5,
            "reason": StockMovement.Reason.RESTOCK,
            "note": "monthly restock",
        }
        payload.update(overrides)
        return payload

    # permission matrix -------------------------------------------------------
    def test_anonymous_caller_is_denied_403(self):
        res = self._post(self._payload(), client=self.fresh_client())
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_catalogue_role_lacks_inventory_adjust_403(self):
        """Pin the least-privilege split: catalogue may write products but
        NOT adjust inventory (roles.py: inventory.adjust = inventory+admin)."""
        catmgr = self._user_with_role("catmgr", ROLE_CATALOGUE)
        self.client.force_authenticate(catmgr)
        res = self._post(self._payload())
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(
            res.data["error"], "You do not have permission to perform this action."
        )
        self.assertEqual(StockMovement.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)  # untouched

    def test_inventory_role_is_allowed(self):
        self.client.force_authenticate(self.invmgr)
        res = self._post(self._payload())
        self.assertEqual(res.status_code, 201, res.data)

    # happy path + ledger contract ---------------------------------------------
    def test_successful_adjustment_writes_the_service_ledger_row(self):
        self.client.force_authenticate(self.invmgr)
        res = self._post(self._payload())

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["delta"], 5)
        self.assertEqual(res.data["stock_after"], 15)
        self.assertEqual(res.data["reason"], "restock")
        self.assertEqual(res.data["note"], "monthly restock")

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 15)
        movement = StockMovement.objects.get(product=self.product)
        self.assertEqual(movement.delta, 5)
        self.assertEqual(movement.stock_after, 15)
        self.assertEqual(movement.reason, StockMovement.Reason.RESTOCK)
        self.assertEqual(movement.note, "monthly restock")
        self.assertEqual(movement.created_by, self.invmgr)

    def test_negative_delta_within_stock_corrects_down(self):
        self.client.force_authenticate(self.invmgr)
        res = self._post(
            self._payload(delta=-4, reason=StockMovement.Reason.CORRECTION, note="")
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 6)
        movement = StockMovement.objects.get()
        self.assertEqual(movement.stock_after, 6)

    def test_below_zero_adjustment_rejected_without_ledger_row(self):
        """The service's ValueError contract surfaces as a 400 and rolls the
        whole mutation back: stock unchanged, no movement row."""
        self.client.force_authenticate(self.invmgr)
        res = self._post(self._payload(delta=-11, reason=StockMovement.Reason.DAMAGE))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("below zero", res.data["error"])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(StockMovement.objects.count(), 0)

    # request validation ---------------------------------------------------
    def test_unknown_product_is_404(self):
        self.client.force_authenticate(self.invmgr)
        res = self._post(self._payload(product_id=999999))
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Product not found")

    def test_missing_fields_are_400(self):
        self.client.force_authenticate(self.invmgr)
        for field in ("product_id", "delta", "reason"):
            with self.subTest(missing=field):
                payload = self._payload()
                del payload[field]
                res = self._post(payload)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data["error"], f"{field} is required")
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_non_integer_delta_is_400(self):
        self.client.force_authenticate(self.invmgr)
        for bad in ("abc", 1.5, True):
            with self.subTest(delta=bad):
                res = self._post(self._payload(delta=bad))
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data["error"], "delta must be an integer")
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_invalid_reason_is_400(self):
        self.client.force_authenticate(self.invmgr)
        res = self._post(self._payload(reason="smuggled"))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("reason", res.data["error"])
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_endpoint_served_on_the_v1_mirror(self):
        self.client.force_authenticate(self.invmgr)
        res = self.client.post(
            "/api/v1/store/products/inventory/adjustments/",
            self._payload(),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)


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

    def test_changelist_stock_column_is_display_only(self):
        """SPEC-6-02 [6.5.17]: a changelist inline stock edit writes no
        movement row, so stock left list_editable — the adjust-stock action
        is the only sanctioned mutation path. The column stays visible and
        price stays inline-editable."""
        self.assertNotIn("stock", ProductAdmin.list_editable)
        res = self.client.get("/admin/products/products/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="form-0-price"')     # price still editable
        self.assertNotContains(res, 'name="form-0-stock"')  # no inline stock input
        self.assertContains(res, 'class="field-stock"')  # stock column still displayed

    def test_changelist_bulk_save_ignores_tampered_stock(self):
        """A hand-crafted changelist POST must not move stock: the formset
        only accepts list_editable fields, so stock can never be changed —
        and certainly never without a movement row — from the changelist."""
        res = self.client.get("/admin/products/products/")
        management = dict(
            re.findall(r'name="(form-[A-Z_]+)" value="([^"]*)"', res.content.decode())
        )

        res = self.client.post(
            "/admin/products/products/",
            {
                **management,
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-0-id": str(self.plain.id),
                "form-0-price": "77.00",
                "form-0-stock": "999",  # the silent edit the old UI allowed
                "_save": "Save",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.price, Decimal("77.00"))  # sanctioned edit applied
        self.assertEqual(self.plain.stock, 3)                 # stock untouched
        self.assertEqual(StockMovement.objects.count(), 0)    # no mutation, no movement

    def test_change_page_stock_is_display_only_add_page_keeps_input(self):
        """SPEC-6-02 (audit cycle-2 probe): the change page's `stock` input
        was a live ledger-free mutation path — a change-form POST silently
        wrote stock. It is read-only once the row exists, while the add page
        keeps the input because creation sets the opening balance, not an
        edit."""
        res = self.client.get(f"/admin/products/products/{self.plain.id}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, 'name="stock"')
        res = self.client.get("/admin/products/products/add/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="stock"')

    def test_change_form_save_ignores_tampered_stock(self):
        """A hand-crafted change-form POST must not move stock: sanctioned
        edits (price) apply, stock is untouched and no movement row lands —
        empirically reproduced pre-fix (POST stock=999 -> 200, stock 3->999,
        zero rows)."""
        res = self.client.post(
            f"/admin/products/products/{self.plain.id}/change/",
            {
                "name": "Plain Oud",
                "category": "Oriental",
                "description": "d",
                "price": "88.00",
                "size": "30",
                "stock": "999",  # the silent edit the old form allowed
                "stock_movements-TOTAL_FORMS": "0",
                "stock_movements-INITIAL_FORMS": "0",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.price, Decimal("88.00"))  # sanctioned edit applied
        self.assertEqual(self.plain.stock, 3)                 # stock untouched
        self.assertEqual(StockMovement.objects.count(), 0)    # no mutation, no movement

    def test_change_page_renders_with_movement_inline_and_preview(self):
        self.with_image.adjust_stock(None, 7, StockMovement.Reason.RESTOCK, "initial fill")

        # with an image: preview renders the stored file
        res = self.client.get(f"/admin/products/products/{self.with_image.id}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Inventory history (all mutations)")
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


# =====================================================================================
# SPEC-1-01: staff writes via permission_classes + explicit serializer fields
# =====================================================================================

EXPECTED_PRODUCT_FIELDS = [
    "id", "name", "slug", "description", "price", "size", "stock",
    "category", "image", "created_at",
]


@tag("products")
class IsAdminUserOrReadOnlyUnitTests(ApiTestCase):
    """Unit contract for common.permissions.IsAdminUserOrReadOnly."""

    def setUp(self):
        self.permission = IsAdminUserOrReadOnly()
        self.staff = self.make_staff()
        self.customer = self.make_user("customer")

    def _request(self, method, user):
        request = Request(APIRequestFactory().generic(method, "/api/products/"))
        request.user = user
        return request

    def test_message_keeps_the_legacy_403_body(self):
        self.assertEqual(self.permission.message, "Administrator access is required.")

    def test_safe_methods_allowed_without_credentials(self):
        for method in ("GET", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                request = self._request(method, AnonymousUser())
                self.assertTrue(self.permission.has_permission(request, None))

    def test_write_methods_require_staff(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                for user in (AnonymousUser(), self.customer):
                    request = self._request(method, user)
                    with self.assertRaises(PermissionDenied) as ctx:
                        self.permission.has_permission(request, None)
                    self.assertEqual(
                        str(ctx.exception), "Administrator access is required."
                    )
                staff_request = self._request(method, self.staff)
                self.assertTrue(
                    self.permission.has_permission(staff_request, None)
                )


@tag("products")
class ProductPermissionClassesApiTests(ApiTestCase):
    """SPEC-1-01 wiring: reads stay public, writes stay staff-only, and the
    403 contract is unchanged now that the gate lives in permission_classes."""

    def setUp(self):
        self.staff = self.make_staff()
        self.product = self.make_product(name="Rose Water")

    def test_read_methods_stay_public_for_anonymous(self):
        client = self.fresh_client()
        self.assertEqual(client.get("/api/products/").status_code, 200)
        self.assertEqual(
            client.get(f"/api/products/{self.product.slug}/").status_code, 200
        )
        # OPTIONS is a SAFE_METHOD too; HEAD is not routed by the FBV
        # (405 before and after this refactor), so it is not asserted here.
        self.assertEqual(
            client.options(f"/api/products/{self.product.slug}/").status_code, 200
        )

    def test_anonymous_write_on_unknown_slug_is_403_not_404(self):
        """Permission checks run before the view body: anonymous writes get a
        uniform 403 even for slugs that do not exist (no existence leak)."""
        res = self.fresh_client().put(
            "/api/products/no-such-slug/", {"price": "1.00"}, format="json"
        )
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["error"], "Administrator access is required.")

    def test_staff_write_still_succeeds_end_to_end(self):
        """SPEC-1-01 wiring: a staff PATCH succeeds end-to-end. The payload
        uses a sanctioned field: since SPEC-6-02 `stock` is read-only on
        updates (a REST stock edit would bypass the StockMovement ledger —
        see ProductStockEditLedgerGuardTests for that contract)."""
        client = self.fresh_client()
        self.api_login("staff", client=client)
        res = client.patch(
            f"/api/products/{self.product.slug}/",
            {"description": "Staff-edited description."},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.description, "Staff-edited description.")


@tag("products")
class ProductCapabilityGateRoleTests(ApiTestCase):
    """SPEC-6-03c: view-level per-role allow/deny for the capability swap.

    Reads stay public and the 403 body stays the legacy one; write authority
    now flows from ``products.write`` (catalogue + admin roles), so a staff
    account without a granting role is denied — a deliberate tightening
    under the no-guard-downgrade rule, never a loosening.
    """

    def setUp(self):
        self.product = self.make_product(name="Gate Rose")
        groups = sync_role_groups()
        # One is_staff user per role, holding exactly that role's group.
        self.role_users = {}
        for role in STAFF_ROLES:
            user = User.objects.create_user(
                username=f"gate-{role}",
                email=f"gate-{role}@example.com",
                password="S3cure-Passphrase!",
                is_staff=True,
            )
            user.groups.add(groups[role])
            self.role_users[role] = user
        # The legacy gate granted every is_staff account write access; the
        # capability gate must deny a role-less staff account (tightening).
        self.roleless_staff = User.objects.create_user(
            username="gate-roleless",
            email="gate-roleless@example.com",
            password="S3cure-Passphrase!",
            is_staff=True,
        )
        self.customer = self.make_user("gate-customer")

    def _writer_client(self, user):
        client = self.fresh_client()
        self.api_login(user.username, client=client)
        return client

    def _write_payload(self, name):
        return {
            "name": name,
            "description": "Created through the gated API.",
            "price": "10.00",
            "size": 30,
            "stock": 3,
            "category": "Floral",
        }

    def test_anonymous_reads_public_writes_legacy_403(self):
        client = self.fresh_client()
        self.assertEqual(client.get("/api/products/").status_code, 200)
        self.assertEqual(
            client.get(f"/api/products/{self.product.slug}/").status_code, 200
        )
        res = client.post(
            "/api/products/", self._write_payload("Sneak"), format="json"
        )
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["error"], "Administrator access is required.")
        self.assertFalse(products.objects.filter(name="Sneak").exists())

    def test_customer_write_is_denied(self):
        client = self._writer_client(self.customer)
        res = client.patch(
            f"/api/products/{self.product.slug}/", {"price": "2.00"}, format="json"
        )
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["error"], "Administrator access is required.")

    def test_roleless_staff_write_is_denied(self):
        """The no-downgrade rule permits only tightenings: the blanket
        is_staff write grant is replaced by the roles map, so a staff
        account with no role loses product write access."""
        client = self._writer_client(self.roleless_staff)
        res = client.post(
            "/api/products/", self._write_payload("Blocked"), format="json"
        )
        self.assertEqual(res.status_code, 403, res.data)
        self.assertFalse(products.objects.filter(name="Blocked").exists())

    def test_writes_follow_the_role_map(self):
        for role, user in self.role_users.items():
            with self.subTest(role=role):
                client = self._writer_client(user)
                res = client.post(
                    "/api/products/",
                    self._write_payload("Role Gate Rose"),
                    format="json",
                )
                if role in (ROLE_CATALOGUE, ROLE_ADMIN):
                    self.assertEqual(res.status_code, 201, res.data)
                    self.assertTrue(
                        products.objects.filter(name="Role Gate Rose").exists()
                    )
                else:
                    self.assertEqual(res.status_code, 403, res.data)
                products.objects.filter(name="Role Gate Rose").delete()

    def test_catalogue_role_full_write_lifecycle_on_detail(self):
        client = self._writer_client(self.role_users[ROLE_CATALOGUE])
        res = client.patch(
            f"/api/products/{self.product.slug}/",
            {"description": "Catalogue-edited."},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.description, "Catalogue-edited.")
        res = client.delete(f"/api/products/{self.product.slug}/")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(products.objects.filter(pk=self.product.pk).exists())

    def test_non_granting_role_cannot_edit_or_delete(self):
        client = self._writer_client(self.role_users[ROLE_SUPPORT])
        res = client.patch(
            f"/api/products/{self.product.slug}/", {"price": "2.00"}, format="json"
        )
        self.assertEqual(res.status_code, 403, res.data)
        res = client.delete(f"/api/products/{self.product.slug}/")
        self.assertEqual(res.status_code, 403, res.data)
        self.assertTrue(products.objects.filter(pk=self.product.pk).exists())


@tag("products")
class ProductSerializerFieldsTests(ApiTestCase):
    """SPEC-1-01: '__all__' replaced by an explicit, drift-guarded whitelist."""

    def test_fields_are_declared_explicitly(self):
        fields = ProductSerializer.Meta.fields
        self.assertIsInstance(fields, (list, tuple))
        self.assertEqual(list(fields), EXPECTED_PRODUCT_FIELDS)

    def test_whitelist_still_covers_every_concrete_model_field(self):
        """The explicit list must equal what '__all__' exposed — a new model
        column must force a conscious decision here, never auto-leak."""
        concrete = {field.name for field in products._meta.concrete_fields}
        self.assertEqual(set(ProductSerializer.Meta.fields), concrete)

    def test_stock_writable_on_create_read_only_on_update(self):
        """SPEC-6-02 (audit cycle-1 BUG-1): a REST stock write on an existing
        row is a ledger-free inventory mutation, so updates expose `stock`
        read-only while creation keeps it writable (opening balance, not an
        edit). The field stays in the payload either way — pinned response
        shape lives in test_serializer_field_set_is_pinned."""
        self.assertFalse(ProductSerializer().fields["stock"].read_only)
        product = self.make_product()
        self.assertTrue(ProductSerializer(product).fields["stock"].read_only)
        self.assertTrue(
            ProductSerializer(
                product, data={"name": "Renamed"}, partial=True
            ).fields["stock"].read_only
        )


@tag("products")
class ProductStockEditLedgerGuardTests(ApiTestCase):
    """SPEC-6-02 (audit cycle-1 BUG-1): staff PATCH/PUT {"stock": N} used to
    return 200, move stock and write ZERO StockMovement rows — a ledger-free
    inventory mutation path. Updates now ignore `stock`, forcing every
    existing-row change through the sanctioned adjust_stock ledger path."""

    def setUp(self):
        self.staff = self.make_staff()
        self.api_login("staff")
        self.product = self.make_product(name="Rose Water", stock=10)

    def test_patch_with_stock_is_ignored_and_writes_no_movement(self):
        res = self.client.patch(
            f"/api/products/{self.product.slug}/", {"stock": 999}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)             # edit ignored
        self.assertEqual(StockMovement.objects.count(), 0)   # no mutation, no row

    def test_put_with_stock_is_ignored_and_writes_no_movement(self):
        res = self.client.put(
            f"/api/products/{self.product.slug}/",
            {
                "name": "Rose Water", "description": "desc", "price": "499.99",
                "size": 50, "stock": 999, "category": "Floral",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)             # edit ignored
        self.assertEqual(StockMovement.objects.count(), 0)   # no mutation, no row

    def test_update_response_still_reports_stock(self):
        """`stock` keeps its place in the public payload; only its
        writability changed."""
        res = self.client.patch(
            f"/api/products/{self.product.slug}/", {"stock": 999}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["stock"], 10)

    def test_create_still_sets_opening_stock_without_movement(self):
        """Creation is exempt (auditor scope note): the requested stock is
        the opening balance, not an edit — so no movement row is expected."""
        res = self.client.post(
            "/api/products/",
            {
                "name": "Opening Balance", "description": "desc",
                "price": "10.00", "size": 30, "stock": 42, "category": "Floral",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["stock"], 42)
        created = products.objects.get(slug="opening-balance")
        self.assertEqual(created.stock, 42)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_adjust_stock_remains_the_sanctioned_edit_path(self):
        """Anti-regression contrast: the same staff member who cannot PATCH
        stock can still move inventory — and only via the ledgered path."""
        res = self.client.patch(
            f"/api/products/{self.product.slug}/", {"stock": 999}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.product.adjust_stock(
            self.staff, 5, StockMovement.Reason.RESTOCK, "sanctioned path"
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 15)  # 10 (PATCH ignored) + 5
        movement = StockMovement.objects.get()
        self.assertEqual(movement.delta, 5)
        self.assertEqual(movement.stock_after, 15)
        self.assertEqual(movement.created_by, self.staff)


@tag("products")
class ProductCatalogIndexSchemaTests(ApiTestCase):
    """SPEC-8-05: spec 8.3 "Indexes" starting set for the catalogue tables.

    2551 ("Product status and category relationships"): category is the
    public listing/filter key and gets the explicit index. The status half
    is N/A for this schema — the products table has no lifecycle-status
    column (stock health is derived by ``stock_health``, not stored state),
    so there is nothing to index there. Slug (2549) stays satisfied by its
    UNIQUE constraint (2479) — a unique constraint already implies a
    backing index; SKU (2553) is equally constraint-covered and already
    pinned at the DB level by ProductVariantSkuUniquenessTests in
    test_variant.py. The pins here keep both halves conscious: the
    category index must exist physically, and slug must not grow a
    duplicate index on top of its constraint.
    """

    def _table_constraints(self, model):
        with connection.cursor() as cursor:
            return connection.introspection.get_constraints(
                cursor, model._meta.db_table
            )

    def test_meta_declares_the_category_index(self):
        """The model-level starting set. Later catalogue indexes must come
        from measured query patterns (2569), i.e. as a conscious edit to
        this set, never by silent accretion."""
        self.assertEqual(
            {tuple(index.fields) for index in products._meta.indexes},
            {("category",)},
        )

    def test_category_index_exists_at_db_level(self):
        covering = [
            info
            for info in self._table_constraints(products).values()
            if info["index"] and info["columns"] == ["category"]
        ]
        self.assertTrue(covering, "no index on products.category")

    def test_slug_stays_satisfied_by_its_unique_constraint(self):
        """2549: unique=True on slug (2479) already implies the lookup
        index the spec asks to start with — the pin rejects any duplicate
        index stacked on top of the constraint."""
        covering = [
            info
            for info in self._table_constraints(products).values()
            if info["columns"] == ["slug"]
        ]
        self.assertTrue(covering, "no constraint on slug at all")
        self.assertTrue(
            all(info["unique"] for info in covering),
            f"slug grew a non-unique duplicate index: {covering}",
        )


@tag("products")
class StockReservationModelTests(ApiTestCase):
    """SPEC-12-01 (spec section 12.1): the reservation data model.

    Schema core only — the lifecycle writers arrive later: SPEC-12-02
    creates reservations inside create_order's atomic block and converts
    them inside verify_payment's locked block; SPEC-12-03 expires them via
    the reconciler. Section 12.1's inventory split maps onto the schema
    as: on-hand inventory stays ``products.stock`` (the only authority),
    reserved inventory is the sum of a product's active reservations'
    quantity, available-to-sell is the derived difference (never stored —
    the reserved/safety-field accounting depth is SPEC-6-13's), expiry is
    ``expires_at``, and owner/reference are the ``owner``/``order`` FKs.
    """

    def setUp(self):
        self.owner = self.make_user(username="reserver")
        self.product = self.make_product(name="Rose Aurum", stock=10)

    def make_order(self, user=None):
        return Order.objects.create(
            user=user or self.owner,
            full_name="Rose Buyer",
            phone="9999999999",
            address="1 Rose Lane",
            city="Pune",
            state="MH",
            pincode="411001",
            total_amount=Decimal("499.99"),
        )

    def make_reservation(self, order=None, product=None, **overrides):
        fields = dict(
            product=product or self.product,
            order=order or self.make_order(),
            owner=self.owner,
            quantity=1,
            expires_at=timezone.now() + timedelta(seconds=900),
        )
        fields.update(overrides)
        return StockReservation.objects.create(**fields)

    def test_field_set_is_pinned(self):
        """Section 12.1 prescribes exactly this core: the product the units
        come off, the order reference and owner, the reserved quantity, the
        status lifecycle, expiry, and distinct created/updated timestamps.
        A new column must force a conscious decision here, never
        auto-appear."""
        names = {field.name for field in StockReservation._meta.concrete_fields}
        self.assertEqual(
            names,
            {
                "id",
                "product",
                "order",
                "owner",
                "quantity",
                "status",
                "expires_at",
                "created_at",
                "updated_at",
            },
        )

    def test_status_lifecycle_carries_the_spec_vocabulary(self):
        """§12.1 "Recommended checkout behaviour" names the transitions: a
        reservation is minted active at checkout (step 3), converted into
        a committed sale on payment confirmation (step 5), released by a
        failed/cancelled checkout (step 6), and expired by the scheduled
        reconciliation (step 7). The writers may only set these named
        states — never free-form strings — and a minted row starts
        active."""
        self.assertEqual(
            {value for value, _ in StockReservation.Status.choices},
            {"active", "converted", "released", "expired"},
        )
        field = StockReservation._meta.get_field("status")
        self.assertEqual(field.choices, StockReservation.Status.choices)
        self.assertEqual(field.get_default(), StockReservation.Status.ACTIVE)
        self.assertEqual(
            self.make_reservation().status, StockReservation.Status.ACTIVE
        )

    def test_fk_targets_follow_the_cross_app_string_pattern(self):
        """product resolves in-app; order and owner use the file's existing
        cross-app string-FK pattern (see StockMovement.created_by) so the
        products app never imports orders at model load. Deletion policy
        is cascade throughout: the reservation is a checkout artifact, so
        it is meaningless once its product or order is gone."""
        product_field = StockReservation._meta.get_field("product")
        order_field = StockReservation._meta.get_field("order")
        owner_field = StockReservation._meta.get_field("owner")
        self.assertIs(product_field.related_model, products)
        self.assertIs(order_field.related_model, Order)
        self.assertIs(owner_field.related_model, User)
        for field in (product_field, order_field, owner_field):
            self.assertIs(field.remote_field.on_delete, models.CASCADE)

        reservation = self.make_reservation()
        self.assertIn(reservation, self.product.stock_reservations.all())
        self.assertIn(reservation, reservation.order.stock_reservations.all())
        self.assertIn(reservation, self.owner.stock_reservations.all())

        reservation.order.delete()
        self.assertEqual(StockReservation.objects.count(), 0)

    def test_order_product_pair_is_unique_at_the_db_level(self):
        """Checkout mints at most one reservation per order line: a
        duplicate (order, product) row would double-count reserved stock
        in the available-to-sell math, and a retried checkout must
        re-target the existing hold, not mint a second one. Pinned at the
        DB level the same way ProductVariant.sku and Order.order_number
        are (§12.1 implies the constraint: "reserved inventory" is a
        per-checkout hold with an owner/reference)."""
        order = self.make_order()
        self.make_reservation(order=order)
        self.assertEqual(StockReservation.objects.count(), 1)
        # The duplicate create runs inside a nested atomic (savepoint): the
        # IntegrityError rolls back to the savepoint, leaving the TestCase
        # transaction usable for the introspection proof below.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_reservation(order=order)

        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, StockReservation._meta.db_table
            )
        pair_constraints = [
            info
            for info in constraints.values()
            if info["columns"] == ["order_id", "product_id"]
        ]
        self.assertTrue(
            any(info["unique"] for info in pair_constraints),
            f"no unique constraint on (order, product): {pair_constraints}",
        )

    def test_different_orders_hold_independent_reservations_on_one_product(self):
        """Uniqueness is per (order, product), not per product: two
        customers' concurrent checkouts each hold units of the same
        product as independent rows, and the reserved inventory is their
        summed quantity. The sufficiency gate is the atomic reservation
        create in SPEC-12-02, not this constraint."""
        first = self.make_reservation(quantity=1)
        second = self.make_reservation(quantity=3)
        self.assertEqual(first.product, second.product)
        reserved = StockReservation.objects.filter(
            product=self.product, status=StockReservation.Status.ACTIVE
        ).aggregate(total=Sum("quantity"))["total"]
        self.assertEqual(reserved, 4)

    def test_expiry_sweep_query_reads_only_stale_active_rows(self):
        """The reconciler's sweep (SPEC-12-03) reads status=active AND
        expires_at<=now; it must be exact on both axes. A still-fresh
        hold, and rows already converted/released/expired with a past
        expiry, are all out of scope — only the stale active hold is
        matched. This is the query shape the (status, expires_at) index
        serves."""
        stale = self.make_reservation(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.make_reservation(expires_at=timezone.now() + timedelta(hours=1))
        for status in (
            StockReservation.Status.CONVERTED,
            StockReservation.Status.RELEASED,
            StockReservation.Status.EXPIRED,
        ):
            done = self.make_reservation(
                expires_at=timezone.now() - timedelta(seconds=1)
            )
            done.status = status
            done.save()

        sweep = list(
            StockReservation.objects.filter(
                status=StockReservation.Status.ACTIVE,
                expires_at__lte=timezone.now(),
            )
        )
        self.assertEqual(sweep, [stale])

    def test_quantity_shares_the_stock_column_type(self):
        """Reserved units are counted in the same column type as on-hand
        stock (products.stock, StockMovement.stock_after): one integer
        domain for unit counts across the inventory schema."""
        field = StockReservation._meta.get_field("quantity")
        self.assertIs(type(field), type(products._meta.get_field("stock")))
        reservation = self.make_reservation(quantity=999_999)
        reservation.refresh_from_db()
        self.assertEqual(reservation.quantity, 999_999)

    def test_meta_pins_the_unique_constraint_and_expiry_index(self):
        """The model-level starting set for §12.1: the (order, product)
        uniqueness constraint plus the composite (status, expires_at)
        index the expiry-based queries read through. Later inventory
        indexes must come from measured query patterns, i.e. as a
        conscious edit to this set, never by silent accretion."""
        self.assertIn(
            "uniq_order_product_reservation",
            {constraint.name for constraint in StockReservation._meta.constraints},
        )
        self.assertEqual(
            {tuple(index.fields) for index in StockReservation._meta.indexes},
            {("status", "expires_at")},
        )

    def test_reservation_ttl_is_read_at_call_time(self):
        """expiry_from_now() is the single place a minting writer derives
        expires_at from: it must honour the env-driven RESERVATION_TTL at
        call time — the ops/services.py pattern — so an override applies
        to the very next call instead of being frozen at first import."""
        with override_settings(RESERVATION_TTL=60):
            before = timezone.now()
            expires = StockReservation.expiry_from_now()
            after = timezone.now()
            self.assertLessEqual(before + timedelta(seconds=60), expires)
            self.assertLessEqual(expires, after + timedelta(seconds=60))

        with override_settings(RESERVATION_TTL=120):
            later = StockReservation.expiry_from_now()
            self.assertGreater(later, expires)

    def test_reservation_ttl_env_parse_is_fail_safe(self):
        """settings.py resolves RESERVATION_TTL through _env_int: the
        documented default (900s — long enough to finish a payment session
        within the hold) stands when the key is absent, and a malformed
        value cannot crash startup."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_env_int("RESERVATION_TTL", 900), 900)
        with patch.dict(os.environ, {"RESERVATION_TTL": "not-a-number"}):
            self.assertEqual(_env_int("RESERVATION_TTL", 900), 900)
        with patch.dict(os.environ, {"RESERVATION_TTL": "450"}):
            self.assertEqual(_env_int("RESERVATION_TTL", 900), 450)

    def test_string_representation_names_product_quantity_and_status(self):
        reservation = self.make_reservation(quantity=2)
        self.assertEqual(str(reservation), "Rose Aurum: 2 reserved (active)")

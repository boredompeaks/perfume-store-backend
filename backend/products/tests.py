import base64
import math
import os
import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.admin.utils import flatten_fieldsets
from django.contrib.auth.models import AnonymousUser, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import (
    IntegrityError,
    connection,
    models,
    transaction,
)
from django.db.models import Sum
from django.test import TransactionTestCase, override_settings, tag
from django.urls import resolve
from django.utils import timezone
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
from common.testing import (
    ApiTestCase,
    covering_indexes,
    redundant_covering_indexes,
)
from config.settings import _env_int
from orders.models import Order
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
        self.p_rose = self.make_product(
            name="Rose Water", category="Floral", price="100.00"
        )
        self.p_oud = self.make_product(
            name="Oud Royale",
            category="Oriental",
            price="200.00",
            description="Smoky oud with saffron.",
        )
        self.p_jasmine = self.make_product(
            name="Jasmine Mist",
            category="Floral",
            price="300.00",
            description="A rose and jasmine blend.",
        )

    def _list_all(self, params=None):
        results, page = [], 1
        while True:
            res = self.client.get("/api/products/", {**(params or {}), "page": page})
            self.assertEqual(res.status_code, 200, res.data)
            results.extend(res.data["results"])
            if not res.data["next_page"]:
                return results
            page += 1

    def test_search_across_name_description_and_category(self):
        cases = {
            "Rose Water": {self.p_rose.id},
            "jasmine blend": {self.p_jasmine.id},
            "oriental": {self.p_oud.id},
            "ROSE": {self.p_rose.id, self.p_jasmine.id},
        }
        for term, expected in cases.items():
            with self.subTest(term=term):
                self.assertEqual(
                    {row["id"] for row in self._list_all({"search": term})}, expected
                )

    def test_search_without_match_returns_empty_page(self):
        res = self.client.get("/api/products/", {"search": "zzz-nothing-like-this"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 0)
        self.assertEqual(res.data["results"], [])

    def test_category_filter_is_case_insensitive(self):
        res = self.client.get("/api/products/", {"category": "FLORAL"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            {row["id"] for row in res.data["results"]},
            {self.p_rose.id, self.p_jasmine.id},
        )

    def test_price_filters_combine_with_category(self):
        res = self.client.get(
            "/api/products/",
            {"category": "floral", "min_price": "150", "max_price": "250"},
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 0)

        res = self.client.get("/api/products/", {"min_price": "150"})
        self.assertEqual(res.data["count"], 2)
        res = self.client.get("/api/products/", {"max_price": "250"})
        self.assertEqual(res.data["count"], 2)
        res = self.client.get(
            "/api/products/", {"min_price": "150", "max_price": "250"}
        )
        self.assertEqual({row["id"] for row in res.data["results"]}, {self.p_oud.id})

    def test_zero_price_bounds_are_applied_not_ignored(self):
        """0 is a real bound: Decimal("0") is falsy, so a truthiness test would silently drop it and a zero ceiling would return every product."""
        res = self.client.get("/api/products/", {"min_price": "0"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 3)

        res = self.client.get("/api/products/", {"max_price": "0"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 0)
        self.assertEqual(res.data["results"], [])

    def test_invalid_price_filter_returns_400(self):
        for params in (
            {"min_price": "abc"},
            {"max_price": "1.2.3"},
            {"min_price": "abc", "max_price": "5"},
        ):
            with self.subTest(params=params):
                res = self.client.get("/api/products/", params)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(
                    res.data["error"], "Price filters must be valid numbers"
                )

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
        self.assertEqual(
            [row["name"] for row in rows], sorted(row["name"] for row in rows)
        )

    def test_invalid_ordering_values_ignored_not_500(self):
        for ordering in (
            "user",
            "password",
            "nonexistent",
            "price,name",
            "-; DROP",
            "stock",
        ):
            with self.subTest(ordering=ordering):
                res = self.client.get("/api/products/", {"ordering": ordering})
                self.assertEqual(res.status_code, 200, res.data)
                self.assertEqual(res.data["count"], 3)

    @override_settings(PRODUCTS_PAGE_SIZE=2)
    def test_pagination_envelope_shape(self):
        res = self.client.get("/api/products/")
        self.assertEqual(res.status_code, 200, res.data)
        for key in (
            "count",
            "total_pages",
            "current_page",
            "next_page",
            "previous_page",
            "results",
        ):
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
        from django.utils import timezone

        fixed = timezone.make_aware(timezone.datetime(2026, 1, 1, 12, 0, 0))
        products.objects.update(
            created_at=fixed
        )  # identical timestamps -> id must break ties
        res = self.client.get("/api/products/")
        self.assertEqual(
            [row["id"] for row in res.data["results"]],
            sorted([row["id"] for row in res.data["results"]], reverse=True),
        )


@tag("products")
class SlugGenerationTests(ApiTestCase):
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
        self.assertEqual(self.make_product(name="###").slug, "product-2")

    def test_long_name_collision_truncated_to_slug_max_length(self):
        """Expected values are hand-written literals, never recomputed from the bound: 98 + len("-2") == the declared 100."""
        first = self.make_product(name="x" * 100)
        self.assertEqual(first.slug, "x" * 100)
        second = self.make_product(name="x" * 100)
        self.assertEqual(second.slug, "x" * 98 + "-2")
        self.assertEqual(len(second.slug), 100)

    def test_over_long_name_collision_against_a_stored_row(self):
        self.make_product(name="x" * 100)
        candidate = products(name="x" * 150)
        candidates = candidate._slug_candidates()
        next(candidates)
        self.assertEqual(next(candidates), "x" * 98 + "-2")

    def test_long_name_without_collision_truncated_to_slug_max_length(self):
        candidate = products(name="x" * 150)
        generated = next(candidate._slug_candidates())
        self.assertEqual(generated, "x" * 100)
        self.assertEqual(len(generated), 100)

    # U+00BD (vulgar fraction one half) slugifies to two characters, the mechanism that makes an
    # over-long generated value reachable from a row the database will accept.
    WIDENING_CHAR = "\u00bd"

    def test_storable_name_slugifying_wider_than_the_slug_column_is_clamped(self):
        product = self.make_product(name=self.WIDENING_CHAR * 60)
        self.assertEqual(product.slug, "12" * 50)
        self.assertEqual(len(product.slug), 100)
        # Re-read through the database: the risk this guards is a value the COLUMN rejects, so the
        # pin has to be about what the column accepted.
        stored = products.objects.get(pk=product.pk).slug
        self.assertEqual(stored, "12" * 50)

    def test_storable_widening_name_collision_lands_inside_the_slug_column(self):
        self.make_product(name=self.WIDENING_CHAR * 60)
        second = self.make_product(name=self.WIDENING_CHAR * 60)
        self.assertEqual(second.slug, "12" * 49 + "-2")
        self.assertEqual(len(second.slug), 100)
        self.assertNotEqual(second.slug, "12" * 50)

    def test_one_over_the_bound_generation_loses_exactly_one_character(self):
        candidate = products(name="z" * 101)
        self.assertEqual(next(candidate._slug_candidates()), "z" * 100)

    def test_slug_max_length_declaration_is_pinned(self):
        self.assertEqual(products._meta.get_field("slug").max_length, 100)

    def test_slug_at_the_bound_is_left_whole(self):
        self.assertEqual(self.make_product(name="y" * 99).slug, "y" * 99)
        self.assertEqual(self.make_product(name="y" * 100).slug, "y" * 100)

    def test_two_names_sharing_a_prefix_get_distinct_slugs(self):
        """Both names slugify to the SAME 100 characters, so the discriminator can only come from the collision branch; truncating the finished candidate would collapse the pair onto one slug."""
        first = self.make_product(name="o" * 100)
        second = self.make_product(name="O" * 100)
        self.assertEqual(first.slug, "o" * 100)
        self.assertEqual(second.slug, "o" * 98 + "-2")
        self.assertNotEqual(first.slug, second.slug)
        third = products(name="o" * 120 + "cc")
        candidates = third._slug_candidates()
        next(candidates)
        next(candidates)
        self.assertEqual(next(candidates), "o" * 98 + "-3")

    def test_collision_suffix_keeps_every_candidate_inside_the_bound(self):
        base = "w" * 100
        first = self.make_product(name=base)
        self.assertEqual(first.slug, "w" * 100)
        slugs = [first.slug]
        for _ in range(11):
            slugs.append(self.make_product(name=base).slug)
        self.assertEqual(len(slugs), 12)
        self.assertEqual(len(set(slugs)), 12)
        self.assertEqual(slugs[-1], "w" * 97 + "-12")
        self.assertEqual(len(slugs[-1]), 100)
        for slug in slugs:
            self.assertLessEqual(len(slug), 100)
        self.assertEqual(products.objects.filter(slug__in=slugs).count(), 12)

    def test_empty_and_punctuation_names_use_the_fallback(self):
        self.assertEqual(self.make_product(name="").slug, "product")
        self.assertEqual(self.make_product(name="###").slug, "product-2")
        candidates = products(name="!" * 150)._slug_candidates()
        next(candidates)
        next(candidates)
        self.assertEqual(next(candidates), "product-3")

    def test_non_slug_integrity_error_propagates(self):
        candidate = products(
            name="Rose Water",
            description="d",
            price="10.00",
            size=1,
            stock=1,
            category="Floral",
        )
        with patch(
            "django.db.models.Model.save",
            side_effect=IntegrityError("some other constraint"),
        ):
            with self.assertRaises(IntegrityError):
                candidate.save()

    def test_explicit_slug_preserved_on_resave(self):
        product = self.make_product(name="Rose Water", slug="custom-slug")
        product.stock = 5
        product.save()
        product.refresh_from_db()
        self.assertEqual(product.slug, "custom-slug")
        product.name = "Renamed Product"
        product.save()
        product.refresh_from_db()
        self.assertEqual(product.slug, "custom-slug")


@tag("products")
class SlugGenerationRaceTests(TransactionTestCase):
    """TransactionTestCase, not TestCase: the point is two SEPARATE database connections holding real row locks, and a TestCase transaction would put both workers in the same one."""

    def test_two_threads_one_name_both_rows_survive(self):
        import threading

        from django.db import OperationalError, close_old_connections

        name = "Contended Rose Aurum"
        barrier = threading.Barrier(2)
        errors = []

        def worker():
            close_old_connections()
            try:
                barrier.wait()
                products.objects.create(
                    name=name,
                    description="d",
                    price="10.00",
                    size=1,
                    stock=1,
                    category="Floral",
                )
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        if connection.vendor == "postgresql":
            self.assertEqual(errors, [])
            self.assertEqual(products.objects.count(), 2)
            slugs = set(products.objects.values_list("slug", flat=True))
            self.assertEqual(len(slugs), 2)
        else:
            # SQLite serialises writes: a genuinely concurrent insert is refused with OperationalError, the
            # engine doing mutual exclusion itself; an IntegrityError escaping the slug path is never acceptable.
            for exc in errors:
                self.assertIsInstance(exc, OperationalError)


@tag("products")
class ProductWritePermissionTests(ApiTestCase):
    def setUp(self):
        self.staff = self.make_staff()
        self.user = self.make_user("customer")
        self.product = self.make_product(name="Rose Water")

    def _forbidden_for_anon_and_customer(self, method, payload=None):
        for client_name, client in (
            ("anon", self.fresh_client()),
            ("customer", self.fresh_client()),
        ):
            if client_name == "customer":
                self.api_login("customer", client=client)
            res = getattr(client, method.lower())(
                f"/api/products/{self.product.slug}/", payload, format="json"
            )
            self.assertEqual(res.status_code, 403, (client_name, res.status_code))
            self.assertEqual(res.data["error"], "Administrator access is required.")
        self.product.refresh_from_db()

    def test_create_requires_staff(self):
        payload = {
            "name": "New Perfume",
            "description": "desc",
            "price": "10.00",
            "size": 30,
            "stock": 1,
            "category": "Floral",
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
            "name": "Rose Water",
            "description": "updated",
            "price": "111.00",
            "size": 50,
            "stock": 9,
            "category": "Floral",
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
        res = client.patch(
            f"/api/products/{self.product.slug}/", {"price": "111.00"}, format="json"
        )
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
        res = client.post(
            "/api/products/", {"name": "X", "price": "not-a-number"}, format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("price", res.data["details"])

    def test_staff_put_and_patch_invalid_payload_rejected_400(self):
        client = self.fresh_client()
        self.api_login("staff", client=client)
        res = client.put(
            f"/api/products/{self.product.slug}/", {"name": ""}, format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("name", res.data["details"])
        res = client.patch(
            f"/api/products/{self.product.slug}/", {"price": "nope"}, format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("price", res.data["details"])


@tag("products")
class ProductDetailTests(ApiTestCase):
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

    def test_serializer_field_set_is_pinned(self):
        product = self.make_product()
        res = self.client.get(f"/api/products/{product.slug}/")
        self.assertEqual(
            set(res.data.keys()),
            {
                "id",
                "name",
                "slug",
                "description",
                "price",
                "size",
                "stock",
                "category",
                "image",
                "created_at",
            },
        )


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
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_negative_delta_allowed_within_stock(self):
        self.product.adjust_stock(self.admin, -4, StockMovement.Reason.CORRECTION)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 6)

    def test_adjust_stock_re_reads_the_locked_row(self):
        """adjust_stock runs under atomic + select_for_update, so it must gate on the row's current value, not the instance's stale snapshot."""
        products.objects.filter(pk=self.product.pk).update(stock=50)
        self.product.adjust_stock(self.admin, -5, StockMovement.Reason.CORRECTION)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 45)
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
        self.assertEqual(self.product.stock_health, "ok")
        self.product.adjust_stock(self.admin, -5, StockMovement.Reason.CORRECTION)
        self.assertEqual(self.product.stock_health, "low")
        self.product.adjust_stock(self.admin, -5, StockMovement.Reason.DAMAGE)
        self.assertEqual(self.product.stock_health, "out")


@tag("products")
class InventoryAdjustApiTests(ApiTestCase):
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

    def test_anonymous_caller_is_denied_403(self):
        res = self._post(self._payload(), client=self.fresh_client())
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_catalogue_role_lacks_inventory_adjust_403(self):
        catmgr = self._user_with_role("catmgr", ROLE_CATALOGUE)
        self.client.force_authenticate(catmgr)
        res = self._post(self._payload())
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(
            res.data["error"], "You do not have permission to perform this action."
        )
        self.assertEqual(StockMovement.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)

    def test_inventory_role_is_allowed(self):
        self.client.force_authenticate(self.invmgr)
        res = self._post(self._payload())
        self.assertEqual(res.status_code, 201, res.data)

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
        self.client.force_authenticate(self.invmgr)
        res = self._post(self._payload(delta=-11, reason=StockMovement.Reason.DAMAGE))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("below zero", res.data["error"])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(StockMovement.objects.count(), 0)

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

    ADMIN_URLS = (
        "/api/v1/admin/inventory/adjustments/",
        "/api/admin/inventory/adjustments/",
    )

    def test_admin_family_paths_reach_the_one_adjustment_view(self):
        from config.urls import v1_admin_patterns
        from products.views import inventory_adjust

        paths = (
            *self.ADMIN_URLS,
            self.URL,
            "/api/v1/store/products/inventory/adjustments/",
        )
        for url in paths:
            with self.subTest(url=url):
                self.assertIs(resolve(url).func, inventory_adjust)

        registrations = [
            pattern
            for pattern in v1_admin_patterns
            if getattr(pattern, "callback", None) is inventory_adjust
        ]
        self.assertEqual(len(registrations), 1)

    def test_admin_path_enforces_the_inventory_adjust_capability(self):
        for url in self.ADMIN_URLS:
            with self.subTest(url=url):
                self.client.force_authenticate(self.invmgr)
                res = self.client.post(url, self._payload(), format="json")
                self.assertEqual(res.status_code, 201, res.data)

    def test_admin_path_refuses_a_staff_role_without_the_capability(self):
        catmgr = self._user_with_role("catmgr-admin", ROLE_CATALOGUE)
        for url in self.ADMIN_URLS:
            with self.subTest(url=url):
                self.client.force_authenticate(catmgr)
                res = self.client.post(url, self._payload(), format="json")
                self.assertEqual(res.status_code, 403, res.data)
                self.assertEqual(
                    res.data["error"],
                    "You do not have permission to perform this action.",
                )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_admin_path_refuses_a_non_staff_caller(self):
        customer = self.make_user("plain-customer")
        for url in self.ADMIN_URLS:
            with self.subTest(url=url):
                self.client.force_authenticate(customer)
                res = self.client.post(url, self._payload(), format="json")
                self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_admin_path_refuses_an_anonymous_caller(self):
        for url in self.ADMIN_URLS:
            with self.subTest(url=url):
                res = self.fresh_client().post(url, self._payload(), format="json")
                self.assertEqual(res.status_code, 403, res.data)
                self.assertEqual(res.data["code"], "permission_denied")
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_deprecated_store_aliases_still_serve_the_write(self):
        for url in (self.URL, "/api/v1/store/products/inventory/adjustments/"):
            with self.subTest(url=url):
                self.client.force_authenticate(self.invmgr)
                res = self.client.post(url, self._payload(), format="json")
                self.assertEqual(res.status_code, 201, res.data)


@tag("products")
class ProductAdminTests(ApiTestCase):
    def setUp(self):
        User.objects.create_superuser(
            "padmin", "padmin@example.com", "S3cure-Passphrase!"
        )
        self.assertTrue(
            self.client.login(username="padmin", password="S3cure-Passphrase!")
        )
        self.with_image = products.objects.create(
            name="Imaged Rose",
            description="d",
            price="50.00",
            size=30,
            stock=0,
            category="Floral",
            image=SimpleUploadedFile("rose.png", TINY_PNG, content_type="image/png"),
        )
        self.plain = products.objects.create(
            name="Plain Oud",
            description="d",
            price="60.00",
            size=30,
            stock=3,
            category="Oriental",
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
        self.assertContains(res, "out of stock")
        self.assertContains(res, "low (3)")
        self.assertContains(res, "rose_")

    def test_changelist_renders_inline_stock_next_to_price(self):
        self.assertIn("stock", ProductAdmin.list_editable)
        res = self.client.get("/admin/products/products/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="form-0-price"')
        self.assertContains(res, 'name="form-0-stock"')
        self.assertContains(res, 'class="field-stock"')

    def test_changelist_bulk_save_of_price_leaves_stock_ledger_free(self):
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
                "form-0-stock": "3",
                "_save": "Save",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.price, Decimal("77.00"))
        self.assertEqual(self.plain.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_change_page_stock_is_display_only_add_page_keeps_input(self):
        """The change page's stock input was a live ledger-free mutation path; it is read-only once the row exists, while the add page keeps the input for the opening balance."""
        res = self.client.get(f"/admin/products/products/{self.plain.id}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, 'name="stock"')
        res = self.client.get("/admin/products/products/add/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="stock"')

    def test_change_form_save_ignores_tampered_stock(self):
        res = self.client.post(
            f"/admin/products/products/{self.plain.id}/change/",
            {
                "name": "Plain Oud",
                "category": "Oriental",
                "description": "d",
                "price": "88.00",
                "size": "30",
                "stock": "999",
                "stock_movements-TOTAL_FORMS": "0",
                "stock_movements-INITIAL_FORMS": "0",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.price, Decimal("88.00"))
        self.assertEqual(self.plain.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_change_page_renders_with_movement_inline_and_preview(self):
        self.with_image.adjust_stock(
            None, 7, StockMovement.Reason.RESTOCK, "initial fill"
        )

        res = self.client.get(f"/admin/products/products/{self.with_image.id}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Inventory history (all mutations)")
        self.assertContains(res, "rose_")
        self.assertContains(
            res, "Imaged Rose: +7 (restock)"
        )  # movement __str__ in the inline

        res = self.client.get(f"/admin/products/products/{self.plain.id}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "No image uploaded.")

    def test_adjust_stock_action_full_flow(self):
        res = self._run_action("adjust_stock", self.plain)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Adjusting stock for the following 1 product(s)")
        self.assertContains(res, "Plain Oud")

        res = self._run_action(
            "adjust_stock",
            self.plain,
            {
                "apply": "1",
                "delta": "20",
                "reason": "restock",
                "note": "supplier delivery",
            },
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
        self.assertEqual(self.plain.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_adjust_stock_action_rejects_invalid_form(self):
        res = self._run_action(
            "adjust_stock",
            self.plain,
            {"apply": "1", "delta": "not-a-number", "reason": "restock"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Change in stock")
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.stock, 3)

    def test_export_csv_streams_selected_products(self):
        res = self._run_action("export_csv", self.plain)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "text/csv")
        body = res.content.decode()
        self.assertIn("id,name,slug,category,price,size,stock,created_at", body)
        self.assertIn("Plain Oud", body)


@tag("products")
class ProductFreeShippingFlagTests(ApiTestCase):
    """The flag is a merchant-editable delivery rule; the public store payload deliberately does not carry it, because no storefront badge consumes it and the total already reflects the waiver."""

    def setUp(self):
        self.product = self.make_product(name="Free Ship Oud")

    def _change_payload(self, **overrides):
        payload = {
            "name": self.product.name,
            "category": self.product.category,
            "description": self.product.description,
            "price": str(self.product.price),
            "size": str(self.product.size),
            "stock_movements-TOTAL_FORMS": "0",
            "stock_movements-INITIAL_FORMS": "0",
        }
        payload.update(overrides)
        return payload

    def _staff_client(self, username, role):
        user = User.objects.create_user(
            username,
            f"{username}@example.com",
            "S3cure-Passphrase!",
            is_staff=True,
        )
        user.groups.add(sync_role_groups()[role])
        client = self.client_class()
        client.force_login(user)
        return client

    def test_defaults_off_and_round_trips(self):
        self.assertIs(self.product.free_shipping, False)

        self.product.free_shipping = True
        self.product.save(update_fields=["free_shipping"])

        self.product.refresh_from_db()
        self.assertIs(self.product.free_shipping, True)

    def test_public_product_payload_does_not_expose_the_flag(self):
        res = self.client.get(f"/api/products/{self.product.slug}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn("free_shipping", res.data)
        self.assertNotIn("free_shipping", ProductSerializer.Meta.fields)

    def test_admin_fieldset_exposes_the_flag(self):
        self.assertIn("free_shipping", flatten_fieldsets(ProductAdmin.fieldsets))

    def test_products_write_role_can_set_the_flag(self):
        client = self._staff_client("flag-editor", ROLE_CATALOGUE)

        res = client.post(
            f"/admin/products/products/{self.product.id}/change/",
            self._change_payload(free_shipping="on"),
            follow=True,
        )

        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertIs(self.product.free_shipping, True)

    def test_role_without_products_write_cannot_edit_the_flag(self):
        client = self._staff_client("flag-viewer", ROLE_SUPPORT)

        res = client.get(f"/admin/products/products/{self.product.id}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, 'name="free_shipping"')

        res = client.post(
            f"/admin/products/products/{self.product.id}/change/",
            self._change_payload(free_shipping="on"),
        )
        self.assertEqual(res.status_code, 403)
        self.product.refresh_from_db()
        self.assertIs(self.product.free_shipping, False)


EXPECTED_PRODUCT_FIELDS = [
    "id",
    "name",
    "slug",
    "description",
    "price",
    "size",
    "stock",
    "category",
    "image",
    "created_at",
]

# is_active and free_shipping are deliberately NOT exposed on the public payload: is_active is
# delist state (a bare flag would leak existence), and no storefront badge consumes free_shipping.
SERIALIZER_EXCLUDED_FIELDS = {"is_active", "free_shipping"}


@tag("products")
class IsAdminUserOrReadOnlyUnitTests(ApiTestCase):
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
                self.assertTrue(self.permission.has_permission(staff_request, None))


@tag("products")
class ProductPermissionClassesApiTests(ApiTestCase):
    def setUp(self):
        self.staff = self.make_staff()
        self.product = self.make_product(name="Rose Water")

    def test_read_methods_stay_public_for_anonymous(self):
        client = self.fresh_client()
        self.assertEqual(client.get("/api/products/").status_code, 200)
        self.assertEqual(
            client.get(f"/api/products/{self.product.slug}/").status_code, 200
        )
        # OPTIONS is a SAFE_METHOD too; HEAD is not routed by the FBV (405 before and after), so it is
        # not asserted here.
        self.assertEqual(
            client.options(f"/api/products/{self.product.slug}/").status_code, 200
        )

    def test_anonymous_write_on_unknown_slug_is_403_not_404(self):
        res = self.fresh_client().put(
            "/api/products/no-such-slug/", {"price": "1.00"}, format="json"
        )
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["error"], "Administrator access is required.")

    def test_staff_write_still_succeeds_end_to_end(self):
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
    def setUp(self):
        self.product = self.make_product(name="Gate Rose")
        groups = sync_role_groups()
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
        res = client.post("/api/products/", self._write_payload("Sneak"), format="json")
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
    def test_fields_are_declared_explicitly(self):
        fields = ProductSerializer.Meta.fields
        self.assertIsInstance(fields, (list, tuple))
        self.assertEqual(list(fields), EXPECTED_PRODUCT_FIELDS)

    def test_whitelist_covers_every_field_or_declares_its_exclusion(self):
        """The explicit list must equal what __all__ exposed minus the columns a written decision excludes; a new model column must force a conscious decision, never auto-leak."""
        concrete = {field.name for field in products._meta.concrete_fields}
        self.assertEqual(
            set(ProductSerializer.Meta.fields),
            concrete - SERIALIZER_EXCLUDED_FIELDS,
        )
        self.assertLessEqual(SERIALIZER_EXCLUDED_FIELDS, concrete)

    def test_stock_writable_on_create_read_only_on_update(self):
        """A REST stock write on an existing row is a ledger-free inventory mutation, so updates expose stock read-only while creation keeps it writable as the opening balance."""
        self.assertFalse(ProductSerializer().fields["stock"].read_only)
        product = self.make_product()
        self.assertTrue(ProductSerializer(product).fields["stock"].read_only)
        self.assertTrue(
            ProductSerializer(product, data={"name": "Renamed"}, partial=True)
            .fields["stock"]
            .read_only
        )


@tag("products")
class ProductStockEditLedgerGuardTests(ApiTestCase):
    """Updates ignore stock so every existing-row change goes through adjust_stock: a REST stock write would be a ledger-free inventory mutation."""

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
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_put_with_stock_is_ignored_and_writes_no_movement(self):
        res = self.client.put(
            f"/api/products/{self.product.slug}/",
            {
                "name": "Rose Water",
                "description": "desc",
                "price": "499.99",
                "size": 50,
                "stock": 999,
                "category": "Floral",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_update_response_still_reports_stock(self):
        res = self.client.patch(
            f"/api/products/{self.product.slug}/", {"stock": 999}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["stock"], 10)

    def test_create_still_sets_opening_stock_without_movement(self):
        """Creation is exempt: the requested stock is the opening balance, not an edit, so no movement row is expected."""
        res = self.client.post(
            "/api/products/",
            {
                "name": "Opening Balance",
                "description": "desc",
                "price": "10.00",
                "size": 30,
                "stock": 42,
                "category": "Floral",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["stock"], 42)
        created = products.objects.get(slug="opening-balance")
        self.assertEqual(created.stock, 42)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_adjust_stock_remains_the_sanctioned_edit_path(self):
        res = self.client.patch(
            f"/api/products/{self.product.slug}/", {"stock": 999}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.product.adjust_stock(
            self.staff, 5, StockMovement.Reason.RESTOCK, "sanctioned path"
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 15)
        movement = StockMovement.objects.get()
        self.assertEqual(movement.delta, 5)
        self.assertEqual(movement.stock_after, 15)
        self.assertEqual(movement.created_by, self.staff)


@tag("products")
class ProductCatalogIndexSchemaTests(ApiTestCase):
    """The status half is N/A: the products table has no lifecycle-status column, and later indexes must come from measured query patterns, never silent accretion."""

    def _table_constraints(self, model):
        with connection.cursor() as cursor:
            return connection.introspection.get_constraints(
                cursor, model._meta.db_table
            )

    def test_meta_declares_the_category_index(self):
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
        """unique=True on slug already implies the lookup index the spec asks for; the one non-unique covering index a correct schema may carry is the engine's varchar_pattern_ops LIKE index."""
        covering = covering_indexes(products, "slug")
        self.assertTrue(covering, "no constraint on slug at all")
        self.assertTrue(
            any(info["unique"] for info in covering.values()),
            f"slug has no unique covering constraint: {covering}",
        )
        self.assertEqual(
            redundant_covering_indexes(products, "slug"),
            {},
            "slug grew a non-unique duplicate index",
        )


@tag("products")
class StockReservationModelTests(ApiTestCase):
    """Schema core only: the lifecycle writers arrive later; on-hand inventory stays products.stock, while reservations track the checkout-time claims."""

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
        """Section 12.1 prescribes this core; a new column must force a conscious decision here, never auto-appear."""
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
        self.assertEqual(
            {value for value, _ in StockReservation.Status.choices},
            {"active", "converted", "released", "expired"},
        )
        field = StockReservation._meta.get_field("status")
        self.assertEqual(field.choices, StockReservation.Status.choices)
        self.assertEqual(field.get_default(), StockReservation.Status.ACTIVE)
        self.assertEqual(self.make_reservation().status, StockReservation.Status.ACTIVE)

    def test_fk_targets_follow_the_cross_app_string_pattern(self):
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
        """A duplicate (order, product) row would double-count reserved stock; a retried checkout must re-target the existing hold, so DB uniqueness is the authority."""
        order = self.make_order()
        self.make_reservation(order=order)
        self.assertEqual(StockReservation.objects.count(), 1)
        # The duplicate create runs inside a nested atomic savepoint, so the IntegrityError rolls back
        # to it and leaves the TestCase transaction usable.
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
        first = self.make_reservation(quantity=1)
        second = self.make_reservation(quantity=3)
        self.assertEqual(first.product, second.product)
        reserved = StockReservation.objects.filter(
            product=self.product, status=StockReservation.Status.ACTIVE
        ).aggregate(total=Sum("quantity"))["total"]
        self.assertEqual(reserved, 4)

    def test_expiry_sweep_query_reads_only_stale_active_rows(self):
        stale = self.make_reservation(expires_at=timezone.now() - timedelta(seconds=1))
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
        field = StockReservation._meta.get_field("quantity")
        self.assertIs(type(field), type(products._meta.get_field("stock")))
        reservation = self.make_reservation(quantity=999_999)
        reservation.refresh_from_db()
        self.assertEqual(reservation.quantity, 999_999)

    def test_meta_pins_the_unique_constraint_and_expiry_index(self):
        self.assertIn(
            "uniq_order_product_reservation",
            {constraint.name for constraint in StockReservation._meta.constraints},
        )
        self.assertEqual(
            {tuple(index.fields) for index in StockReservation._meta.indexes},
            {("status", "expires_at")},
        )

    def test_reservation_ttl_is_read_at_call_time(self):
        """expiry_from_now() is the single place a minting writer derives expires_at from: it must honour RESERVATION_TTL at call time so an override applies to the very next call."""
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
        """RESERVATION_TTL resolves through _env_int: the documented 900s default stands when the key is absent, and a malformed value cannot crash startup."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_env_int("RESERVATION_TTL", 900), 900)
        with patch.dict(os.environ, {"RESERVATION_TTL": "not-a-number"}):
            self.assertEqual(_env_int("RESERVATION_TTL", 900), 900)
        with patch.dict(os.environ, {"RESERVATION_TTL": "450"}):
            self.assertEqual(_env_int("RESERVATION_TTL", 900), 450)

    def test_string_representation_names_product_quantity_and_status(self):
        reservation = self.make_reservation(quantity=2)
        self.assertEqual(str(reservation), "Rose Aurum: 2 reserved (active)")


@tag("products")
class ImageUploadSizeCapTests(ApiTestCase):
    """The cap lives on the model field as validate_image_size, so DRF's ModelSerializer and the admin's ModelForm share one enforcement point."""

    def setUp(self):
        self.staff = self.make_staff()
        self.client = self.fresh_client()
        self.api_login("staff", client=self.client)

    def _png(self, fill_bytes):
        return SimpleUploadedFile(
            "big.png", TINY_PNG + b"x" * fill_bytes, content_type="image/png"
        )

    def test_oversized_api_upload_is_rejected_400(self):
        payload = {
            "name": "Too Big",
            "description": "d",
            "price": "10.00",
            "size": 30,
            "stock": 1,
            "category": "Floral",
            "image": self._png(settings.MAX_UPLOAD_MB * 1024 * 1024 + 1),
        }
        res = self.client.post("/api/products/", payload, format="multipart")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("image", res.data["details"])
        self.assertIn("maximum upload size", res.data["details"]["image"][0])
        self.assertFalse(products.objects.filter(name="Too Big").exists())

    def test_boundary_api_upload_passes(self):
        payload = {
            "name": "Exactly At Cap",
            "description": "d",
            "price": "10.00",
            "size": 30,
            "stock": 1,
            "category": "Floral",
            # Cap minus the TINY_PNG header already in the buffer: the body lands exactly at the limit, and
            # `>` (not `>=`) admits it.
            "image": self._png(settings.MAX_UPLOAD_MB * 1024 * 1024 - len(TINY_PNG)),
        }
        res = self.client.post("/api/products/", payload, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(products.objects.filter(name="Exactly At Cap").exists())

    def test_no_image_payload_still_passes(self):
        payload = {
            "name": "No Image",
            "description": "d",
            "price": "10.00",
            "size": 30,
            "stock": 1,
            "category": "Floral",
        }
        res = self.client.post("/api/products/", payload, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)

    def test_validator_passes_through_empty_values(self):
        from products.models import validate_image_size

        validate_image_size(None)
        validate_image_size(SimpleUploadedFile("empty.png", b""))

    def test_env_override_tightens_the_cap(self):
        with override_settings(MAX_UPLOAD_MB=1):
            payload = {
                "name": "Tighter Cap",
                "description": "d",
                "price": "10.00",
                "size": 30,
                "stock": 1,
                "category": "Floral",
                "image": self._png(1024 * 1024 + 1),
            }
            res = self.client.post("/api/products/", payload, format="multipart")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("1 MB", res.data["details"]["image"][0])

    def test_admin_form_rejects_oversized_upload(self):
        product = self.make_product(name="Admin Rose")
        admin_client = self.client_class()
        admin_client.force_login(self.staff)
        res = admin_client.post(
            f"/admin/products/products/{product.id}/change/",
            {
                "name": "Admin Rose",
                "description": "d",
                "price": "10.00",
                "size": 30,
                "stock": 1,
                "category": "Floral",
                "products-stockmovement_set-TOTAL_FORMS": "0",
                "products-stockmovement_set-INITIAL_FORMS": "0",
                "products-stockmovement_set-MIN_NUM_FORMS": "0",
                "products-stockmovement_set-MAX_NUM_FORMS": "1000",
                "image": self._png(settings.MAX_UPLOAD_MB * 1024 * 1024 + 1),
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "maximum upload size")
        product.refresh_from_db()
        self.assertFalse(product.image)

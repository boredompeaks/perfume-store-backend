import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import tag
from django.urls import resolve
from django.utils import timezone
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle

from cart.models import Cart, CartItem
from cart.views import (
    CartMutationRateThrottle,
    cart_coupon,
    cart_detail,
    cart_item_detail,
)
from common.testing import ApiTestCase
from orders.models import Order


@tag("cart")
class CartLazyCreationTests(ApiTestCase):
    def test_get_creates_session_cart_lazily(self):
        self.assertEqual(Cart.objects.count(), 0)

        res = self.client.get("/api/cart/")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["items"], [])
        self.assertEqual(Cart.objects.count(), 1)

    def test_get_reuses_the_same_cart_across_requests(self):
        first = self.client.get("/api/cart/").data
        second = self.client.get("/api/cart/").data
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(Cart.objects.count(), 1)


@tag("cart")
class CartAddItemTests(ApiTestCase):
    def setUp(self):
        self.product = self.make_product(stock=5)

    def _add(self, quantity, product_id=None):
        return self.client.post(
            "/api/cart/",
            {
                "product_id": product_id if product_id is not None else self.product.id,
                "quantity": quantity,
            },
            format="json",
        )

    def test_add_item_creates_row_with_correct_quantity(self):
        res = self._add(2)

        self.assertEqual(res.status_code, 201, res.data)
        item = CartItem.objects.get()
        self.assertEqual(item.cart, Cart.objects.get())
        self.assertEqual(item.product, self.product)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(len(res.data["items"]), 1)
        self.assertEqual(res.data["items"][0]["quantity"], 2)
        self.assertEqual(res.data["items"][0]["product"]["id"], self.product.id)
        self.assertEqual(res.data["items"][0]["product"]["name"], self.product.name)

    def test_add_item_defaults_to_quantity_one(self):
        res = self.client.post(
            "/api/cart/", {"product_id": self.product.id}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(CartItem.objects.get().quantity, 1)

    def test_add_same_product_accumulates_quantity(self):
        self.assertEqual(self._add(1).status_code, 201)
        res = self._add(2)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 3)
        self.assertEqual(res.data["items"][0]["quantity"], 3)

    def test_overstock_rejected_on_new_row(self):
        res = self._add(6)
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Not enough stock")
        self.assertEqual(CartItem.objects.count(), 0)

    def test_overstock_rejected_on_accumulate(self):
        self.assertEqual(self._add(3).status_code, 201)
        res = self._add(3)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Not enough stock")
        self.assertEqual(CartItem.objects.get().quantity, 3)

    def test_add_item_validation_errors(self):
        cases = [
            ({}, 400, "product_id is required"),
            ({"quantity": 1}, 400, "product_id is required"),
            ({"product_id": 999999}, 404, "Product not found"),
            ({"product_id": "not-a-number"}, 404, "Product not found"),
            (
                {"product_id": self.product.id, "quantity": "abc"},
                400,
                "Quantity must be a number",
            ),
            (
                {"product_id": self.product.id, "quantity": 0},
                400,
                "Quantity must be greater than 0",
            ),
            (
                {"product_id": self.product.id, "quantity": -2},
                400,
                "Quantity must be greater than 0",
            ),
        ]
        for payload, expected_status, expected_error in cases:
            with self.subTest(payload=payload):
                res = self.client.post("/api/cart/", payload, format="json")
                self.assertEqual(res.status_code, expected_status, payload)
                self.assertEqual(res.data["error"], expected_error)
        self.assertEqual(CartItem.objects.count(), 0)


@tag("cart")
class CartItemPatchTests(ApiTestCase):
    def setUp(self):
        self.product = self.make_product(stock=5)
        self.seed_session_cart([(self.product, 1)])
        self.item = CartItem.objects.get()

    def _patch(self, payload):
        return self.client.patch(f"/api/cart/{self.item.id}/", payload, format="json")

    def test_patch_rejects_invalid_quantities(self):
        cases = [
            ({"quantity": 0}, "Quantity must be greater than 0"),
            ({"quantity": -1}, "Quantity must be greater than 0"),
            ({"quantity": "abc"}, "Quantity must be a number"),
            ({"quantity": None}, "quantity is required"),
            ({}, "quantity is required"),
            ({"quantity": 6}, "Not enough stock"),
        ]
        for payload, expected_error in cases:
            with self.subTest(payload=payload):
                res = self._patch(payload)
                self.assertEqual(res.status_code, 400, payload)
                self.assertEqual(res.data["error"], expected_error)

        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)

    def test_patch_updates_quantity(self):
        res = self._patch({"quantity": 4})
        self.assertEqual(res.status_code, 200, res.data)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 4)
        self.assertEqual(res.data["items"][0]["quantity"], 4)

    def test_patch_boundary_quantity_equal_to_stock_allowed(self):
        res = self._patch({"quantity": 5})
        self.assertEqual(res.status_code, 200, res.data)


@tag("cart")
class CartItemDeleteTests(ApiTestCase):
    def setUp(self):
        self.product = self.make_product()
        self.other = self.make_product(name="Oud Royale", category="Oriental")
        self.seed_session_cart([(self.product, 2), (self.other, 1)])

    def test_delete_removes_only_target_item(self):
        item = CartItem.objects.get(product=self.product)
        res = self.client.delete(f"/api/cart/{item.id}/")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual(
            [row["product"]["id"] for row in res.data["items"]], [self.other.id]
        )

    def test_delete_unknown_item_returns_404(self):
        res = self.client.delete("/api/cart/999999/")
        self.assertEqual(res.status_code, 404, res.data)

    def test_v09_cart_payload_excludes_session_id(self):
        res = self.client.get("/api/cart/")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            set(res.data.keys()),
            {"id", "items", "coupon_code", "created_at", "updated_at"},
        )
        session_key = self.client.session.session_key
        self.assertTrue(session_key)
        self.assertNotIn(session_key, json.dumps(res.data))


@tag("cart")
class CrossSessionIsolationTests(ApiTestCase):
    def test_item_from_another_session_is_404(self):
        owner_product = self.make_product()
        self.seed_session_cart([(owner_product, 2)])
        owned_item = CartItem.objects.get()

        attacker = self.fresh_client()
        self.assertEqual(
            attacker.get("/api/cart/").status_code, 200
        )  # own session/cart

        res = attacker.patch(
            f"/api/cart/{owned_item.id}/", {"quantity": 1}, format="json"
        )
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart item not found")

        res = attacker.delete(f"/api/cart/{owned_item.id}/")
        self.assertEqual(res.status_code, 404, res.data)

        owned_item.refresh_from_db()
        self.assertEqual(owned_item.quantity, 2)
        self.assertEqual(Cart.objects.count(), 2)

    def test_item_without_any_session_is_404(self):
        product = self.make_product()
        self.seed_session_cart([(product, 1)])
        item = CartItem.objects.get()

        sessionless = self.fresh_client()
        res = sessionless.patch(f"/api/cart/{item.id}/", {"quantity": 1}, format="json")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")

    def test_carts_are_separate_per_session(self):
        product = self.make_product()
        mine = self.seed_session_cart([(product, 1)])

        other = self.fresh_client()
        theirs = other.get("/api/cart/").data
        self.assertNotEqual(mine["id"], theirs["id"])
        self.assertEqual(theirs["items"], [])

    def test_model_string_representation(self):
        product = self.make_product()
        self.seed_session_cart([(product, 2)])
        item = CartItem.objects.get()
        self.assertEqual(str(item), f"{product.name} x 2")


@tag("cart")
class CartThrottleTests(ApiTestCase):
    def _add(self, product_id):
        return self.client.post(
            "/api/cart/",
            {"product_id": product_id, "quantity": 1},
            format="json",
        )

    def test_mutation_scopes_and_rates_are_configured(self):
        self.assertEqual(cart_detail.view_class.throttle_scope, "cart")
        self.assertEqual(
            cart_detail.view_class.throttle_classes,
            [CartMutationRateThrottle],
        )
        self.assertEqual(cart_item_detail.view_class.throttle_scope, "cart")
        self.assertIn(ScopedRateThrottle, cart_item_detail.view_class.throttle_classes)
        self.assertIn("cart", api_settings.DEFAULT_THROTTLE_RATES)

    def test_add_and_update_share_the_mutation_budget(self):
        """DRF binds THROTTLE_RATES at import, so the rate is patched on the throttle class."""
        product = self.make_product(stock=10)
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["cart"] = "2/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            self.assertEqual(self._add(product.id).status_code, 201)
            self.assertEqual(self._add(product.id).status_code, 201)
            item = CartItem.objects.get()
            throttled = self.client.patch(
                f"/api/cart/{item.id}/", {"quantity": 1}, format="json"
            )
        self.assertEqual(throttled.status_code, 429, throttled.data)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 2)

    def test_reads_do_not_consume_the_mutation_budget(self):
        product = self.make_product(stock=10)
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["cart"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            self.assertEqual(self._add(product.id).status_code, 201)
            self.assertEqual(self._add(product.id).status_code, 429)
            self.assertEqual(self.client.get("/api/cart/").status_code, 200)


@tag("cart")
class CartCouponStateTests(ApiTestCase):
    def setUp(self):
        self.buyer = self.make_user("buyer")
        self.api_login("buyer")
        self.product = self.make_product(price="500.00", stock=10)
        self.seed_session_cart([(self.product, 2)])

    def _apply(self, payload, client=None):
        return (client or self.client).post("/api/cart/coupon/", payload, format="json")

    def _remove(self, client=None):
        return (client or self.client).delete("/api/cart/coupon/")

    def _cart(self):
        return Cart.objects.get()

    def test_apply_persists_coupon_on_cart_and_surfaces_code(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10")

        res = self._apply({"code": "SAVE10"})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._cart().coupon_id, coupon.id)
        self.assertEqual(res.data["coupon_code"], "SAVE10")
        self.assertEqual(self.client.get("/api/cart/").data["coupon_code"], "SAVE10")

    def test_apply_matches_code_case_insensitively(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10")

        res = self._apply({"code": "save10"})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._cart().coupon_id, coupon.id)
        self.assertEqual(res.data["coupon_code"], "SAVE10")

    def test_apply_replaces_previous_coupon(self):
        first = self.make_coupon(code="SAVE10", discount_value="10")
        second = self.make_coupon(code="FIVEPC", discount_value="5")

        self._apply({"code": first.code})
        res = self._apply({"code": second.code})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._cart().coupon_id, second.id)

    def test_apply_rejections_share_the_uniform_envelope(self):
        cases = [
            ("unknown", "SAVE404"),
            ("inactive", self.make_coupon(code="DEAD", active=False).code),
            (
                "expired",
                self.make_coupon(
                    code="OLD", valid_until=timezone.now() - timedelta(minutes=1)
                ).code,
            ),
            (
                "not-yet-valid",
                self.make_coupon(
                    code="FUTURE", valid_from=timezone.now() + timedelta(days=1)
                ).code,
            ),
            (
                "usage-limit",
                self.make_coupon(code="MAXED", usage_limit=5, used_count=5).code,
            ),
            (
                "min-order",
                self.make_coupon(code="BIGSPEND", minimum_order_amount="5000").code,
            ),
        ]
        for name, code in cases:
            with self.subTest(case=name):
                res = self._apply({"code": code})
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(
                    res.data,
                    {
                        "error": "Invalid coupon code",
                        "code": "validation_error",
                        "details": {},
                    },
                )
        self.assertIsNone(self._cart().coupon_id)

    def test_apply_without_code_is_rejected(self):
        res = self._apply({})

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Coupon code is required")
        self.assertEqual(res.data["code"], "validation_error")
        self.assertIsNone(self._cart().coupon_id)

    def test_apply_without_cart_404s_uniformly(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        fresh = self.fresh_client()

        known = self._apply({"code": "SAVE10"}, client=fresh)
        unknown = self._apply({"code": "SAVE404"}, client=fresh)

        self.assertEqual(known.status_code, 404, known.data)
        self.assertEqual(known.data, unknown.data)
        self.assertEqual(
            known.data, {"error": "Cart not found", "code": "not_found", "details": {}}
        )

    def test_remove_clears_the_persisted_coupon(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        self._apply({"code": "SAVE10"})

        res = self._remove()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self._cart().coupon_id)
        self.assertIsNone(res.data["coupon_code"])

    def test_remove_without_coupon_applied_is_idempotent(self):
        res = self._remove()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(res.data["coupon_code"])
        self.assertIsNone(self._cart().coupon_id)

    def test_remove_without_cart_404s(self):
        res = self.fresh_client().delete("/api/cart/coupon/")

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")

    def test_apply_with_session_but_no_cart_row_404s(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        Cart.objects.all().delete()

        res = self._apply({"code": "SAVE10"})

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")

    def test_another_session_cannot_read_or_clear_my_coupon(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10")
        self._apply({"code": "SAVE10"})
        attacker = self.fresh_client()

        res = attacker.delete("/api/cart/coupon/")

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(self._cart().coupon_id, coupon.id)
        self.assertIsNone(attacker.get("/api/cart/").data["coupon_code"])

    def test_coupon_endpoints_share_the_cart_mutation_scope(self):
        self.assertEqual(cart_coupon.view_class.throttle_scope, "cart")
        self.assertIn(ScopedRateThrottle, cart_coupon.view_class.throttle_classes)
        self.assertIn("cart", api_settings.DEFAULT_THROTTLE_RATES)

    def test_apply_and_remove_share_one_mutation_budget(self):
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["cart"] = "1/min"
        self.make_coupon(code="SAVE10", discount_value="10")

        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            self.assertEqual(self._apply({"code": "SAVE10"}).status_code, 200)
            throttled = self._remove()

        self.assertEqual(throttled.status_code, 429, throttled.data)
        self.assertIsNotNone(self._cart().coupon_id)

    def test_legacy_and_v1_mounts_resolve_the_same_view(self):
        legacy = resolve("/api/cart/coupon/")
        versioned = resolve("/api/v1/store/cart/coupon/")

        self.assertEqual(legacy.func, versioned.func)

    def test_checkout_uses_the_persisted_coupon_when_no_code_is_posted(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        self._apply({"code": "SAVE10"})

        res = self.checkout()

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertEqual(order.coupon.code, "SAVE10")
        self.assertEqual(order.discount_amount, Decimal("100.00"))
        self.assertEqual(order.total_amount, Decimal("900.00"))

    def test_checkout_revalidates_a_coupon_invalidated_after_apply(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10")
        self._apply({"code": "SAVE10"})
        coupon.active = False
        coupon.save()

        res = self.checkout()

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "This coupon is inactive")
        self.assertFalse(Order.objects.exists())

    def test_explicit_checkout_code_wins_over_the_persisted_coupon(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        other = self.make_coupon(code="FIVEPC", discount_value="5")
        self._apply({"code": "SAVE10"})

        res = self.checkout(coupon_code=other.code)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Order.objects.get(id=res.data["id"]).coupon.code, "FIVEPC")

    def test_deleted_coupon_degrades_gracefully_at_checkout(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10")
        self._apply({"code": "SAVE10"})

        coupon.delete()

        self.assertIsNone(self._cart().coupon_id)
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertIsNone(order.coupon)
        self.assertEqual(order.total_amount, Decimal("1000.00"))


@tag("cart")
class NonObjectJsonBodyTests(ApiTestCase):
    """Bodies are sent as raw strings: the DRF client's format="json" path treats falsy payloads as no data."""

    RAW_BODIES = ("[]", "[1,2]", "42", '"hello"', "null", "true")

    def setUp(self):
        self.product = self.make_product(stock=5)
        self.seed_session_cart([(self.product, 1)])
        self.item = CartItem.objects.get()

    def _post(self, body, path="/api/cart/"):
        return self.client.post(path, body, content_type="application/json")

    def _patch(self, body):
        return self.client.patch(
            f"/api/cart/{self.item.id}/", body, content_type="application/json"
        )

    def _refuse_same_as_empty_object(self, post):
        expected = post("{}")
        self.assertEqual(expected.status_code, 400, expected.data)
        for body in self.RAW_BODIES:
            with self.subTest(body=body):
                res = post(body)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.content, expected.content)
                self.assertEqual(res.data, expected.data)

    def test_add_refuses_a_non_object_body_like_an_empty_object(self):
        self._refuse_same_as_empty_object(self._post)
        self.assertEqual(CartItem.objects.count(), 1)

    def test_patch_refuses_a_non_object_body_like_an_empty_object(self):
        self._refuse_same_as_empty_object(self._patch)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)

    def test_coupon_apply_refuses_a_non_object_body_like_an_empty_object(self):
        self._refuse_same_as_empty_object(
            lambda body: self._post(body, "/api/cart/coupon/")
        )
        self.assertIsNone(Cart.objects.get().coupon_id)


@tag("cart")
class CartOwnershipTests(ApiTestCase):
    def setUp(self):
        self.product = self.make_product(stock=10)
        self.buyer = self.make_user("buyer")

    def test_authenticated_request_gets_the_account_cart(self):
        _, token = self.api_login("buyer")

        res = self.client.get("/api/cart/")

        self.assertEqual(res.status_code, 200, res.data)
        cart = Cart.objects.get(id=res.data["id"])
        self.assertEqual(cart.user, self.buyer)
        self.assertIsNone(cart.session_id)
        self.assertFalse(Cart.objects.filter(session_id__isnull=False).exists())

    def test_anonymous_request_gets_the_session_cart(self):
        res = self.client.get("/api/cart/")

        self.assertEqual(res.status_code, 200, res.data)
        cart = Cart.objects.get(id=res.data["id"])
        self.assertIsNone(cart.user_id)
        self.assertEqual(cart.session_id, self.client.session.session_key)
        self.assertEqual(self.current_cart().id, cart.id)

    def test_two_users_never_share_a_cart(self):
        _, token = self.api_login("buyer")
        self.client.post(
            "/api/cart/", {"product_id": self.product.id, "quantity": 2}, format="json"
        )

        other = self.fresh_client()
        self.make_user("other")
        self.api_login("other", client=other)
        res = other.get("/api/cart/")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["items"], [])
        self.assertNotEqual(res.data["id"], Cart.objects.get(user=self.buyer).id)

    def test_one_cart_per_user_across_requests(self):
        _, token = self.api_login("buyer")

        first = self.client.get("/api/cart/").data
        second = self.client.get("/api/cart/").data

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(Cart.objects.filter(user=self.buyer).count(), 1)

    def test_the_unique_constraint_refuses_a_second_account_cart(self):
        Cart.objects.create(user=self.buyer, session_id=None)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Cart.objects.create(user=self.buyer, session_id=None)

    def test_a_guest_cart_is_unchanged_for_a_visitor_who_never_signs_in(self):
        first = self.client.get("/api/cart/").data
        self.client.post(
            "/api/cart/", {"product_id": self.product.id, "quantity": 1}, format="json"
        )
        second = self.client.get("/api/cart/").data

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(Cart.objects.filter(user__isnull=True).count(), 1)

    def test_an_account_cart_cannot_reach_a_guest_cart_line(self):
        self.seed_session_cart([(self.product, 1)])
        guest_item = CartItem.objects.get()
        login_client = self.fresh_client()
        _, token = self.api_login("buyer", client=login_client)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.client.get("/api/cart/")

        res = self.client.patch(
            f"/api/cart/{guest_item.id}/", {"quantity": 5}, format="json"
        )

        self.assertEqual(res.status_code, 404, res.data)
        guest_item.refresh_from_db()
        self.assertEqual(guest_item.quantity, 1)
        self.assertEqual(Cart.objects.filter(user=self.buyer).count(), 1)

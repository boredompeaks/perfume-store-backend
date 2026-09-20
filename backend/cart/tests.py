"""Cart unit tests - docs/test-gaps.md items 23-29."""
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import tag
from django.urls import resolve
from django.utils import timezone
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle

from cart.models import Cart, CartItem
from cart.views import CartMutationRateThrottle, cart_coupon, cart_detail, cart_item_detail
from common.testing import ApiTestCase
from orders.models import Order


@tag("cart")
class CartLazyCreationTests(ApiTestCase):
    # 23. GET creates session cart lazily ------------------------------------------------
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
            {"product_id": product_id if product_id is not None else self.product.id, "quantity": quantity},
            format="json",
        )

    # 24. new row created with correct quantity -----------------------------------------
    def test_add_item_creates_row_with_correct_quantity(self):
        res = self._add(2)

        self.assertEqual(res.status_code, 201, res.data)
        item = CartItem.objects.get()
        self.assertEqual(item.cart, Cart.objects.get())
        self.assertEqual(item.product, self.product)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(len(res.data["items"]), 1)
        self.assertEqual(res.data["items"][0]["quantity"], 2)
        # nested product representation gives the client everything it needs
        self.assertEqual(res.data["items"][0]["product"]["id"], self.product.id)
        self.assertEqual(res.data["items"][0]["product"]["name"], self.product.name)

    def test_add_item_defaults_to_quantity_one(self):
        res = self.client.post("/api/cart/", {"product_id": self.product.id}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(CartItem.objects.get().quantity, 1)

    # 25. existing row quantity accumulates ------------------------------------------------
    def test_add_same_product_accumulates_quantity(self):
        self.assertEqual(self._add(1).status_code, 201)
        res = self._add(2)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual(CartItem.objects.get().quantity, 3)
        self.assertEqual(res.data["items"][0]["quantity"], 3)

    # 26. quantity > stock -> 400 (new and accumulate paths) ---------------------------------
    def test_overstock_rejected_on_new_row(self):
        res = self._add(6)  # stock is 5
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Not enough stock")
        self.assertEqual(CartItem.objects.count(), 0)

    def test_overstock_rejected_on_accumulate(self):
        self.assertEqual(self._add(3).status_code, 201)
        res = self._add(3)  # 3 + 3 > stock 5

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Not enough stock")
        self.assertEqual(CartItem.objects.get().quantity, 3)  # untouched

    def test_add_item_validation_errors(self):
        cases = [
            ({}, 400, "product_id is required"),
            ({"quantity": 1}, 400, "product_id is required"),
            ({"product_id": 999999}, 404, "Product not found"),
            ({"product_id": "not-a-number"}, 404, "Product not found"),
            ({"product_id": self.product.id, "quantity": "abc"}, 400, "Quantity must be a number"),
            ({"product_id": self.product.id, "quantity": 0}, 400, "Quantity must be greater than 0"),
            ({"product_id": self.product.id, "quantity": -2}, 400, "Quantity must be greater than 0"),
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

    # 27. 0/negative/non-numeric -> 400; > stock -> 400 -------------------------------------
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

    # 28. removed; response shape checked (V-09 flip test below) ---------------------------
    def test_delete_removes_only_target_item(self):
        item = CartItem.objects.get(product=self.product)
        res = self.client.delete(f"/api/cart/{item.id}/")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(CartItem.objects.count(), 1)
        self.assertEqual([row["product"]["id"] for row in res.data["items"]], [self.other.id])

    def test_delete_unknown_item_returns_404(self):
        res = self.client.delete("/api/cart/999999/")
        self.assertEqual(res.status_code, 404, res.data)

    @unittest.expectedFailure
    def test_v09_cart_payload_excludes_session_id(self):
        """V-09/F-15: the cart response leaks the raw session id (the client
        already owns the cookie; the token adds nothing but risk). Flip when
        `session_id` is dropped from CartSerializer."""
        res = self.client.get("/api/cart/")
        self.assertNotIn("session_id", res.data)


@tag("cart")
class CrossSessionIsolationTests(ApiTestCase):
    # 29. item from another session -> 404 ----------------------------------------------------
    def test_item_from_another_session_is_404(self):
        owner_product = self.make_product()
        self.seed_session_cart([(owner_product, 2)])
        owned_item = CartItem.objects.get()

        attacker = self.fresh_client()
        self.assertEqual(attacker.get("/api/cart/").status_code, 200)  # own session/cart

        res = attacker.patch(f"/api/cart/{owned_item.id}/", {"quantity": 1}, format="json")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart item not found")

        res = attacker.delete(f"/api/cart/{owned_item.id}/")
        self.assertEqual(res.status_code, 404, res.data)

        owned_item.refresh_from_db()
        self.assertEqual(owned_item.quantity, 2)  # untouched
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
    """Conventions: every public mutating endpoint gets a throttle scope.
    Cart add/update/remove share the 'cart' scope; GET is exempt so browsing
    never consumes the mutation budget."""

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
        """Third mutation inside a 2/min budget is 429'd — here the PATCH,
        proving update is throttled under the same scope. DRF binds
        THROTTLE_RATES at import, so the rate is patched on the throttle
        class rather than via override_settings."""
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
        self.assertEqual(item.quantity, 2)  # the throttled PATCH never landed

    def test_reads_do_not_consume_the_mutation_budget(self):
        product = self.make_product(stock=10)
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["cart"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            self.assertEqual(self._add(product.id).status_code, 201)
            self.assertEqual(self._add(product.id).status_code, 429)  # budget spent
            # GET stays available even with the mutation bucket exhausted
            self.assertEqual(self.client.get("/api/cart/").status_code, 200)


@tag("cart")
class CartCouponStateTests(ApiTestCase):
    """R-9.3.5/R-9.3.6: POST/DELETE /api/cart/coupon/ manage the coupon as
    persistent cart state (a nullable FK on Cart), so the applied coupon
    survives across requests and is re-validated by checkout's pre-existing
    coupon path when the payload posts no explicit code."""

    def setUp(self):
        self.buyer = self.make_user("buyer")
        self.api_login("buyer")
        self.product = self.make_product(price="500.00", stock=10)
        self.seed_session_cart([(self.product, 2)])  # subtotal 1000.00

    def _apply(self, payload, client=None):
        return (client or self.client).post("/api/cart/coupon/", payload, format="json")

    def _remove(self, client=None):
        return (client or self.client).delete("/api/cart/coupon/")

    def _cart(self):
        return Cart.objects.get()

    # -- apply: persistence + serializer exposure ---------------------------
    def test_apply_persists_coupon_on_cart_and_surfaces_code(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10")

        res = self._apply({"code": "SAVE10"})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._cart().coupon_id, coupon.id)
        # The applied code rides every cart representation so the frontend
        # can render it without a second lookup (R-9.3.5).
        self.assertEqual(res.data["coupon_code"], "SAVE10")
        self.assertEqual(self.client.get("/api/cart/").data["coupon_code"], "SAVE10")

    def test_apply_matches_code_case_insensitively(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10")

        res = self._apply({"code": "save10"})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._cart().coupon_id, coupon.id)
        self.assertEqual(res.data["coupon_code"], "SAVE10")  # canonical form

    def test_apply_replaces_previous_coupon(self):
        first = self.make_coupon(code="SAVE10", discount_value="10")
        second = self.make_coupon(code="FIVEPC", discount_value="5")

        self._apply({"code": first.code})
        res = self._apply({"code": second.code})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._cart().coupon_id, second.id)

    # -- apply: rejections (delegated to the shared preview gate, V-11) -----
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
                # One body for every failure reason (SPEC-9-03 envelope on
                # the preview's uniform rejection): no reason leaks.
                self.assertEqual(
                    res.data,
                    {
                        "error": "Invalid coupon code",
                        "code": "validation_error",
                        "details": {},
                    },
                )
        self.assertIsNone(self._cart().coupon_id)  # nothing persisted on rejection

    def test_apply_without_code_is_rejected(self):
        res = self._apply({})

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Coupon code is required")
        self.assertEqual(res.data["code"], "validation_error")
        self.assertIsNone(self._cart().coupon_id)

    def test_apply_without_cart_404s_uniformly(self):
        """No existence leak: with no cart, a known-valid and an unknown code
        get the identical 404 (orders.apply_coupon precedent)."""
        self.make_coupon(code="SAVE10", discount_value="10")
        fresh = self.fresh_client()

        known = self._apply({"code": "SAVE10"}, client=fresh)
        unknown = self._apply({"code": "SAVE404"}, client=fresh)

        self.assertEqual(known.status_code, 404, known.data)
        self.assertEqual(known.data, unknown.data)
        self.assertEqual(
            known.data, {"error": "Cart not found", "code": "not_found", "details": {}}
        )

    # -- remove: idempotent --------------------------------------------------
    def test_remove_clears_the_persisted_coupon(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        self._apply({"code": "SAVE10"})

        res = self._remove()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self._cart().coupon_id)
        self.assertIsNone(res.data["coupon_code"])

    def test_remove_without_coupon_applied_is_idempotent(self):
        """Removing when nothing is applied still succeeds: the caller's end
        state (no coupon) already holds, so no error and no 404 — the
        response is the cart-family 200 with the cart body."""
        res = self._remove()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(res.data["coupon_code"])
        self.assertIsNone(self._cart().coupon_id)

    def test_remove_without_cart_404s(self):
        res = self.fresh_client().delete("/api/cart/coupon/")

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")

    def test_apply_with_session_but_no_cart_row_404s(self):
        """A session without a cart row is the same 404 as no session at
        all: apply never lazily creates a cart to attach a coupon to."""
        self.make_coupon(code="SAVE10", discount_value="10")
        Cart.objects.all().delete()

        res = self._apply({"code": "SAVE10"})

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")

    # -- isolation: session-scoped authority (the cart family's permission
    #    dimension — no token can reach another session's cart state) -------
    def test_another_session_cannot_read_or_clear_my_coupon(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10")
        self._apply({"code": "SAVE10"})
        attacker = self.fresh_client()

        res = attacker.delete("/api/cart/coupon/")

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(self._cart().coupon_id, coupon.id)  # untouched
        self.assertIsNone(attacker.get("/api/cart/").data["coupon_code"])

    # -- throttle: conventions require a scope on public mutating endpoints --
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
        # the throttled remove never landed
        self.assertIsNotNone(self._cart().coupon_id)

    # -- dual mount (SPEC-9-02): one urlconf serves both route families ------
    def test_legacy_and_v1_mounts_resolve_the_same_view(self):
        legacy = resolve("/api/cart/coupon/")
        versioned = resolve("/api/v1/store/cart/coupon/")

        self.assertEqual(legacy.func, versioned.func)

    # -- checkout re-validation composition -----------------------------------
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
        """The persisted reference never bypasses the rules: state changed
        since apply, so checkout rejects through its pre-existing coupon
        path instead of silently riding the FK into the order."""
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
        """SET_NULL: deleting the coupon row drops it from the cart instead
        of stranding checkout with a dangling reference."""
        coupon = self.make_coupon(code="SAVE10", discount_value="10")
        self._apply({"code": "SAVE10"})

        coupon.delete()

        self.assertIsNone(self._cart().coupon_id)
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertIsNone(order.coupon)
        self.assertEqual(order.total_amount, Decimal("1000.00"))

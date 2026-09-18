"""Cart unit tests - docs/test-gaps.md items 23-29."""
import unittest

from django.test import tag

from cart.models import Cart, CartItem
from common.testing import ApiTestCase


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

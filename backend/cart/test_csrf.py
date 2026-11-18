from django.conf import settings
from django.contrib.auth.models import User
from django.test import tag
from rest_framework.test import APIClient

from cart.models import CartItem
from common.testing import ApiTestCase


def enforcing_client():
    """A client that does NOT suppress CSRF checks (Django's recipe)."""
    return APIClient(enforce_csrf_checks=True)


def csrf_token(client):
    return client.cookies[settings.CSRF_COOKIE_NAME].value


@tag("cart")
class CSRFCookieIssuanceTests(ApiTestCase):
    def test_cart_get_issues_the_csrf_cookie(self):
        client = enforcing_client()

        res = client.get("/api/cart/")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(settings.CSRF_COOKIE_NAME, client.cookies)


@tag("cart")
class GuestCartCSRFEnforcementTests(ApiTestCase):
    def setUp(self):
        self.product = self.make_product(stock=5)
        self.client = enforcing_client()
        res = self.client.get("/api/cart/")
        self.assertEqual(res.status_code, 200, res.data)
        self.token = csrf_token(self.client)

    def test_add_with_session_cookie_and_no_token_is_403(self):
        res = self.client.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": 1},
            format="json",
        )

        self.assertEqual(res.status_code, 403)
        self.assertIn("CSRF", str(res.data["error"]))
        self.assertFalse(CartItem.objects.exists())

    def test_add_with_valid_token_is_201(self):
        res = self.client.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": 1},
            format="json",
            HTTP_X_CSRFTOKEN=self.token,
        )

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(len(res.data["items"]), 1)

    def test_add_with_mismatched_token_is_403(self):
        res = self.client.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": 1},
            format="json",
            HTTP_X_CSRFTOKEN="forged-value",
        )

        self.assertEqual(res.status_code, 403)

    def test_quantity_update_and_remove_are_enforced(self):
        seeded = self.client.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": 1},
            format="json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        item_id = seeded.data["items"][0]["id"]

        patched = self.client.patch(
            f"/api/cart/{item_id}/",
            {"quantity": 2},
            format="json",
        )
        self.assertEqual(patched.status_code, 403)

        patched = self.client.patch(
            f"/api/cart/{item_id}/",
            {"quantity": 2},
            format="json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(patched.status_code, 200, patched.data)

        deleted = self.client.delete(f"/api/cart/{item_id}/")
        self.assertEqual(deleted.status_code, 403)

        deleted = self.client.delete(
            f"/api/cart/{item_id}/", HTTP_X_CSRFTOKEN=self.token
        )
        self.assertEqual(deleted.status_code, 200, deleted.data)

    def test_apply_coupon_orders_family_is_enforced(self):
        self.make_coupon(code="SAVE10")
        self.client.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": 1},
            format="json",
            HTTP_X_CSRFTOKEN=self.token,
        )

        rejected = self.client.post(
            "/api/orders/apply-coupon/",
            {"code": "SAVE10"},
            format="json",
        )
        self.assertEqual(rejected.status_code, 403)

        accepted = self.client.post(
            "/api/orders/apply-coupon/",
            {"code": "SAVE10"},
            format="json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(accepted.status_code, 200, accepted.data)

    def test_fresh_visitor_first_mutation_is_not_gated(self):
        """A brand-new visitor has no auth-bearing cookie to forge; the cart GET then issues the token pair."""
        fresh = enforcing_client()

        res = fresh.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": 1},
            format="json",
        )

        self.assertEqual(res.status_code, 201, res.data)

    def test_django_session_authenticated_user_is_enforced(self):
        self.make_user()
        client = enforcing_client()
        client.force_login(User.objects.get(username="buyer"))
        res = client.get("/api/cart/")
        self.assertEqual(res.status_code, 200, res.data)
        token = csrf_token(client)

        rejected = client.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": 1},
            format="json",
        )
        self.assertEqual(rejected.status_code, 403)

        accepted = client.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": 1},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(accepted.status_code, 201, accepted.data)


@tag("cart")
class JWTAndAuthFlowInterplayTests(ApiTestCase):
    def setUp(self):
        self.buyer = self.make_user()
        self.product = self.make_product(stock=5)
        self.client = enforcing_client()

    def test_jwt_authenticated_mutation_is_unaffected(self):
        _, _ = self.api_login(client=self.client)
        res = self.client.get("/api/cart/")
        self.assertEqual(res.status_code, 200, res.data)
        token = csrf_token(self.client)
        res = self.client.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": 2},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(res.status_code, 201, res.data)

        res = self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(),
            format="json",
        )

        self.assertEqual(res.status_code, 201, res.data)

    def test_login_refresh_logout_work_with_session_cookie_present(self):
        res = self.client.get("/api/cart/")
        self.assertEqual(res.status_code, 200, res.data)
        token = csrf_token(self.client)

        res = self.client.post(
            "/api/accounts/login/",
            {"username": "buyer", "password": "S3cure-Passphrase!"},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn("refresh", res.data)

        res = self.client.post(
            "/api/accounts/token/refresh/",
            {},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['access']}")
        res = self.client.post(
            "/api/accounts/logout/",
            {},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(res.status_code, 200, res.data)

    def test_login_without_session_cookie_needs_no_token(self):
        res = self.client.post(
            "/api/accounts/login/",
            {"username": "buyer", "password": "S3cure-Passphrase!"},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)

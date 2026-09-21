"""SPEC-17-03 [R-17.18]: CSRF enforcement on session-cookie cart mutations.

The live-confirmed exploit (BACKEND_REQUESTS.md [P1]) was: POST/DELETE on
/api/cart/ with only the session cookie and no X-CSRFToken returned
201/200. These tests pin the fix end-to-end and hermetically, following
Django's documented CSRF test recipe:

- the enforcing client is built with ``enforce_csrf_checks=True`` — the
  default test client sets ``_dont_enforce_csrf_checks``, which the
  CSRFCheck DRF's authentication runs inherits and honours, so the rest of
  the suite is unaffected by the new gate;
- the token pair is acquired through the API itself: GET /api/cart/ issues
  the ``csrftoken`` cookie (ensure_csrf_cookie), the mutation replays it
  as ``X-CSRFToken`` — exactly the SPA's flow (api.ts reads the cookie,
  sends the header).

The JWT interplay (refresh cookie flow from SPEC-17-02, checkout) is
pinned here too: cookie+JWT endpoints keep working under the gate.
"""
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
    """The cookie value the SPA would read and replay as X-CSRFToken."""
    return client.cookies[settings.CSRF_COOKIE_NAME].value


@tag("cart")
class CSRFCookieIssuanceTests(ApiTestCase):
    def test_cart_get_issues_the_csrf_cookie(self):
        client = enforcing_client()

        res = client.get("/api/cart/")

        self.assertEqual(res.status_code, 200, res.data)
        # ensure_csrf_cookie on the boot surface: the browser holds the
        # double-submit cookie before the first gated mutation.
        self.assertIn(settings.CSRF_COOKIE_NAME, client.cookies)


@tag("cart")
class GuestCartCSRFEnforcementTests(ApiTestCase):
    """The live-confirmed surface: guest (anonymous) session cart mutations."""

    def setUp(self):
        self.product = self.make_product(stock=5)
        self.client = enforcing_client()
        # Build the guest session + cart through the API: this sets both
        # the sessionid and the csrftoken cookies, like a real visitor.
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
        # Rejection must also mean no cart mutation happened.
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
        """The global authentication chain covers the orders mount without
        touching orders/views.py: apply-coupon is a session-cookie POST."""
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
        """A brand-new visitor has no session cookie — no auth-bearing
        cookie rides the request, so there is nothing to forge; the cart
        GET then issues the token pair for every later mutation."""
        fresh = enforcing_client()

        res = fresh.post(
            "/api/cart/",
            {"product_id": self.product.id, "quantity": 1},
            format="json",
        )

        self.assertEqual(res.status_code, 201, res.data)

    def test_django_session_authenticated_user_is_enforced(self):
        """The stock half of the gate: a Django-session-authenticated user
        (request.user active) is held to the same standard as the guest —
        tokenless unsafe request 403s, the replayed pair passes."""
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
    """The gate must not add CSRF friction to credential flows that are
    not exploitable: JWT-bearer mutations (Authorization headers cannot be
    attached cross-site) and the SPEC-17-02 cookie refresh flow."""

    def setUp(self):
        self.buyer = self.make_user()
        self.product = self.make_product(stock=5)
        self.client = enforcing_client()

    def test_jwt_authenticated_mutation_is_unaffected(self):
        # Login first (fresh client, no session cookie yet — ungated), then
        # build the session cart: the checkout request afterwards carries
        # BOTH the session cookie and the JWT with no CSRF token.
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
        """A guest who carted before logging in sends sessionid on every
        accounts call; with the SPA's token header the 17-02 flows survive
        the gate (the SPA reads the same cookie pair for all mutations)."""
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
        self.assertNotIn("refresh", res.data)  # R-17.12: cookie, not body

        res = self.client.post(
            "/api/accounts/token/refresh/", {}, format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['access']}"
        )
        res = self.client.post(
            "/api/accounts/logout/", {}, format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(res.status_code, 200, res.data)

    def test_login_without_session_cookie_needs_no_token(self):
        """The fresh-visitor login path (no cart ever built in this
        browser): no session cookie, no gate — the SPA's first login works
        before any cart activity has issued a token."""
        res = self.client.post(
            "/api/accounts/login/",
            {"username": "buyer", "password": "S3cure-Passphrase!"},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)

from django.conf import settings
from django.test import SimpleTestCase
from rest_framework.test import APIClient

from common.testing import ApiTestCase


class CsrfStackWiringTests(SimpleTestCase):
    def test_csrf_gate_installed_after_jwt(self):
        default_auth = settings.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]
        self.assertIn("django.middleware.csrf.CsrfViewMiddleware", settings.MIDDLEWARE)
        self.assertEqual(
            [cls.split(".")[-1] for cls in default_auth],
            ["JWTAuthentication", "SessionCartCSRFAuthentication"],
            "DEFAULT_AUTHENTICATION_CLASSES changed: the CSRF gate's "
            "position is load-bearing (JWT first short-circuits bearer "
            "requests away from the gate; the gate second covers session "
            "cookies) -- review and update the CSRF pins in this module "
            "in the same commit.",
        )

    def test_session_cookie_samesite_default_is_explicit_config(self):
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")


class SessionCartCsrfEnforcementTests(ApiTestCase):
    def _enforcing_client_with_token(self):
        client = APIClient(enforce_csrf_checks=True)
        res = client.get("/api/cart/")
        self.assertEqual(res.status_code, 200, res.data)
        return client, client.cookies[settings.CSRF_COOKIE_NAME].value

    def test_cart_get_issues_the_csrf_cookie(self):
        client = APIClient(enforce_csrf_checks=True)
        res = client.get("/api/cart/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(settings.CSRF_COOKIE_NAME, client.cookies)

    def test_cart_add_with_session_cookie_and_no_csrf_token_is_403(self):
        product = self.make_product()
        client, _token = self._enforcing_client_with_token()
        res = client.post(
            "/api/cart/",
            {"product_id": product.id, "quantity": 2},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_cart_add_with_valid_token_succeeds(self):
        product = self.make_product()
        client, token = self._enforcing_client_with_token()
        res = client.post(
            "/api/cart/",
            {"product_id": product.id, "quantity": 2},
            format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(len(res.data["items"]), 1)

    def test_default_client_succeeds_because_checks_are_suppressed(self):
        """Two clients in identical cookie state differing only in the suppression flag: the enforcing one is 403'd, the suite-default one gets 201."""
        product = self.make_product()

        enforced = APIClient(enforce_csrf_checks=True)
        self.assertEqual(enforced.get("/api/cart/").status_code, 200)
        res = enforced.post(
            "/api/cart/",
            {"product_id": product.id, "quantity": 1},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

        suppressed = APIClient(enforce_csrf_checks=False)
        self.assertEqual(suppressed.get("/api/cart/").status_code, 200)
        res = suppressed.post(
            "/api/cart/",
            {"product_id": product.id, "quantity": 1},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)


class CsrfMiddlewareDifferentialTests(ApiTestCase):
    def test_tokenless_post_to_non_exempt_view_is_rejected(self):
        enforced = APIClient(enforce_csrf_checks=True)
        res = enforced.post(
            "/admin/login/",
            {"username": "nobody", "password": "wrong"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)


class TokenAuthMutationCsrfTruthTests(ApiTestCase):
    def test_checkout_with_jwt_and_no_csrf_token_succeeds(self):
        product = self.make_product()
        self.make_user()
        _, token = self.api_login()
        self.assertTrue(token)
        self.seed_session_cart([(product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)

    def test_apply_coupon_with_jwt_and_no_csrf_token_succeeds(self):
        product = self.make_product(price="200.00")
        self.make_coupon(code="CSRF10", discount_type="percentage", discount_value="10")
        self.make_user()
        _, token = self.api_login()
        self.assertTrue(token)
        self.seed_session_cart([(product, 1)])
        res = self.client.post(
            "/api/orders/apply-coupon/", {"code": "CSRF10"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["coupon"], "CSRF10")


class ApiXssOutputSafetyTests(ApiTestCase):
    """User content comes back verbatim-as-stored and JSON-encoded; entity-encoding here would corrupt non-HTML consumers and mask the frontend's job."""

    SCRIPT = "<script>alert('xss')</script>"
    META = "<meta http-equiv=refresh content='0;url=javascript:alert(1)'>"

    def test_xss_payloads_in_product_content_return_verbatim_as_json(self):
        product = self.make_product(
            name=f"Rose {self.SCRIPT}",
            description=f"Notes of {self.META} over musk.",
        )
        detail = self.client.get(f"/api/products/{product.slug}/")
        listing = self.client.get("/api/products/")
        for res in (detail, listing):
            self.assertEqual(res.status_code, 200, res.data)
            self.assertTrue(res["Content-Type"].startswith("application/json"))
        self.assertEqual(detail.data["name"], f"Rose {self.SCRIPT}")
        self.assertEqual(detail.data["description"], f"Notes of {self.META} over musk.")
        stored = {row["id"]: row for row in listing.data["results"]}[product.id]
        self.assertEqual(stored["name"], f"Rose {self.SCRIPT}")
        self.assertIn(self.SCRIPT, detail.content.decode())

    def test_payload_flows_through_nested_cart_serializer_verbatim(self):
        product = self.make_product(name=self.SCRIPT)
        cart = self.seed_session_cart([(product, 1)])
        self.assertEqual(cart["items"][0]["product"]["name"], self.SCRIPT)

    def test_xss_payloads_in_checkout_user_content_return_verbatim_as_json(self):
        product = self.make_product()
        self.make_user()
        _, token = self.api_login()
        self.assertTrue(token)
        self.seed_session_cart([(product, 1)])
        name = "<img src=x onerror=alert(1)>"
        address = f"12 {self.SCRIPT} Lane"
        res = self.checkout(full_name=name, address=address)
        self.assertEqual(res.status_code, 201, res.data)
        listing = self.client.get("/api/orders/")
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertTrue(listing["Content-Type"].startswith("application/json"))
        order = listing.data["results"][0]
        self.assertEqual(order["full_name"], name)
        self.assertEqual(order["address"], address)

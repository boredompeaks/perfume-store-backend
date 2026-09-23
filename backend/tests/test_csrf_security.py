"""[R-21.1.4] Security test layer: CSRF-enforcement truth + API XSS-output
safety (SPEC-21-2).

CSRF half -- the SPEC-17-03 truth, pinned deliberately (the original pins
documented the pre-17-03 gap and carried the flip instruction; they were
inverted in the same commit that closed the gap):

Django's CsrfViewMiddleware IS installed (config/settings.py MIDDLEWARE), but
DRF wraps every view it serves -- @api_view-generated and class-based alike
-- in csrf_exempt (rest_framework.views.APIView.as_view: "session based
authentication is explicitly CSRF validated, all other authentication is CSRF
exempt"), and DRF re-enforces CSRF only inside
SessionAuthentication.enforce_csrf. SPEC-17-03 added
common.authentication.SessionCartCSRFAuthentication to
DEFAULT_AUTHENTICATION_CLASSES AFTER JWTAuthentication (config/settings.py),
extending the stock hook to the guest surface the stock class skips: every
request that presents the session cookie (named user or anonymous guest
cart) is CSRF-checked on unsafe methods, while JWT-bearer requests
short-circuit the chain and gain no friction (an Authorization header cannot
be attached cross-site). Net truth:

- Session-cookie cart mutations (POST/PATCH/DELETE on /api/cart/*, and the
  session-cookie apply-coupon in the orders family) are CSRF-enforced: a
  tokenless cross-site request that can attach the session cookie is 403'd
  by the gate, not just by browser SameSite trust.
- The csrftoken cookie is issued by GET /api/cart/ (ensure_csrf_cookie) --
  the SPA's boot call -- and replayed as X-CSRFToken by frontend/src/lib.
- JWT-authenticated money paths (checkout, apply-coupon with a bearer) are
  not CSRF-gated: the JWT is not an ambient credential, so there is nothing
  for a cross-site request to forge.

XSS half -- the API-boundary contract (frontend sink tests are out of scope
here: no component-test infra yet, and the JSON-LD sink at
frontend/src/app/products/[slug]/page.tsx is SPEC-17-06 / S21-4 territory):

The API never interpolates user content into HTML server-side (grep-verified:
the only render() surfaces are the staff-gated admin/ops templates -- Django
template auto-escaping, no mark_safe of user content -- and .txt email
templates). Pinned at the boundary: script/meta/img payloads in product and
checkout content come back verbatim-as-stored through the JSON serializers
with Content-Type application/json. The API emits data, never markup; output
encoding is the consumer's job (React escaping).
"""

from django.conf import settings
from django.test import SimpleTestCase
from rest_framework.test import APIClient

from common.testing import ApiTestCase


class CsrfStackWiringTests(SimpleTestCase):
    """The load-bearing wiring, pinned so the SPEC-17-03 gate cannot be
    removed silently: changing the chain must update this module in the
    same commit."""

    def test_csrf_gate_installed_after_jwt(self):
        default_auth = settings.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]
        self.assertIn(
            "django.middleware.csrf.CsrfViewMiddleware", settings.MIDDLEWARE
        )
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
        """'Lax' is now an explicit, env-driven default (no longer an
        implicit Django default leaned on as a mitigation): the server-side
        gate is the enforcement, SameSite is the same-site belt. If a
        deployment hardens to 'Strict', set SESSION_COOKIE_SAMESITE; the
        default only changes with an update to this pin."""
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")


class SessionCartCsrfEnforcementTests(ApiTestCase):
    """Cart endpoints key on the session cookie alone (no JWT involved).
    Pins the SPEC-17-03 truth: these mutations ARE CSRF-enforced -- the
    exact surface BACKEND_REQUESTS.md live-confirmed exploitable."""

    def _enforcing_client_with_token(self):
        """Django's hermetic CSRF recipe: enforce_csrf_checks=True plus the
        token pair acquired through the API itself (GET issues the cookie,
        the mutation replays it as the header)."""
        client = APIClient(enforce_csrf_checks=True)
        res = client.get("/api/cart/")
        self.assertEqual(res.status_code, 200, res.data)
        return client, client.cookies[settings.CSRF_COOKIE_NAME].value

    def test_cart_get_issues_the_csrf_cookie(self):
        """[Pin, flipped] The API DOES issue a CSRF token now, on the SPA's
        boot surface: GET /api/cart/ sets the csrftoken cookie the frontend
        replays as X-CSRFToken."""
        client = APIClient(enforce_csrf_checks=True)
        res = client.get("/api/cart/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(settings.CSRF_COOKIE_NAME, client.cookies)

    def test_cart_add_with_session_cookie_and_no_csrf_token_is_403(self):
        """[Pin, flipped] POST /api/cart/ with only the session cookie, no
        X-CSRFToken -> 403 (was the live-confirmed 201 in
        BACKEND_REQUESTS.md; SPEC-17-03 closed it)."""
        product = self.make_product()
        client, _token = self._enforcing_client_with_token()
        res = client.post(
            "/api/cart/",
            {"product_id": product.id, "quantity": 2},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_cart_add_with_valid_token_succeeds(self):
        """[Pin, flipped] The legitimate client -- cookie pair replayed as
        the header, exactly the SPA's flow -- still gets its 201."""
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
        """[Pin + differential] Two clients in identical cookie state
        (sessionid + csrftoken from a cart GET), differing only in the
        suppression flag: the enforcing one is 403'd, the suppressed one
        (the suite default, _dont_enforce_csrf_checks) gets 201. The gate
        rides the DRF authentication hook and honours the same suppression
        flag Django's middleware does -- so the suite's 2xx pins are the
        test-client artifact, and the enforcing pins above prove the
        server-side check itself."""
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
    """Proves the cart pins above mean 'exempt', not 'middleware absent':
    the same stack still rejects tokenless unsafe requests against views
    that are not csrf_exempt (the Django admin login)."""

    def test_tokenless_post_to_non_exempt_view_is_rejected(self):
        enforced = APIClient(enforce_csrf_checks=True)
        res = enforced.post(
            "/admin/login/",
            {"username": "nobody", "password": "wrong"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)


class TokenAuthMutationCsrfTruthTests(ApiTestCase):
    """JWT-bearer requests short-circuit the authentication chain before
    the CSRF gate (first successful authenticator wins), and an
    Authorization header is not an ambient credential -- a cross-site
    request cannot attach it -- so the money paths named in
    BACKEND_REQUESTS.md answer 2xx with no CSRF token by design. Pins that
    the gate adds no friction to bearer-authenticated flows."""

    def test_checkout_with_jwt_and_no_csrf_token_succeeds(self):
        """[Pin] POST /api/orders/checkout/ -- session cart + JWT, no
        X-CSRFToken -> 201. The gate sits AFTER JWTAuthentication in
        DEFAULT_AUTHENTICATION_CLASSES, so this request never reaches it;
        the session cookie checkout requires rides along but the credential
        that authorizes the order is the non-ambient JWT."""
        product = self.make_product()
        self.seed_session_cart([(product, 1)])
        self.make_user()
        _, token = self.api_login()
        self.assertTrue(token)
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)

    def test_apply_coupon_with_jwt_and_no_csrf_token_succeeds(self):
        """[Pin] POST /api/orders/apply-coupon/ with a bearer -- no
        X-CSRFToken -> 200 preview. Same JWT-first truth as checkout: the
        tokenless session-cookie variant is the one the gate rejects (see
        SessionCartCsrfEnforcementTests and cart/test_csrf.py for the
        guest surface)."""
        product = self.make_product(price="200.00")
        self.seed_session_cart([(product, 1)])
        self.make_coupon(
            code="CSRF10", discount_type="percentage", discount_value="10"
        )
        self.make_user()
        _, token = self.api_login()
        self.assertTrue(token)
        res = self.client.post(
            "/api/orders/apply-coupon/", {"code": "CSRF10"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["coupon"], "CSRF10")


class ApiXssOutputSafetyTests(ApiTestCase):
    """[R-21.1.4] XSS at the API boundary: user-controlled content comes
    back verbatim-as-stored, JSON-encoded, with Content-Type
    application/json. The API never serves user content as HTML; output
    encoding belongs to the consumer (React escaping). Verbatim is the
    contract -- deliberately NOT entity-encoded here: the body is data for a
    JSON parser, and HTML-flavoured rewriting on the API side would corrupt
    non-HTML consumers and mask the frontend's job."""

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
            # JSON transport, never HTML: a browser must not parse this
            # response as markup regardless of payload.
            self.assertTrue(res["Content-Type"].startswith("application/json"))
        self.assertEqual(detail.data["name"], f"Rose {self.SCRIPT}")
        self.assertEqual(
            detail.data["description"], f"Notes of {self.META} over musk."
        )
        stored = {row["id"]: row for row in listing.data["results"]}[product.id]
        self.assertEqual(stored["name"], f"Rose {self.SCRIPT}")
        # Raw-body pin: the payload rides as literal JSON string data -- no
        # server-side HTML escaping or interpolation happened anywhere.
        self.assertIn(self.SCRIPT, detail.content.decode())

    def test_payload_flows_through_nested_cart_serializer_verbatim(self):
        product = self.make_product(name=self.SCRIPT)
        cart = self.seed_session_cart([(product, 1)])
        self.assertEqual(cart["items"][0]["product"]["name"], self.SCRIPT)

    def test_xss_payloads_in_checkout_user_content_return_verbatim_as_json(self):
        product = self.make_product()
        self.seed_session_cart([(product, 1)])
        self.make_user()
        _, token = self.api_login()
        self.assertTrue(token)
        name = "<img src=x onerror=alert(1)>"
        address = f"12 {self.SCRIPT} Lane"
        res = self.checkout(full_name=name, address=address)
        self.assertEqual(res.status_code, 201, res.data)
        listing = self.client.get("/api/orders/")
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertTrue(
            listing["Content-Type"].startswith("application/json")
        )
        order = listing.data["results"][0]
        self.assertEqual(order["full_name"], name)
        self.assertEqual(order["address"], address)

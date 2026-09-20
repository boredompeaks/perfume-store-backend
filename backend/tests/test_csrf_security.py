"""[R-21.1.4] Security test layer: CSRF-enforcement truth + API XSS-output
safety (SPEC-21-2).

CSRF half -- today's truth, pinned deliberately (SPEC-17-03 owns the change):

Django's CsrfViewMiddleware IS installed (config/settings.py MIDDLEWARE), but
DRF wraps every view it serves -- @api_view-generated and class-based alike
-- in csrf_exempt (rest_framework.views.APIView.as_view: "session based
authentication is explicitly CSRF validated, all other authentication is CSRF
exempt"), and DRF re-enforces CSRF only inside
SessionAuthentication.enforce_csrf. REST_FRAMEWORK's
DEFAULT_AUTHENTICATION_CLASSES is JWT-only (config/settings.py), so that hook
can never run. Net truth (live-confirmed in BACKEND_REQUESTS.md "[P1] CSRF
enforcement for session-cart mutations"):

- Session-cookie cart mutations (POST/PATCH/DELETE on /api/cart/*) are NOT
  CSRF-enforced: a cross-site request that can attach the session cookie can
  mutate a victim's cart.
- JWT-authenticated money paths (checkout, apply-coupon) are not
  CSRF-enforced either, but the JWT is not an ambient credential -- a
  cross-site request cannot attach it -- so their exploitable surface is the
  session cookie they ALSO require (cart resolution), same as above.
- Mitigations: SESSION_COOKIE_SAMESITE 'Lax' (Django default --
  browser-trust, not server enforcement) plus the same-site deployment
  constraint documented in BACKEND_REQUESTS.md.

When SPEC-17-03 lands SessionAuthentication in DEFAULT_AUTHENTICATION_CLASSES
(SessionAuthentication.enforce_csrf then rejects tokenless unsafe requests
that authenticate by session), the behaviour pins below flip 2xx -> 403 and
CsrfStackWiringTests fails: updating this module is the deliberate one-line
contract update, not an accident.

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
    """The load-bearing wiring, pinned so the SPEC-17-03 flip cannot land
    silently: changing either line must update this module in the same
    commit."""

    def test_csrf_middleware_installed_but_drf_csrf_hook_absent(self):
        default_auth = settings.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]
        self.assertIn(
            "django.middleware.csrf.CsrfViewMiddleware", settings.MIDDLEWARE
        )
        self.assertEqual(
            [cls.split(".")[-1] for cls in default_auth],
            ["JWTAuthentication"],
            "DEFAULT_AUTHENTICATION_CLASSES changed: adding "
            "SessionAuthentication enforces CSRF on session-cookie flows "
            "(SPEC-17-03) -- review and update the CSRF pins in this module "
            "in the same commit.",
        )

    def test_session_cookie_samesite_default_is_the_documented_mitigation(self):
        """'Lax' is the relied-upon mitigation for today's enforcement gap
        (browser-trust, not server enforcement). If a deployment hardens to
        'Strict' or the gap closes server-side (SPEC-17-03), update this pin
        in the same commit."""
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")


class SessionCartCsrfTruthTests(ApiTestCase):
    """Cart endpoints key on the session cookie alone (no JWT involved).
    Pins today's truth: these mutations are NOT CSRF-enforced."""

    def test_cart_add_with_session_cookie_and_no_csrf_token_succeeds(self):
        """[Pin] POST /api/cart/ with only the session cookie, no X-CSRFToken
        -> 201 (BACKEND_REQUESTS.md live-confirmed). SPEC-17-03 (adding
        SessionAuthentication) flips this to 403."""
        product = self.make_product()
        self.assertEqual(self.client.get("/api/cart/").status_code, 200)
        res = self.client.post(
            "/api/cart/",
            {"product_id": product.id, "quantity": 2},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(len(res.data["items"]), 1)

    def test_cart_item_update_and_remove_without_csrf_token_succeed(self):
        """[Pin] PATCH/DELETE /api/cart/{id}/ with only the session cookie,
        no X-CSRFToken -> 200/200 (BACKEND_REQUESTS.md live-confirmed)."""
        product = self.make_product(stock=5)
        cart = self.seed_session_cart([(product, 1)])
        item_id = cart["items"][0]["id"]
        res = self.client.patch(
            f"/api/cart/{item_id}/", {"quantity": 3}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        res = self.client.delete(f"/api/cart/{item_id}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["items"], [])

    def test_cart_mutations_still_succeed_under_enforced_csrf_checks(self):
        """[Pin + differential] A client that asks CsrfViewMiddleware to
        fully enforce (enforce_csrf_checks=True) cannot get the cart
        rejected either: DRF marks its views csrf_exempt, so the middleware
        short-circuits before any token check. The exemption is server-side,
        not a test-client artifact."""
        product = self.make_product()
        enforced = APIClient(enforce_csrf_checks=True)
        res = enforced.post(
            "/api/cart/",
            {"product_id": product.id, "quantity": 1},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

    def test_api_never_issues_a_csrf_cookie(self):
        """[Pin] No API response hands a client a CSRF token
        (BACKEND_REQUESTS.md: no CSRF token can be sent because the API-only
        backend never issues a CSRF cookie) -- the SameSite mitigation is all
        that stands between a cross-site request and a session-cart mutation
        today."""
        res = self.client.get("/api/cart/")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("csrftoken", res.cookies)


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
    """DRF's enforce_csrf runs only from SessionAuthentication; with
    JWT-only defaults it never runs, and the middleware skips the
    csrf_exempt DRF views. Pins that the money paths named in
    BACKEND_REQUESTS.md answer 2xx with no CSRF token."""

    def test_checkout_with_jwt_and_no_csrf_token_succeeds(self):
        """[Pin] POST /api/orders/checkout/ -- session cart + JWT, no
        X-CSRFToken -> 201. The session cookie checkout requires for cart
        resolution rides the ambient-credential path CSRF protects; a
        cross-site request still cannot checkout without the (non-ambient)
        JWT. SPEC-17-03 makes SessionAuthentication enforce_csrf reject
        tokenless submissions that authenticate by session."""
        product = self.make_product()
        self.seed_session_cart([(product, 1)])
        self.make_user()
        _, token = self.api_login()
        self.assertTrue(token)
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)

    def test_apply_coupon_with_jwt_and_no_csrf_token_succeeds(self):
        """[Pin] POST /api/orders/apply-coupon/ -- no X-CSRFToken -> 200
        preview. Same JWT-only truth as checkout."""
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
        order = listing.data[0]
        self.assertEqual(order["full_name"], name)
        self.assertEqual(order["address"], address)

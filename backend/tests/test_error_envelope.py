"""SPEC-9-03: uniform error envelope (R-9.2.19, spec line 3002).

Spec §9.2 closes the API contract with "Use appropriate HTTP status codes,
validation errors and consistent error responses" — it prescribes
consistency, not a literal shape, so the single envelope shape
``{"error", "code", "details"}`` is pinned here and applied to every JSON
error response the backend emits, from two directions that never overlap:
the ``REST_FRAMEWORK`` exception handler envelops DRF exception bodies
(``{"detail": ...}``, serializer field-error mappings) at the source, and
``common.errors.ErrorEnvelopeMiddleware`` envelops the ad-hoc
``{"error": ...}`` view returns that raise no exception. The middleware is
idempotent, so a handler-produced envelope is never wrapped twice.

``ErrorEnvelopeWireTests`` pins what the CLIENT receives: the rendered
``response.content`` bytes. Asserting ``response.data`` alone is the trap
that let the wire bug ship — the middleware can rewrite ``.data`` after
Django's base handler has already rendered the body, leaving the bytes on
the wire in the raw DRF shape.
"""

import json
from decimal import Decimal
from unittest.mock import patch

from django.http import HttpResponse
from django.test import override_settings
from django.urls import path

from common.testing import ApiTestCase


def _broken_json_view(request):
    """A JSON-content-type error body that is not valid JSON: no real view
    emits one, but the middleware must pass it through untouched rather
    than crash the response chain."""
    return HttpResponse("{broken", status=400, content_type="application/json")


urlpatterns = [
    path("broken-json/", _broken_json_view),
]


class ErrorEnvelopeTests(ApiTestCase):
    # ad-hoc {"error": ...} view returns --------------------------------------
    def test_adhoc_error_body_is_enveloped(self):
        self.make_product()
        res = self.client.get("/api/products/does-not-exist/")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(
            res.data,
            {
                "error": "Product not found",
                "code": "not_found",
                "details": {},
            },
        )

    # DRF exception bodies ----------------------------------------------------
    def test_drf_permission_denial_is_enveloped(self):
        res = self.client.post("/api/products/", {"name": "X"}, format="json")
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(
            res.data,
            {
                "error": "Administrator access is required.",
                "code": "permission_denied",
                "details": {},
            },
        )

    def test_drf_authentication_error_is_enveloped(self):
        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(res.data["code"], "not_authenticated")
        self.assertIsInstance(res.data["error"], str)
        self.assertEqual(res.data["details"], {})

    # serializer field errors -------------------------------------------------
    def test_serializer_field_errors_move_into_details(self):
        res = self.client.post(
            "/api/accounts/register/", {"username": "enveloper"}, format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["code"], "validation_error")
        self.assertEqual(res.data["error"], "Validation failed.")
        self.assertIsInstance(res.data["details"]["password"], list)

    # context keys ride inside details ----------------------------------------
    def test_scalar_context_moves_into_details(self):
        product = self.make_product(price="100.00", stock=5)
        self.register_and_verify()
        self.api_login()
        self.seed_session_cart([(product, 1)])
        self.make_coupon(code="BIGSPEND", minimum_order_amount="5000")
        res = self.checkout(coupon_code="BIGSPEND")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Minimum order amount is required")
        self.assertEqual(
            res.data["details"]["minimum_order_amount"], Decimal("5000.00")
        )

    # pass-through guards -----------------------------------------------------
    def test_non_error_json_body_is_untouched(self):
        """The health probe's 503 body is a plain checks dict, not an error
        payload — the envelope must leave it byte-identical."""
        with patch("ops.views.get_health", return_value={"status": "degraded"}):
            res = self.client.get("/health/")
        self.assertEqual(res.status_code, 503, res.content)
        self.assertEqual(json.loads(res.content), {"status": "degraded"})
        self.assertNotIn(b"code", res.content)

    def test_error_json_body_on_plain_view_is_rewritten(self):
        """A non-DRF JSON error response (JsonResponse-style) carrying the
        ad-hoc error shape is enveloped through the content path."""
        with patch(
            "ops.views.get_health",
            return_value={"status": "degraded", "error": "boom"},
        ):
            res = self.client.get("/health/")
        self.assertEqual(res.status_code, 503, res.content)
        body = json.loads(res.content)
        self.assertEqual(body["error"], "boom")
        self.assertEqual(body["code"], "server_error")

    def test_unparseable_json_error_body_is_untouched(self):
        with override_settings(ROOT_URLCONF="tests.test_error_envelope"):
            res = self.client.get("/broken-json/")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.content, b"{broken")


class ErrorEnvelopeWireTests(ApiTestCase):
    """The client-facing pin: BYTES (``response.content``), never
    ``response.data``.

    Django's base handler renders the response before the middleware chain
    runs, so a payload rewrite that stops at ``.data`` never reaches the
    wire — each test below asserts the exact body a socket-level client
    would read, and asserts the envelope arrives exactly once (no
    middleware-over-handler double wrap).
    """

    def _wire_body(self, res):
        """Parse the rendered bytes — the only surface this class trusts."""
        self.assertIn(b"application/json", res["Content-Type"].encode())
        return json.loads(res.content)

    def _assert_envelope_once(self, body, *, code):
        """Exactly one envelope: exact key set, right code, no nested
        envelope inside ``details`` and no leftover raw ``detail`` key."""
        self.assertEqual(set(body), {"error", "code", "details"})
        self.assertIsInstance(body["error"], str)
        self.assertTrue(body["error"])
        self.assertEqual(body["code"], code)
        self.assertIsInstance(body["details"], dict)
        self.assertNotIn("detail", body)
        for value in body["details"].values():
            if isinstance(value, dict):
                self.assertFalse({"error", "code", "details"} <= set(value))

    def test_401_permission_bytes_are_envelope(self):
        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 401)
        self._assert_envelope_once(self._wire_body(res), code="not_authenticated")

    def test_400_validation_bytes_are_envelope(self):
        res = self.client.post(
            "/api/accounts/register/", {"username": "wirepin"}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        body = self._wire_body(res)
        self._assert_envelope_once(body, code="validation_error")
        self.assertEqual(body["error"], "Validation failed.")
        self.assertIsInstance(body["details"].get("password"), list)

    def test_403_permission_bytes_are_envelope(self):
        res = self.client.post("/api/products/", {"name": "X"}, format="json")
        self.assertEqual(res.status_code, 403)
        body = self._wire_body(res)
        self._assert_envelope_once(body, code="permission_denied")
        self.assertEqual(body["error"], "Administrator access is required.")

    def test_guest_refusal_403_bytes_are_envelope(self):
        """The GUEST-DEADEND refusal path: an ad-hoc ``Response({"error"})``
        that raises no exception, so only the middleware can envelop it —
        and it must do so on the rendered bytes."""
        res = self.client.post("/api/orders/checkout/", {}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(
            res.content,
            b'{"error":"Guest checkout is not available. '
            b'Sign in or create an account to place an order.",'
            b'"code":"permission_denied","details":{}}',
        )
        self._assert_envelope_once(self._wire_body(res), code="permission_denied")

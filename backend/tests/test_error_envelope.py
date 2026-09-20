"""SPEC-9-03: uniform error envelope (R-9.2.19, spec line 3002).

Spec §9.2 closes the API contract with "Use appropriate HTTP status codes,
validation errors and consistent error responses" — it prescribes
consistency, not a literal shape, so the single envelope shape
``{"error", "code", "details"}`` is pinned here and applied by
``common.errors.ErrorEnvelopeMiddleware`` to every JSON error response the
backend emits: ad-hoc ``{"error": ...}`` view returns, DRF exception bodies
(``{"detail": ...}``) and serializer field-error mappings alike.
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

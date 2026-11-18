import json
from unittest.mock import patch

from django.http import HttpResponse
from django.test import override_settings
from django.urls import path

from common.testing import ApiTestCase


def _broken_json_view(request):
    return HttpResponse("{broken", status=400, content_type="application/json")


urlpatterns = [
    path("broken-json/", _broken_json_view),
]


class ErrorEnvelopeTests(ApiTestCase):
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

    def test_serializer_field_errors_move_into_details(self):
        res = self.client.post(
            "/api/accounts/register/", {"username": "enveloper"}, format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["code"], "validation_error")
        self.assertEqual(res.data["error"], "Validation failed.")
        self.assertIsInstance(res.data["details"]["password"], list)

    def test_scalar_context_moves_into_details(self):
        product = self.make_product(price="100.00", stock=5)
        self.register_and_verify()
        self.api_login()
        self.seed_session_cart([(product, 1)])
        self.make_coupon(code="BIGSPEND", minimum_order_amount="5000")
        res = self.checkout(coupon_code="BIGSPEND")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Minimum order amount is required")
        # The context moves into details; the money value stays a 2-dp string so the envelope never hands the client a JSON float.
        self.assertEqual(res.data["details"]["minimum_order_amount"], "5000.00")

    def test_non_error_json_body_is_untouched(self):
        with patch("ops.views.get_health", return_value={"status": "degraded"}):
            res = self.client.get("/health/")
        self.assertEqual(res.status_code, 503, res.content)
        self.assertEqual(json.loads(res.content), {"status": "degraded"})
        self.assertNotIn(b"code", res.content)

    def test_error_json_body_on_plain_view_is_rewritten(self):
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
    """The client-facing pin: BYTES, never response.data - Django renders the body before the middleware chain, so a .data rewrite never reaches the wire."""

    def _wire_body(self, res):
        self.assertIn(b"application/json", res["Content-Type"].encode())
        return json.loads(res.content)

    def _assert_envelope_once(self, body, *, code):
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
        res = self.client.post("/api/orders/checkout/", {}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(
            res.content,
            b'{"error":"Guest checkout is not available. '
            b'Sign in or create an account to place an order.",'
            b'"code":"permission_denied","details":{}}',
        )
        self._assert_envelope_once(self._wire_body(res), code="permission_denied")

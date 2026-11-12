import logging

from corsheaders.defaults import default_headers as cors_default_headers
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

import config.settings as config_settings
from common.middleware import (
    NO_REQUEST_ID,
    REQUEST_ID_HEADER,
    REQUEST_ID_MAX_LENGTH,
    RequestIDFormatter,
    RequestIDMiddleware,
    sanitize_request_id,
)
from common.models import AuditEvent
from common.testing import ApiTestCase
from orders.views import GUEST_TOKEN_HEADER, IDEMPOTENCY_KEY_HEADER

TRACED = "abc123def456"


def failing_request(client, path, **kwargs):
    client.raise_request_exception = False
    return client.post(path, **kwargs)


class PropagationTests(ApiTestCase):
    def test_inbound_request_id_is_honoured_and_echoed(self):
        res = self.client.get("/api/products/", headers={REQUEST_ID_HEADER: TRACED})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers[REQUEST_ID_HEADER], TRACED)

    def test_request_without_one_gets_a_generated_id_echoed(self):
        res = self.client.get("/api/products/")
        request_id = res.headers[REQUEST_ID_HEADER]
        self.assertEqual(len(request_id), 32)
        self.assertTrue(all(char in "0123456789abcdef" for char in request_id))

    def test_generated_ids_differ_per_request(self):
        first = self.client.get("/api/products/")
        second = self.client.get("/api/products/")
        self.assertNotEqual(
            first.headers[REQUEST_ID_HEADER], second.headers[REQUEST_ID_HEADER]
        )

    def test_header_is_present_on_a_4xx_response_too(self):
        res = self.client.get("/api/products/no-such-slug/")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.headers[REQUEST_ID_HEADER], res["X-Request-ID"])
        self.assertTrue(res["X-Request-ID"])


class UnhandledExceptionTests(ApiTestCase):
    def _make_failing_payment_request(self, request_id):
        """Checkout, then make the gateway raise out of the view: that is a genuine unhandled exception converted to a 500 above the middleware."""
        self.make_user("buyer")
        _, _ = self.api_login()
        product = self.make_product(stock=10)
        self.seed_session_cart([(product, 2)])
        checked_out = self.checkout()
        self.assertEqual(checked_out.status_code, 201)
        client_mock = self.razorpay_mock()
        client_mock.order.create.side_effect = RuntimeError("gateway down")
        # The id the checkout minted, not a literal: a sequence is not transactional, so the first row on PostgreSQL is not id=1.
        return failing_request(
            self.client,
            "/api/orders/payment/",
            data={"order_id": checked_out.data["id"]},
            format="json",
            headers={REQUEST_ID_HEADER: request_id},
        )

    def test_unhandled_exception_500_still_returns_the_request_id(self):
        res = self._make_failing_payment_request(TRACED)
        self.assertEqual(res.status_code, 500)
        self.assertEqual(res.headers[REQUEST_ID_HEADER], TRACED)

    def test_exception_escaping_the_whole_chain_is_converted_here(self):
        factory = RequestFactory()

        def explode(request):
            raise RuntimeError("nothing above this middleware")

        request = factory.get("/api/products/")
        response = RequestIDMiddleware(explode)(request)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers[REQUEST_ID_HEADER], request.request_id)


class SanitizationTests(ApiTestCase):
    def _echoed_id_for(self, hostile):
        res = self.client.get("/api/products/", headers={REQUEST_ID_HEADER: hostile})
        self.assertEqual(res.status_code, 200)
        return res.headers[REQUEST_ID_HEADER]

    def test_over_long_inbound_value_is_replaced_not_echoed(self):
        too_long = "a" * (REQUEST_ID_MAX_LENGTH + 1)
        echoed = self._echoed_id_for(too_long)
        self.assertNotEqual(echoed, too_long)
        self.assertLessEqual(len(echoed), REQUEST_ID_MAX_LENGTH)

    def test_inbound_value_at_the_cap_is_honoured(self):
        at_cap = "b" * REQUEST_ID_MAX_LENGTH
        self.assertEqual(self._echoed_id_for(at_cap), at_cap)

    def test_header_injecting_value_is_rejected(self):
        for hostile in (
            "abc\r\nX-Injected: yes",
            "abc def",
            "abc<script>",
            "abc/def",
            "a" * 200,
        ):
            with self.subTest(value=hostile):
                echoed = self._echoed_id_for(hostile)
                self.assertNotIn(hostile, echoed)
                self.assertEqual(len(echoed), 32)

    def test_surrounding_whitespace_is_trimmed_then_validated(self):
        self.assertEqual(sanitize_request_id("  " + TRACED + "  "), TRACED)
        self.assertEqual(sanitize_request_id("   "), "")
        self.assertEqual(sanitize_request_id(None), "")
        self.assertEqual(sanitize_request_id("has space"), "")
        self.assertEqual(sanitize_request_id("a\nb"), "")


class AuditAndLogCorrelationTests(ApiTestCase):
    def test_audit_row_stores_the_request_id(self):
        self.make_user("buyer")
        res = self.client.post(
            "/api/accounts/login/",
            {"username": "buyer", "password": "S3cure-Passphrase!"},
            format="json",
            headers={REQUEST_ID_HEADER: TRACED},
        )
        self.assertEqual(res.status_code, 200)
        event = AuditEvent.objects.get(event_type=AuditEvent.EventType.AUTH_LOGIN)
        self.assertEqual(event.request_id, TRACED)
        self.assertEqual(event.request_id, res.headers[REQUEST_ID_HEADER])

    def test_audit_log_line_carries_the_request_id(self):
        self.make_user("buyer")
        with self.assertLogs("common.audit", level="INFO") as logs:
            self.client.post(
                "/api/accounts/login/",
                {"username": "buyer", "password": "S3cure-Passphrase!"},
                format="json",
                headers={REQUEST_ID_HEADER: TRACED},
            )
        self.assertIn(f"request_id={TRACED}", "\n".join(logs.output))

    def test_formatter_stamps_the_id_on_every_record(self):
        formatter = RequestIDFormatter(
            "{name} request_id={request_id} {message}", style="{"
        )
        record = logging.LogRecord(
            "probe", logging.INFO, __file__, 1, "hello", None, None
        )
        formatted = {}

        def serve(request):
            formatted["inside"] = formatter.format(record)
            return HttpResponse("ok")

        request = RequestFactory().get("/api/products/")
        served = RequestIDMiddleware(serve)(request)
        self.assertEqual(
            formatted["inside"],
            f"probe request_id={served.headers[REQUEST_ID_HEADER]} hello",
        )

    def test_formatter_uses_a_placeholder_outside_a_request(self):
        formatter = RequestIDFormatter("request_id={request_id} {message}", style="{")
        record = logging.LogRecord(
            "probe", logging.INFO, __file__, 1, "hello", None, None
        )
        self.assertEqual(formatter.format(record), f"request_id={NO_REQUEST_ID} hello")

    def test_audit_event_written_outside_a_request_has_no_id(self):
        event = AuditEvent.record(AuditEvent.EventType.ORDER_CREATED)
        self.assertEqual(event.request_id, "")


class LogFormatterWiringTests(SimpleTestCase):
    def test_wired_formatter_stamps_the_request_id(self):
        formatter = config_settings.LOGGING["formatters"]["plain"]
        self.assertEqual(formatter["()"], "common.middleware.RequestIDFormatter")
        self.assertIn("request_id={request_id}", formatter["format"])
        self.assertEqual(formatter["style"], "{")
        for part in ("levelname", "asctime", "name", "message"):
            self.assertIn(part, formatter["format"])

    def test_cors_exposes_the_header_to_browsers(self):
        self.assertEqual(config_settings.CORS_EXPOSE_HEADERS, [REQUEST_ID_HEADER])

    def test_middleware_is_first_in_the_chain(self):
        self.assertEqual(
            config_settings.MIDDLEWARE[0], "common.middleware.RequestIDMiddleware"
        )


class CorsAllowHeadersTests(SimpleTestCase):
    """A preflight that omits a header makes the BROWSER refuse the request, so checkout (Idempotency-Key) and guest reads (X-Guest-Order-Token) die before the server sees the POST."""

    def test_checkout_and_guest_headers_are_allowed(self):
        self.assertEqual(IDEMPOTENCY_KEY_HEADER, "Idempotency-Key")
        self.assertEqual(GUEST_TOKEN_HEADER, "X-Guest-Order-Token")
        allowed = {h.lower() for h in config_settings.CORS_ALLOW_HEADERS}
        self.assertIn(IDEMPOTENCY_KEY_HEADER.lower(), allowed)
        self.assertIn(GUEST_TOKEN_HEADER.lower(), allowed)

    def test_corsheaders_defaults_are_still_allowed(self):
        allowed = {h.lower() for h in config_settings.CORS_ALLOW_HEADERS}
        for header in cors_default_headers:
            self.assertIn(header, allowed)


class RequestIdIsNotLeakedBetweenRequestsTests(ApiTestCase):
    def test_second_request_gets_its_own_id_and_the_first_is_not_reused(self):
        first = self.client.get("/api/products/", headers={REQUEST_ID_HEADER: TRACED})
        second = self.client.get("/api/products/")
        self.assertEqual(first.headers[REQUEST_ID_HEADER], TRACED)
        self.assertNotEqual(second.headers[REQUEST_ID_HEADER], TRACED)

"""SPEC-7-02: logging baseline.

- The LOGGING dict is env-driven: LOG_LEVEL sets the app level through a
  fail-safe resolver (unknown values fall back to the default), LOG_FILE
  adds the rotating file handler (nothing hardcodes a path), and the
  request / audit channels are pinned so payment failures and the audit
  trail stay observable regardless of the app level.
- Every AuditEvent-emitting failure branch in verify_payment emits a log
  record naming the order reference and the reason, payment-intent
  creation failures log with their traceback, and AuditEvent.record emits
  one INFO line per event.
- No secret values (the Razorpay key id / secret) appear in the output.
"""
import os
from unittest.mock import patch

import config.settings as config_settings
from django.test import SimpleTestCase

from common.models import AuditEvent
from common.testing import (
    TEST_RAZORPAY_KEY_ID,
    TEST_RAZORPAY_KEY_SECRET,
    ApiTestCase,
)


class LogLevelEnvTests(SimpleTestCase):
    """_env_log_level: env-driven with a fail-safe default."""

    def test_valid_level_is_used_case_insensitively(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "warning"}):
            self.assertEqual(
                config_settings._env_log_level("LOG_LEVEL", "INFO"),
                "WARNING",
            )

    def test_unknown_level_falls_back_with_warning(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "loud"}):
            with self.assertLogs("config.settings", level="WARNING") as logs:
                self.assertEqual(
                    config_settings._env_log_level("LOG_LEVEL", "INFO"),
                    "INFO",
                )
        # The resolver upper-cases before matching, so the warning names
        # the offending value in its normalised form.
        self.assertIn("LOUD", logs.output[0])

    def test_missing_level_uses_default(self):
        env = {k: v for k, v in os.environ.items() if k != "LOG_LEVEL"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                config_settings._env_log_level("LOG_LEVEL", "INFO"),
                "INFO",
            )

    def test_notset_is_rejected_as_app_level(self):
        # A root logger at NOTSET logs everything — the opposite of a level
        # constraint — so it is treated as an unknown value.
        with patch.dict(os.environ, {"LOG_LEVEL": "NOTSET"}):
            with self.assertLogs("config.settings", level="WARNING"):
                self.assertEqual(
                    config_settings._env_log_level("LOG_LEVEL", "INFO"),
                    "INFO",
                )


class LoggingConfigStructureTests(SimpleTestCase):
    """The wiring itself: handlers, pins, and env self-consistency."""

    def test_console_handler_always_wired(self):
        self.assertIn("console", config_settings.LOGGING["handlers"])

    def test_root_level_matches_env_resolved_setting(self):
        # LOGGING must carry the same level the env resolver produced, so
        # LOG_LEVEL provably reaches the config without a hardcoded drift.
        self.assertEqual(
            config_settings.LOGGING["root"]["level"],
            config_settings.APP_LOG_LEVEL,
        )

    def test_request_errors_pinned_at_error(self):
        entry = config_settings.LOGGING["loggers"]["django.request"]
        self.assertEqual(entry["level"], "ERROR")

    def test_audit_channel_pinned_at_info(self):
        entry = config_settings.LOGGING["loggers"]["common.audit"]
        self.assertEqual(entry["level"], "INFO")

    def test_brace_formatter_covers_who_what_when(self):
        formatter = config_settings.LOGGING["formatters"]["plain"]
        self.assertEqual(formatter["style"], "{")
        for part in ("levelname", "asctime", "name", "message"):
            self.assertIn(part, formatter["format"])


class FileHandlerTests(SimpleTestCase):
    """LOG_FILE adds a rotating file handler; nothing hardcodes a path."""

    def test_no_file_handler_without_a_path(self):
        built = config_settings._build_logging("INFO")
        self.assertEqual(list(built["handlers"]), ["console"])
        self.assertEqual(built["root"]["handlers"], ["console"])

    def test_env_path_adds_rotating_handler_everywhere(self):
        built = config_settings._build_logging("INFO", "logs/app.log")
        file_handler = built["handlers"]["file"]
        self.assertEqual(
            file_handler["class"], "logging.handlers.RotatingFileHandler"
        )
        self.assertEqual(file_handler["filename"], "logs/app.log")
        self.assertEqual(built["root"]["handlers"], ["console", "file"])


class AuditTrailLogTests(ApiTestCase):
    """The AuditEvent.record path emits one INFO line per event."""

    def test_record_emits_info_line_with_stored_identity(self):
        user = self.make_user("trail-logger")
        with self.assertLogs("common.audit", level="INFO") as logs:
            event = AuditEvent.record(
                AuditEvent.EventType.AUTH_LOGIN,
                actor=user,
                detail={"username": "trail-logger"},
            )
        line = logs.output[0]
        self.assertIn(f"audit auth.login id={event.pk}", line)
        self.assertIn("actor=trail-logger", line)
        self.assertIn("trail-logger", line)

    def test_record_anonymous_actor_logs_none(self):
        with self.assertLogs("common.audit", level="INFO") as logs:
            AuditEvent.record(AuditEvent.EventType.ORDER_CREATED)
        self.assertIn("actor=None", logs.output[0])


class VerifyPaymentLogTests(ApiTestCase):
    """Every verify_payment failure branch logs reference and reason."""

    def setUp(self):
        self.user = self.make_user("buyer")
        _, self.token = self.api_login()
        self.product = self.make_product(stock=10)

    def _checkout(self, **overrides):
        self.seed_session_cart([(self.product, 2)])
        res = self.checkout(**overrides)
        self.assertEqual(res.status_code, 201, res.data)
        return res.data["id"]

    def _start_payment(self, order_id):
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order_id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)

    def _verify(self, order_id, **overrides):
        payload = {
            "razorpay_order_id": "order_TEST0001",
            "razorpay_payment_id": "pay_TEST0001",
            "razorpay_signature": "sig",
            "order_id": order_id,
        }
        payload.update(overrides)
        return self.client.post(
            "/api/orders/payment/verify/", payload, format="json"
        )

    def test_signature_rejection_logs_warning_with_gateway_reference(self):
        order_id = self._checkout()
        client_mock = self.razorpay_mock()
        self.razorpay_fail_signature(client_mock)
        with self.assertLogs("orders.views", level="WARNING") as logs:
            res = self._verify(order_id)
        self.assertEqual(res.status_code, 400, res.data)
        line = "\n".join(logs.output)
        self.assertIn("Payment signature rejected", line)
        self.assertIn("order_TEST0001", line)

    def test_order_not_found_logs_warning_with_claimed_id(self):
        self._checkout()
        self.razorpay_mock()
        with self.assertLogs("orders.views", level="WARNING") as logs:
            res = self._verify(999999)
        self.assertEqual(res.status_code, 404, res.data)
        line = "\n".join(logs.output)
        self.assertIn("not found", line)
        self.assertIn("999999", line)

    def test_replayed_verify_logs_info(self):
        order_id = self._checkout()
        self.razorpay_mock()
        self._start_payment(order_id)
        self.assertEqual(self._verify(order_id).status_code, 200)
        with self.assertLogs("orders.views", level="INFO") as logs:
            res = self._verify(order_id)
        self.assertEqual(res.status_code, 400, res.data)
        line = "\n".join(logs.output)
        self.assertIn("already processed", line)
        self.assertIn(str(order_id), line)

    def test_reference_mismatch_logs_warning_with_both_references(self):
        order_id = self._checkout()
        self.razorpay_mock()
        self._start_payment(order_id)
        with self.assertLogs("orders.views", level="WARNING") as logs:
            res = self._verify(order_id, razorpay_order_id="order_OTHER")
        self.assertEqual(res.status_code, 400, res.data)
        line = "\n".join(logs.output)
        self.assertIn("is bound to gateway order", line)
        self.assertIn("order_OTHER", line)
        self.assertIn(str(order_id), line)

    def test_stock_conflict_logs_info_with_order_reference(self):
        order_id = self._checkout()
        self.razorpay_mock()
        self._start_payment(order_id)
        # Another buyer drains the stock between checkout and verify.
        self.product.refresh_from_db()
        self.product.stock = 0
        self.product.save(update_fields=["stock"])
        with self.assertLogs("orders.views", level="INFO") as logs:
            res = self._verify(order_id)
        self.assertEqual(res.status_code, 409, res.data)
        line = "\n".join(logs.output)
        self.assertIn("stock conflict", line)
        self.assertIn(str(order_id), line)

    def test_coupon_invalid_logs_info_with_order_reference(self):
        coupon = self.make_coupon(discount_value="10")
        order_id = self._checkout(coupon_code="SAVE10")
        self.razorpay_mock()
        self._start_payment(order_id)
        coupon.active = False
        coupon.save(update_fields=["active"])
        with self.assertLogs("orders.views", level="INFO") as logs:
            res = self._verify(order_id)
        self.assertEqual(res.status_code, 409, res.data)
        line = "\n".join(logs.output)
        self.assertIn("no longer valid", line)
        self.assertIn(str(order_id), line)

    def test_gateway_failure_logs_error_with_order_reference(self):
        order_id = self._checkout()
        client_mock = self.razorpay_mock()
        client_mock.order.create.side_effect = RuntimeError("gateway down")
        self.client.raise_request_exception = False
        with self.assertLogs("orders.views", level="ERROR") as logs:
            res = self.client.post(
                "/api/orders/payment/", {"order_id": order_id}, format="json"
            )
        self.assertEqual(res.status_code, 500)
        line = "\n".join(logs.output)
        self.assertIn("Payment intent creation failed", line)
        self.assertIn(str(order_id), line)
        # logger.exception: the traceback rides the record, not the text.
        self.assertIsNotNone(logs.records[0].exc_info)


class NoSecretsInLogTests(ApiTestCase):
    """The introduced log output never carries key material."""

    def test_signature_failure_output_has_no_razorpay_credentials(self):
        self.make_user("buyer")
        self.api_login()
        product = self.make_product(stock=10)
        self.seed_session_cart([(product, 2)])
        self.assertEqual(self.checkout().status_code, 201)
        client_mock = self.razorpay_mock()
        self.razorpay_fail_signature(client_mock)
        with self.assertLogs(level="INFO") as logs:
            res = self.client.post(
                "/api/orders/payment/verify/",
                {
                    "razorpay_order_id": "order_TEST0001",
                    "razorpay_payment_id": "pay_TEST0001",
                    "razorpay_signature": "sig",
                    "order_id": 999999,
                },
                format="json",
            )
        self.assertEqual(res.status_code, 400, res.data)
        output = "\n".join(logs.output)
        # Both introduced surfaces are present...
        self.assertIn("Payment signature rejected", output)
        self.assertIn("audit payment.signature_rejected", output)
        # ...and neither leaks the configured credentials.
        self.assertNotIn(TEST_RAZORPAY_KEY_ID, output)
        self.assertNotIn(TEST_RAZORPAY_KEY_SECRET, output)

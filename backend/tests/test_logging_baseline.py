import os
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import SimpleTestCase

import config.settings as config_settings
from common import notifications
from common.models import AuditEvent
from common.testing import (
    TEST_RAZORPAY_KEY_ID,
    TEST_RAZORPAY_KEY_SECRET,
    ApiTestCase,
)
from orders.models import Order
from orders.views import checkout as checkout_views


class LogLevelEnvTests(SimpleTestCase):
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
    def test_console_handler_always_wired(self):
        self.assertIn("console", config_settings.LOGGING["handlers"])

    def test_root_level_matches_env_resolved_setting(self):
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
    def test_no_file_handler_without_a_path(self):
        built = config_settings._build_logging("INFO")
        self.assertEqual(list(built["handlers"]), ["console"])
        self.assertEqual(built["root"]["handlers"], ["console"])

    def test_env_path_adds_rotating_handler_everywhere(self):
        built = config_settings._build_logging("INFO", "logs/app.log")
        file_handler = built["handlers"]["file"]
        self.assertEqual(file_handler["class"], "logging.handlers.RotatingFileHandler")
        self.assertEqual(file_handler["filename"], "logs/app.log")
        self.assertEqual(built["root"]["handlers"], ["console", "file"])


class AuditTrailLogTests(ApiTestCase):
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
        return self.client.post("/api/orders/payment/verify/", payload, format="json")

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
        self.assertIsNotNone(logs.records[0].exc_info)


class NoSecretsInLogTests(ApiTestCase):
    def _make_order(self, **overrides):
        fields = {
            "full_name": "Log Buyer",
            "phone": "9876543210",
            "address": "12 Rose Lane",
            "city": "Mumbai",
            "state": "Maharashtra",
            "pincode": "400001",
            "discount_amount": Decimal("0.00"),
            "shipping_amount": Decimal("0.00"),
            "total_amount": Decimal("499.99"),
        }
        fields.update(overrides)
        return Order.objects.create(**fields)

    def test_no_recipient_address_in_email_or_checkout_logs(self):
        user = self.make_user("logbuyer")
        order = self._make_order(user=user)

        with self.assertLogs("common.notifications", level="INFO") as logs:
            notifications.dispatch(AuditEvent.EventType.ORDER_PAID, {"order": order})

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        output = "\n".join(logs.output)
        self.assertIn("email sent", output)
        self.assertNotIn(user.email, output)
        self.assertIn(str(order.id), output)

        # The guest branch of customer_name is the email, and the guest
        # checkout door is refused, so the replay helper is driven directly.
        guest_email = "guest-leak@example.com"
        guest_order = self._make_order(
            user=None,
            guest_email=guest_email,
            guest_token="logs-baseline-guest-token",
        )
        with self.assertLogs("orders.views.checkout", level="INFO") as logs:
            response = checkout_views._collapse_onto_duplicate(guest_order, None, user)

        self.assertEqual(response.status_code, 200)
        output = "\n".join(logs.output)
        self.assertIn("Checkout dedup", output)
        self.assertNotIn(guest_email, output)
        self.assertIn(str(guest_order.id), output)

    def test_no_username_in_checkout_replay_or_shipping_warning(self):
        user = self.make_user("loggingbaseline")
        _, token = self.api_login(user.username)
        self.auth(token)
        product = self.make_product(stock=10)
        self.seed_session_cart([(product, 2)])

        first = self.checkout()
        self.assertEqual(first.status_code, 201, first.data)
        order_id = first.data["id"]

        with self.assertLogs("orders.views", level="INFO") as logs:
            replay = self.checkout()

        self.assertEqual(replay.status_code, 200, replay.data)
        output = "\n".join(logs.output)
        self.assertIn("Checkout dedup", output)
        self.assertNotIn(user.username, output)
        self.assertIn(str(order_id), output)

        with self.assertLogs("orders.views", level="INFO") as logs:
            keyed = self.client.post(
                "/api/orders/checkout/",
                self.checkout_payload(),
                format="json",
                HTTP_IDEMPOTENCY_KEY="logging-baseline-key",
            )

        self.assertEqual(keyed.status_code, 200, keyed.data)
        output = "\n".join(logs.output)
        self.assertIn("Checkout idempotency", output)
        self.assertNotIn(user.username, output)
        self.assertIn(str(order_id), output)

        # No ShippingMethod exists in a clean test database, so this checkout
        # takes the silent-zero warning branch; the pk is its correlation key.
        with self.assertLogs("orders.views", level="WARNING") as logs:
            priced = self.checkout(city="Pune")

        self.assertEqual(priced.status_code, 201, priced.data)
        output = "\n".join(logs.output)
        self.assertIn("NO shipping charge", output)
        self.assertNotIn(user.username, output)
        self.assertIn(str(user.pk), output)

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
        self.assertIn("Payment signature rejected", output)
        self.assertIn("audit payment.signature_rejected", output)
        self.assertNotIn(TEST_RAZORPAY_KEY_ID, output)
        self.assertNotIn(TEST_RAZORPAY_KEY_SECRET, output)

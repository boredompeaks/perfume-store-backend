"""SPEC-19-1: in-process notification service ([R-19.0], spec 19.1).

- The migrated accounts emails (verification, username reminder, password
  reset) ride the one send path with byte-identical bodies, subjects and
  recipients — the pre-service outbox contracts are pinned exactly.
- ``dispatch()`` is the event-driven entry point: explicit calls beside
  ``AuditEvent.record`` hooks inside the caller's atomic block. A send
  failure is logged and swallowed (never breaks the business transaction);
  unregistered events are no-ops.
- Proof event: ``order.paid`` -> order-confirmation email (order number,
  total, frontend URL) on ``verify_payment`` success, with a failing send
  leaving the captured payment confirmed. locmem backend only — no network.
"""
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core import mail
from django.test import tag

from common import notifications
from common.models import AuditEvent
from common.testing import ApiTestCase, extract_link_params
from orders.models import Order


def _make_order(user, total="499.99"):
    return Order.objects.create(
        user=user,
        full_name="Notify Buyer",
        phone="9876543210",
        address="12 Rose Lane",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        total_amount=Decimal(total),
    )


@tag("notifications")
class MigratedEmailContractTests(ApiTestCase):
    """The migrated accounts emails are byte-identical to the retired
    ``_send_email`` f-string bodies — zero behavior change is pinned."""

    def test_verification_email_body_subject_recipient_unchanged(self):
        res = self.client.post(
            "/api/accounts/register/",
            {
                "username": "pinme",
                "email": "pinme@example.com",
                "password": "S3cure-Passphrase!",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Verify your Perfume Store email")
        self.assertEqual(message.to, ["pinme@example.com"])
        self.assertEqual(message.from_email, settings.DEFAULT_FROM_EMAIL)
        uid, token = extract_link_params(message.body, "verify-email")
        self.assertEqual(
            message.body,
            "Welcome! Verify your email by opening this link:\n\n"
            f"{settings.FRONTEND_URL}/verify-email?uid={uid}&token={token}\n\n"
            "If you did not create this account, ignore this email.",
        )

    def test_username_reminder_body_unchanged(self):
        self.make_user("pinuser")
        res = self.client.post(
            "/api/accounts/forgot-username/",
            {"email": "pinuser@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Your Perfume Store username")
        self.assertEqual(message.to, ["pinuser@example.com"])
        self.assertEqual(message.body, "Your username is: pinuser")

    def test_password_reset_body_unchanged(self):
        self.make_user("pinreset")
        res = self.client.post(
            "/api/accounts/password-reset/",
            {"email": "pinreset@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Reset your Perfume Store password")
        self.assertEqual(message.to, ["pinreset@example.com"])
        uid, token = extract_link_params(message.body, "reset-password")
        self.assertEqual(
            message.body,
            "Use this one-time link to choose a new password:\n\n"
            f"{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}\n\n"
            "If you did not request this, ignore this email.",
        )


@tag("notifications")
class SendEmailPathTests(ApiTestCase):
    """The one send path: template loading home + settings sender."""

    def test_send_email_renders_template_and_uses_settings_sender(self):
        notifications.send_email(
            "username_reminder",
            {"username": "pathcheck"},
            "subject pin",
            "pathcheck@example.com",
        )
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.body, "Your username is: pathcheck")
        self.assertEqual(message.from_email, settings.DEFAULT_FROM_EMAIL)
        self.assertEqual(message.to, ["pathcheck@example.com"])


@tag("notifications")
class DispatchTests(ApiTestCase):
    """dispatch(): registry-driven, never raises, log-and-continue."""

    def test_unknown_event_is_a_no_op(self):
        # A future spec-19.1 event with no notification wired yet: nothing
        # is sent, and the no-op is findable in logs (DEBUG) not silent.
        with self.assertLogs("common.notifications", level="DEBUG") as logs:
            notifications.dispatch("order.shipped", {})
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("no notification registered", "\n".join(logs.output))

    def test_order_paid_dispatches_confirmation_email(self):
        order = _make_order(self.make_user("notifybuyer"))
        notifications.dispatch(AuditEvent.EventType.ORDER_PAID, {"order": order})
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [order.user.email])
        self.assertIn(f"#{order.id}", message.subject)
        self.assertIn(f"Order number: #{order.id}", message.body)
        self.assertIn("499.99", message.body)
        self.assertIn(f"{settings.FRONTEND_URL}/orders/", message.body)

    def test_missing_order_context_warns_and_skips(self):
        with self.assertLogs("common.notifications", level="WARNING") as logs:
            notifications.dispatch(AuditEvent.EventType.ORDER_PAID, {})
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("missing order", "\n".join(logs.output))

    def test_send_failure_is_logged_and_swallowed(self):
        order = _make_order(self.make_user("notifyfail"))
        with mock.patch(
            "common.notifications.send_email", side_effect=Exception("smtp down")
        ):
            with self.assertLogs("common.notifications", level="ERROR") as logs:
                notifications.dispatch(
                    AuditEvent.EventType.ORDER_PAID, {"order": order}
                )
        self.assertIn("smtp down", "\n".join(logs.output))
        # Reaching this line proves the exception never escaped dispatch.


@tag("notifications")
class OrderPaidProofEventTests(ApiTestCase):
    """End-to-end: verify_payment success dispatches the confirmation
    email; a send failure never breaks the captured payment."""

    def setUp(self):
        self.user = self.make_user("proofbuyer")
        _, self.token = self.api_login(username=self.user.username)
        self.product = self.make_product(stock=5)

    def _pay(self):
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order_id = res.data["id"]
        self.razorpay_mock()
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order_id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        return self.client.post(
            "/api/orders/payment/verify/",
            {
                "razorpay_order_id": "order_TEST0001",
                "razorpay_payment_id": "pay_TEST0001",
                "razorpay_signature": "sig",
                "order_id": order_id,
            },
            format="json",
        )

    def test_verify_success_sends_confirmation_email(self):
        res = self._pay()
        self.assertEqual(res.status_code, 200, res.data)
        order = Order.objects.get(pk=res.data["order_id"])
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.user.email])
        self.assertIn(f"#{order.id}", message.subject)
        self.assertIn(f"Order number: #{order.id}", message.body)
        self.assertIn("499.99", message.body)

    def test_send_failure_does_not_break_the_payment(self):
        # SMTP-503 mirror of the accounts flows: the payment outcome is
        # untouched, the audit trail still commits, only the send fails.
        with mock.patch(
            "common.notifications.send_email", side_effect=Exception("smtp down")
        ):
            with self.assertLogs("common.notifications", level="ERROR"):
                res = self._pay()
        self.assertEqual(res.status_code, 200, res.data)
        order = Order.objects.get(pk=res.data["order_id"])
        self.assertEqual(order.status, "confirmed")
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_PAID, order=order
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 0)

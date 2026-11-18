from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core import mail
from django.db import transaction
from django.test import tag

from common import notifications
from common.models import AuditEvent, NotificationOutbox
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
    def test_vocabulary_member_with_no_handler_is_a_debug_no_op(self):
        with self.assertLogs("common.notifications", level="DEBUG") as logs:
            notifications.dispatch(AuditEvent.EventType.ORDER_SHIPPED, {})
        self.assertEqual(len(mail.outbox), 0)
        output = "\n".join(logs.output)
        self.assertIn("no notification registered", output)
        # DEBUG is asserted by the absence of the warning wording: assertLogs(DEBUG)
        # admits WARNING lines too, so the level alone cannot tell them apart.
        self.assertNotIn("not an AuditEvent.EventType member", output)

    def test_ordered_member_tuple_is_the_whole_vocabulary_in_declaration_order(self):
        # The module keeps its own ordered enumeration of the registry enum (mypy cannot model Django's metaclass __iter__); this pins it against the enum.
        members = notifications._EVENT_TYPE_MEMBERS
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(set(members), set(AuditEvent.EventType))
        self.assertEqual(set(members), set(notifications._EVENT_TYPE_VALUES))
        self.assertEqual(
            [member.value for member in members],
            [member.value for member in AuditEvent.EventType],
        )
        for member in members:
            self.assertIsInstance(member, AuditEvent.EventType)

    def test_name_outside_the_vocabulary_is_a_warning_not_a_gentle_no_op(self):
        # A name the registry can never match is a programming error, not a content gap, and must not be absorbed at DEBUG.
        with self.assertLogs("common.notifications", level="WARNING") as logs:
            notifications.dispatch("order.teleported", {})
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("not an AuditEvent.EventType member", "\n".join(logs.output))

    def test_dead_name_dispatch_still_never_raises(self):
        notifications.dispatch("order.teleported", {})
        self.assertEqual(len(mail.outbox), 0)

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


@tag("notifications")
class OrderPaidProofEventTests(ApiTestCase):
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

    def test_verify_success_queues_and_the_drain_delivers(self):
        res = self._pay()
        self.assertEqual(res.status_code, 200, res.data)
        order = Order.objects.get(pk=res.data["order_id"])
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(len(mail.outbox), 0)
        row = NotificationOutbox.objects.get(event_type=AuditEvent.EventType.ORDER_PAID)
        self.assertEqual(row.status, NotificationOutbox.Status.PENDING)
        notifications.drain_notifications()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.user.email])
        self.assertIn(f"#{order.id}", message.subject)
        self.assertIn(f"Order number: #{order.id}", message.body)
        self.assertIn("499.99", message.body)

    def test_enqueue_failure_does_not_break_the_payment(self):
        with mock.patch(
            "orders.views.notifications.enqueue",
            side_effect=RuntimeError("outbox unavailable"),
        ):
            with self.assertLogs("orders.views.payment", level="ERROR"):
                res = self._pay()
        self.assertEqual(res.status_code, 200, res.data)
        order = Order.objects.get(pk=res.data["order_id"])
        self.assertEqual(order.status, "confirmed")
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_PAID, order=order
            ).exists()
        )
        self.assertEqual(NotificationOutbox.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)


@tag("notifications")
class PostCommitDispatchTests(ApiTestCase):
    def test_send_is_deferred_until_the_transaction_commits(self):
        order = _make_order(self.make_user("deferred"))
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                notifications.dispatch_on_commit(
                    AuditEvent.EventType.ORDER_PAID, {"order": order}
                )
                self.assertEqual(len(mail.outbox), 0)
            self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [order.user.email])

    def test_rollback_discards_the_registered_send(self):
        order = _make_order(self.make_user("rolledback"))
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    notifications.dispatch_on_commit(
                        AuditEvent.EventType.ORDER_PAID, {"order": order}
                    )
                    raise RuntimeError("later in the money block failed")
        self.assertEqual(len(mail.outbox), 0)

    def test_post_commit_send_failure_is_logged_and_swallowed(self):
        order = _make_order(self.make_user("postcommitfail"))
        with mock.patch(
            "common.notifications.send_email", side_effect=Exception("smtp down")
        ):
            with self.assertLogs("common.notifications", level="ERROR") as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    notifications.dispatch_on_commit(
                        AuditEvent.EventType.ORDER_PAID, {"order": order}
                    )
        self.assertTrue(any(r.levelname == "ERROR" for r in logs.records), logs.records)
        self.assertIn("smtp down", "\n".join(logs.output))

    def test_plain_dispatch_still_sends_synchronously(self):
        order = _make_order(self.make_user("syncreg"))
        with self.captureOnCommitCallbacks(execute=True):
            notifications.dispatch(AuditEvent.EventType.ORDER_PAID, {"order": order})
            self.assertEqual(len(mail.outbox), 1)

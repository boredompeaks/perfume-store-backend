"""SPEC-19-1: in-process notification service ([R-19.0], spec 19.1).

- The migrated accounts emails (verification, username reminder, password
  reset) ride the one send path with byte-identical bodies, subjects and
  recipients — the pre-service outbox contracts are pinned exactly.
- ``dispatch()`` is the event-driven entry point: explicit calls beside
  ``AuditEvent.record`` hooks inside the caller's atomic block. A send
  failure is logged and swallowed (never breaks the business transaction);
  unregistered events are no-ops.
- ASYNC-2b1 adds ``dispatch_on_commit()``: the verify_payment hook site
  registers its order.paid send instead of performing it, so the money
  path's ``select_for_update`` rows are released at commit rather than
  held across the SMTP round trip. Deferral, rollback-discard and the
  post-commit failure log are pinned; the proof-event tests execute the
  callback via ``captureOnCommitCallbacks`` so they cannot pass vacuously.
- Proof event: ``order.paid`` -> order-confirmation email (order number,
  total, frontend URL) on ``verify_payment`` success, with a failing send
  leaving the captured payment confirmed. locmem backend only — no network.
"""

from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core import mail
from django.db import transaction
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

    def test_vocabulary_member_with_no_handler_is_a_debug_no_op(self):
        # A vocabulary member whose content has not landed: nothing is sent,
        # and the no-op is findable in logs (DEBUG) not silent. order.shipped
        # used to be this test's "unknown" name while actually being a name
        # OUTSIDE the vocabulary — the case the next test pins separately.
        with self.assertLogs("common.notifications", level="DEBUG") as logs:
            notifications.dispatch(AuditEvent.EventType.ORDER_SHIPPED, {})
        self.assertEqual(len(mail.outbox), 0)
        output = "\n".join(logs.output)
        self.assertIn("no notification registered", output)
        # DEBUG is asserted by the absence of the warning wording, not by the
        # level alone: assertLogs(DEBUG) admits WARNING lines too, so a pin
        # that only read the capture could not tell the two cases apart.
        self.assertNotIn("not an AuditEvent.EventType member", output)

    def test_ordered_member_tuple_is_the_whole_vocabulary_in_declaration_order(self):
        # The registry is keyed on the enum but the module keeps its own
        # ordered enumeration of it (mypy does not model the metaclass
        # __iter__ Django supplies, so the class object cannot be iterated in
        # typed code). This pins that enumeration against the enum itself:
        # every member, the same objects, the enum's own order, and the same
        # set the DEBUG-vs-WARNING lookup uses — so the two cannot drift apart
        # and make a member-without-handler read as an unknown name.
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
        # ASYNC-2e. A name the registry can never match is a programming
        # error, not a content gap, and the two are no longer indistinguishable
        # — this is the defect class that let three lifecycle notifications go
        # missing with nothing to report. It must not be absorbed at DEBUG.
        with self.assertLogs("common.notifications", level="WARNING") as logs:
            notifications.dispatch("order.teleported", {})
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("not an AuditEvent.EventType member", "\n".join(logs.output))

    def test_dead_name_dispatch_still_never_raises(self):
        # The escalation must not become a failure: a notification can never
        # roll back the transaction it follows, dead name or not.
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
        # ASYNC-2b1: verify_payment REGISTERS the order.paid send on
        # transaction.on_commit, and a TestCase rolls its own transaction
        # back — so without captureOnCommitCallbacks the callback would
        # never run and every assertion below would pass only because
        # nothing was sent. execute=True runs the real callback; the
        # assertions are the original ones, not relaxed.
        with self.captureOnCommitCallbacks(execute=True):
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


@tag("notifications")
class PostCommitDispatchTests(ApiTestCase):
    """ASYNC-2b1: ``dispatch_on_commit`` defers the send past the commit so
    a money-path ``transaction.atomic()`` block is not holding its
    ``select_for_update`` rows across an SMTP round trip."""

    def test_send_is_deferred_until_the_transaction_commits(self):
        order = _make_order(self.make_user("deferred"))
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                notifications.dispatch_on_commit(
                    AuditEvent.EventType.ORDER_PAID, {"order": order}
                )
                # Inside the block, while the row locks are held: nothing
                # sent, so no socket is open under them.
                self.assertEqual(len(mail.outbox), 0)
            # Leaving a savepoint is not the commit either - only the real
            # outermost commit runs the callback.
            self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [order.user.email])

    def test_rollback_discards_the_registered_send(self):
        # The inline send this replaced could not be recalled: a later
        # rollback in the same block still emailed a confirmed-looking
        # order. A registered callback dies with its transaction.
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
        # A callback runs where a raise could not roll anything back, so
        # the broad catch in dispatch() is what keeps it out of an
        # already-decided response - proven here through the real path.
        order = _make_order(self.make_user("postcommitfail"))
        with mock.patch(
            "common.notifications.send_email", side_effect=Exception("smtp down")
        ):
            with self.assertLogs("common.notifications", level="ERROR") as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    notifications.dispatch_on_commit(
                        AuditEvent.EventType.ORDER_PAID, {"order": order}
                    )
        # ERROR with traceback, not a DEBUG line and not a silent pass.
        self.assertTrue(any(r.levelname == "ERROR" for r in logs.records), logs.records)
        self.assertIn("smtp down", "\n".join(logs.output))

    def test_plain_dispatch_still_sends_synchronously(self):
        # dispatch_on_commit is opt-in: the registry itself was NOT made
        # to defer, so the ASYNC-2b2/2b3 registry hook sites
        # (shipped/delivered in orders/events.py, the orders/webhooks.py
        # callback) keep sending inside their own transaction and a caller
        # can still ask for a synchronous send. Back-in-stock is not a
        # registry site at all - products/models.py calls send_email
        # directly - so no change to dispatch could have moved it.
        order = _make_order(self.make_user("syncreg"))
        with self.captureOnCommitCallbacks(execute=True):
            notifications.dispatch(AuditEvent.EventType.ORDER_PAID, {"order": order})
            self.assertEqual(len(mail.outbox), 1)

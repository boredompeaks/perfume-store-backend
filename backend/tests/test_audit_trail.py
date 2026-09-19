"""SPEC-7-01: append-only business-event audit trail ([R-7.20] + S1[1.22]).

- Order lifecycle (created, paid), payment verify outcomes (intent,
  verified, and every failure branch), and authentication events each land
  an ``AuditEvent`` row through the real endpoints.
- Every hook writes inside the same ``transaction.atomic()`` block as the
  side effect it records: when the trail cannot be written, the effect
  rolls back with it (pinned with an injected audit-write failure).
- Rows are append-only: updates and deletes raise at the model level, and
  events outlive the order/user rows they describe (``SET_NULL`` FKs).
"""
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.core import mail

from common.models import AuditEvent
from common.testing import ApiTestCase, extract_link_params
from orders.models import Order
from products.models import StockMovement
from rest_framework.throttling import ScopedRateThrottle


class AuditEventModelTests(ApiTestCase):
    """Model-level contract: category derivation and the append-only guards."""

    def test_record_derives_category_and_stores_payload(self):
        event = AuditEvent.record(
            AuditEvent.EventType.AUTH_REGISTERED,
            detail={"username": "buyer"},
        )
        self.assertEqual(event.category, AuditEvent.Category.AUTH)
        self.assertEqual(event.event_type, "auth.registered")
        self.assertIsNone(event.actor)
        self.assertEqual(event.detail, {"username": "buyer"})
        self.assertEqual(
            str(event), f"{event.created_at:%Y-%m-%d %H:%M:%S} auth.registered"
        )

    def test_record_rejects_unknown_prefix(self):
        # An unprefixed/unknown identifier must fail loudly, not write a
        # row no category query will ever find.
        with self.assertRaises(ValueError):
            AuditEvent.record("bogus.event")

    def test_anonymous_actor_stores_null(self):
        event = AuditEvent.record(
            AuditEvent.EventType.PAYMENT_VERIFIED, actor=AnonymousUser()
        )
        self.assertIsNone(event.actor)

    def test_updates_are_forbidden(self):
        event = AuditEvent.record(AuditEvent.EventType.ORDER_CREATED)
        event.detail = {"tampered": True}
        with self.assertRaises(ValueError):
            event.save()
        event.refresh_from_db()
        self.assertEqual(event.detail, {})

    def test_deletes_are_forbidden(self):
        event = AuditEvent.record(AuditEvent.EventType.ORDER_CREATED)
        with self.assertRaises(ValueError):
            event.delete()
        self.assertTrue(AuditEvent.objects.filter(pk=event.pk).exists())

    def test_events_outlive_deleted_order_and_actor(self):
        """SET_NULL on both FKs: the trail survives the rows it describes,
        with the identifying values preserved in detail."""
        self.make_user("buyer")
        _, token = self.api_login()
        product = self.make_product(stock=10)
        self.seed_session_cart([(product, 2)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order_id = res.data["id"]

        self.auth(None)
        User.objects.get(username="buyer").delete()

        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.ORDER_CREATED
        )
        self.assertIsNone(event.order)
        self.assertIsNone(event.actor)
        self.assertEqual(event.detail["order_id"], order_id)


class OrderPaymentTrailTests(ApiTestCase):
    """order/payment events through the real checkout and payment endpoints."""

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

    def test_order_created_event_names_actor_order_and_total(self):
        order_id = self._checkout()
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.ORDER_CREATED
        )
        self.assertEqual(event.category, AuditEvent.Category.ORDER)
        self.assertEqual(event.actor, self.user)
        self.assertEqual(event.order_id, order_id)
        self.assertEqual(event.detail["order_id"], order_id)
        self.assertEqual(event.detail["total_amount"], "999.98")
        self.assertEqual(event.detail["item_count"], 1)
        self.assertIsNone(event.detail["coupon"])

    def test_checkout_rolls_back_together_with_trail(self):
        """The hook writes inside the checkout's atomic block: an audit
        failure aborts the order too — no phantom trail, no orphan order."""
        self.seed_session_cart([(self.product, 2)])
        self.client.raise_request_exception = False
        with patch.object(AuditEvent, "record", side_effect=RuntimeError("down")):
            res = self.checkout()
        self.assertEqual(res.status_code, 500)
        # The only surviving row is setUp's successful login event: the
        # order and its trail row rolled back together.
        self.assertEqual(AuditEvent.objects.count(), 1)
        self.assertFalse(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_CREATED
            ).exists()
        )
        self.assertEqual(Order.objects.count(), 0)

    def test_payment_initiated_recorded_once_per_intent(self):
        order_id = self._checkout()
        self.razorpay_mock()
        self._start_payment(order_id)
        # The reuse path persists nothing, so it emits nothing.
        self._start_payment(order_id)
        events = AuditEvent.objects.filter(
            event_type=AuditEvent.EventType.PAYMENT_INITIATED
        )
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.category, AuditEvent.Category.PAYMENT)
        self.assertEqual(event.order_id, order_id)
        self.assertEqual(event.detail["razorpay_order_id"], "order_TEST0001")

    def test_verified_payment_writes_payment_and_order_events(self):
        order_id = self._checkout()
        self.razorpay_mock()
        self._start_payment(order_id)
        res = self._verify(order_id)
        self.assertEqual(res.status_code, 200, res.data)

        verified = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.PAYMENT_VERIFIED
        )
        self.assertEqual(verified.order_id, order_id)
        self.assertEqual(verified.detail["razorpay_payment_id"], "pay_TEST0001")
        paid = AuditEvent.objects.get(event_type=AuditEvent.EventType.ORDER_PAID)
        self.assertEqual(paid.order_id, order_id)
        self.assertEqual(paid.detail["total_amount"], "999.98")

    def test_verify_rolls_back_together_with_trail(self):
        """An audit failure in the verify transaction undoes stock, coupon,
        order state and cart cleanup as well — effect and trail never
        disagree."""
        order_id = self._checkout()
        self.razorpay_mock()
        self._start_payment(order_id)
        self.client.raise_request_exception = False
        with patch.object(AuditEvent, "record", side_effect=RuntimeError("down")):
            res = self._verify(order_id)
        self.assertEqual(res.status_code, 500)
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type__in=[
                    AuditEvent.EventType.PAYMENT_VERIFIED,
                    AuditEvent.EventType.ORDER_PAID,
                ]
            ).count(),
            0,
        )
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.status, "pending")
        self.assertIsNone(order.razorpay_payment_id)
        # auth.login (setUp), order.created and payment.initiated survive;
        # the verify's trail rows rolled back with the stock, coupon, order
        # state and cart cleanup.
        self.assertEqual(AuditEvent.objects.count(), 3)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_signature_rejection_is_recorded_and_order_stays_pending(self):
        order_id = self._checkout()
        client_mock = self.razorpay_mock()
        self.razorpay_fail_signature(client_mock)
        res = self._verify(order_id)
        self.assertEqual(res.status_code, 400, res.data)

        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.PAYMENT_SIGNATURE_REJECTED
        )
        # The gateway references are unproven, so no order FK is set.
        self.assertIsNone(event.order)
        self.assertEqual(event.detail["razorpay_payment_id"], "pay_TEST0001")
        self.assertEqual(Order.objects.get(pk=order_id).status, "pending")
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.PAYMENT_VERIFIED
            ).count(),
            0,
        )

    def test_verify_for_unowned_order_is_recorded_without_fk(self):
        self._checkout()
        self.razorpay_mock()
        res = self._verify(999999)
        self.assertEqual(res.status_code, 404, res.data)

        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.PAYMENT_ORDER_NOT_FOUND
        )
        self.assertIsNone(event.order)
        self.assertEqual(event.detail["order_id"], 999999)

    def test_replayed_verify_is_recorded_without_second_success(self):
        order_id = self._checkout()
        self.razorpay_mock()
        self._start_payment(order_id)
        self.assertEqual(self._verify(order_id).status_code, 200)
        res = self._verify(order_id)
        self.assertEqual(res.status_code, 400, res.data)

        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.PAYMENT_ALREADY_PROCESSED
        )
        self.assertEqual(event.order_id, order_id)
        self.assertEqual(event.detail["order_status"], "confirmed")
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.PAYMENT_VERIFIED
            ).count(),
            1,
        )

    def test_reference_mismatch_verify_is_recorded(self):
        order_id = self._checkout()
        self.razorpay_mock()
        self._start_payment(order_id)
        res = self._verify(order_id, razorpay_order_id="order_OTHER")
        self.assertEqual(res.status_code, 400, res.data)

        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.PAYMENT_REFERENCE_MISMATCH
        )
        self.assertEqual(event.order_id, order_id)
        self.assertEqual(event.detail["claimed_razorpay_order_id"], "order_OTHER")
        self.assertEqual(Order.objects.get(pk=order_id).status, "pending")

    def test_stock_conflict_verify_is_recorded(self):
        order_id = self._checkout()
        self.razorpay_mock()
        self._start_payment(order_id)
        # Another buyer drains the stock between checkout and verify; the
        # lock-protected backstop refuses the sale and the trail keeps it.
        self.product.refresh_from_db()
        self.product.stock = 0
        self.product.save(update_fields=["stock"])

        res = self._verify(order_id)
        self.assertEqual(res.status_code, 409, res.data)

        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.PAYMENT_STOCK_CONFLICT
        )
        self.assertEqual(event.order_id, order_id)
        self.assertEqual(event.detail["requested"], 2)
        self.assertEqual(event.detail["available"], 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_coupon_invalidation_between_checkout_and_verify_is_recorded(self):
        coupon = self.make_coupon(discount_value="10")
        order_id = self._checkout(coupon_code="SAVE10")
        self.razorpay_mock()
        self._start_payment(order_id)
        coupon.active = False
        coupon.save(update_fields=["active"])

        res = self._verify(order_id)
        self.assertEqual(res.status_code, 409, res.data)

        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.PAYMENT_COUPON_INVALID
        )
        self.assertEqual(event.order_id, order_id)
        self.assertEqual(event.detail["coupon_id"], coupon.pk)
        self.assertEqual(Order.objects.get(pk=order_id).status, "pending")


class AuthTrailTests(ApiTestCase):
    """auth events through the real registration, login and recovery flows."""

    def test_register_writes_registration_event(self):
        self.register_and_verify(username="audited-buyer")
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.AUTH_REGISTERED
        )
        self.assertEqual(event.category, AuditEvent.Category.AUTH)
        self.assertEqual(event.actor.username, "audited-buyer")
        self.assertEqual(event.detail["username"], "audited-buyer")

    def test_login_success_and_failure_each_leave_one_event(self):
        self.make_user("buyer")
        res = self.client.post(
            "/api/accounts/login/",
            {"username": "buyer", "password": "S3cure-Passphrase!"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        ok = AuditEvent.objects.get(event_type=AuditEvent.EventType.AUTH_LOGIN)
        self.assertEqual(ok.actor.username, "buyer")

        res = self.client.post(
            "/api/accounts/login/",
            {"username": "buyer", "password": "wrong-pass"},
            format="json",
        )
        self.assertEqual(res.status_code, 401)
        # A malformed body (400) is also a rejected attempt, not a success.
        res = self.client.post(
            "/api/accounts/login/", {"username": "buyer"}, format="json"
        )
        self.assertEqual(res.status_code, 400)

        failed = AuditEvent.objects.filter(
            event_type=AuditEvent.EventType.AUTH_LOGIN_FAILED
        )
        self.assertEqual(failed.count(), 2)
        for event in failed:
            self.assertEqual(event.detail["username"], "buyer")

    def test_login_with_unknown_username_records_attempt(self):
        res = self.client.post(
            "/api/accounts/login/",
            {"username": "ghost", "password": "whatever-1A"},
            format="json",
        )
        self.assertEqual(res.status_code, 401)
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.AUTH_LOGIN_FAILED
        )
        self.assertIsNone(event.actor)
        self.assertEqual(event.detail["username"], "ghost")

    @staticmethod
    def _throttle_auth_at_3_per_min():
        # DRF snapshots DEFAULT_THROTTLE_RATES into the throttle class at
        # import, so the rate is pinned on the class directly: the assertion
        # cannot drift with a deployment's THROTTLE_AUTH_RATE setting.
        return patch.object(
            ScopedRateThrottle,
            "THROTTLE_RATES",
            {
                "auth": "3/min",
                "coupon": "10/min",
                "cart": "60/min",
                "recovery": "5/min",
            },
        )

    def test_throttled_login_writes_no_event(self):
        """A 429 refusal is the rate limit doing its job before the view
        runs — an auth attempt that never happened writes nothing."""
        self.make_user("buyer")
        with self._throttle_auth_at_3_per_min():
            for _ in range(3):
                res = self.client.post(
                    "/api/accounts/login/",
                    {"username": "buyer", "password": "wrong-pass"},
                    format="json",
                )
                self.assertEqual(res.status_code, 401)
            res = self.client.post(
                "/api/accounts/login/",
                {"username": "buyer", "password": "wrong-pass"},
                format="json",
            )
            self.assertEqual(res.status_code, 429)
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.AUTH_LOGIN_FAILED
            ).count(),
            3,
        )
        self.assertFalse(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.AUTH_LOGIN
            ).exists()
        )

    def test_email_verified_event_written_once(self):
        user = self.register_and_verify(username="verifier")
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.AUTH_EMAIL_VERIFIED
        )
        self.assertEqual(event.actor, user)

        # Re-verifying an already-active account is a no-op: no event.
        uid, token = extract_link_params(mail.outbox[0].body, "verify-email")
        res = self.client.post(
            "/api/accounts/verify-email/",
            {"uid": uid, "token": token},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.AUTH_EMAIL_VERIFIED
            ).count(),
            1,
        )

    def test_password_reset_writes_reset_event(self):
        user = self.make_user("resetter")
        res = self.client.post(
            "/api/accounts/password-reset/",
            {"email": "resetter@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 1)
        uid, token = extract_link_params(mail.outbox[0].body, "reset-password")
        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "N3w-Passphrase!"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.AUTH_PASSWORD_RESET
        )
        self.assertEqual(event.actor, user)
        self.assertEqual(event.detail["username"], "resetter")

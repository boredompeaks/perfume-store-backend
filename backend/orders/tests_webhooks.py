import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.db import IntegrityError, transaction
from django.db.models.query import QuerySet
from django.test import override_settings
from django.utils import timezone
from rest_framework.permissions import AllowAny

from common import notifications
from common.models import AuditEvent, NotificationOutbox
from common.testing import ApiTestCase
from orders.inventory import StockUnavailable, commit_order_sale
from orders.models import (
    Coupon,
    Order,
    OrderItem,
    OrderStatusEvent,
    PaymentEvent,
    Refund,
)
from orders.state import (
    CAPTURED_MONEY_PAYMENT_STATUSES,
    CAPTURED_SALE_PAYMENT_STATUSES,
    TRIGGER_PAYMENT_WEBHOOK,
    status_for_payment,
)
from orders.webhooks import razorpay_webhook
from products.models import StockMovement, StockReservation, products

WEBHOOK_URL = "/api/v1/webhooks/razorpay/"
WEBHOOK_SECRET = "TESTINGONLY-WEBHOOK-SECRET-DO-NOT-USE"


@override_settings(RAZORPAY_WEBHOOK_SECRET=WEBHOOK_SECRET)
class WebhookTestCase(ApiTestCase):
    def sign(self, raw_body):
        return hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

    def deliver(self, body, event_id="evt_TEST0001", signature=None, raw=None):
        payload = raw if raw is not None else json.dumps(body).encode("utf-8")
        headers = {}
        if event_id is not None:
            headers["HTTP_X_RAZORPAY_EVENT_ID"] = event_id
        headers["HTTP_X_RAZORPAY_SIGNATURE"] = (
            self.sign(payload) if signature is None else signature
        )
        return self.client.post(
            WEBHOOK_URL,
            data=payload,
            content_type="application/json",
            **headers,
        )

    def make_pending_order(
        self,
        total="1200.50",
        gateway_order="order_TEST9",
        gateway_payment=None,
        username="buyer",
    ):
        user = self.make_user(username=username)
        order = Order.objects.create(
            user=user,
            full_name="Buyer Person",
            phone="9876543210",
            address="12 Rose Lane",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal(total),
        )
        order.razorpay_order_id = gateway_order
        order.razorpay_payment_id = gateway_payment
        order.save(update_fields=["razorpay_order_id", "razorpay_payment_id"])
        return order

    def capture_event(
        self,
        order,
        payment_id="pay_TEST9",
        amount_minor=120050,
        event="payment.captured",
    ):
        return {
            "entity": "event",
            "account_id": "acc_TEST",
            "event": event,
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": "payment",
                    "id": payment_id,
                    "amount": amount_minor,
                    "currency": "INR",
                    "order_id": order.razorpay_order_id,
                    "captured": True,
                    "status": "captured",
                }
            },
        }

    def hold_stock(self, order, quantity=1, status=StockReservation.Status.ACTIVE):
        return StockReservation.objects.create(
            product=self.make_product(stock=10),
            order=order,
            owner=order.user,
            quantity=quantity,
            status=status,
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def add_item(self, order, product=None, quantity=2):
        product = self.make_product(stock=10) if product is None else product
        OrderItem.objects.create(
            order=order,
            product=product,
            product_name=product.name,
            price=product.price,
            quantity=quantity,
            subtotal=product.price * quantity,
        )
        return product

    def hold_for(self, order, product, quantity=2):
        return StockReservation.objects.create(
            product=product,
            order=order,
            owner=order.user,
            quantity=quantity,
            status=StockReservation.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def attach_coupon(self, order, coupon):
        order.coupon = coupon
        order.save(update_fields=["coupon"])
        return order

    def _verify(self, order):
        return self.client.post(
            "/api/orders/payment/verify/",
            {
                "order_id": order.id,
                "razorpay_order_id": order.razorpay_order_id,
                "razorpay_payment_id": "pay_TEST9",
                "razorpay_signature": "sig",
            },
            format="json",
        )


class SignatureVerificationTests(WebhookTestCase):
    def test_valid_signature_reconciles_the_captured_payment(self):
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.fulfilment_status, "unfulfilled")
        self.assertEqual(order.razorpay_payment_id, "pay_TEST9")
        self.assertIsNotNone(order.paid_at)
        event = PaymentEvent.objects.get(event_id="evt_TEST0001")
        self.assertEqual(event.outcome, PaymentEvent.Outcome.APPLIED)
        self.assertEqual(event.order, order)
        self.assertEqual(event.amount, Decimal("1200.50"))
        self.assertEqual(str(event), "payment.captured evt_TEST0001 (applied)")

    def test_invalid_signature_is_refused_and_moves_no_money(self):
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order), signature="deadbeef")

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertEqual(order.status, "pending")
        self.assertIsNone(order.razorpay_payment_id)
        self.assertIsNone(order.paid_at)
        self.assertFalse(PaymentEvent.objects.exists())

    def test_missing_signature_header_is_refused(self):
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order), signature="")

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertFalse(PaymentEvent.objects.exists())

    def test_signature_over_different_bytes_is_refused(self):
        # The signature covers the raw body, so a payload edited after signing (or a signature
        # copied from another delivery) does not verify.
        order = self.make_pending_order()
        body = self.capture_event(order)
        signature = self.sign(json.dumps(body).encode("utf-8"))
        body["payload"]["payment"]["amount"] = 1

        response = self.deliver(body, signature=signature)

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_unsigned_forged_payload_cannot_mark_an_order_paid(self):
        order = self.make_pending_order()

        response = self.client.post(
            WEBHOOK_URL,
            data=json.dumps(self.capture_event(order)).encode("utf-8"),
            content_type="application/json",
            HTTP_X_RAZORPAY_EVENT_ID="evt_FORGED",
        )

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertEqual(order.status, "pending")
        self.assertIsNone(order.razorpay_payment_id)
        self.assertFalse(OrderStatusEvent.objects.exists())

    @override_settings(RAZORPAY_WEBHOOK_SECRET="")
    def test_unconfigured_secret_fails_closed(self):
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 503)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertFalse(PaymentEvent.objects.exists())

    def test_delivery_without_an_event_id_is_refused(self):
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order), event_id=None)

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertFalse(PaymentEvent.objects.exists())

    def test_non_ascii_signature_header_is_refused_not_crashed(self):
        # compare_digest raises TypeError on a non-ASCII str, so comparing the header verbatim would
        # let an anonymous caller force a 500 on a money endpoint (and a 500 makes the provider retry).
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order), signature="é" * 64)

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertFalse(PaymentEvent.objects.exists())

    def test_signature_with_surrounding_whitespace_is_refused(self):
        # The provider signs and compares a hex digest literally; a value with a trailing newline is
        # not what it sent.
        order = self.make_pending_order()
        payload = json.dumps(self.capture_event(order)).encode("utf-8")

        response = self.deliver(
            self.capture_event(order),
            signature=self.sign(payload) + "\n",
        )

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_signature_of_the_wrong_length_is_refused(self):
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order), signature="ab")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PaymentEvent.objects.exists())


class ReplayProtectionTests(WebhookTestCase):
    def test_replayed_event_id_has_no_second_effect(self):
        order = self.make_pending_order()
        body = self.capture_event(order)

        first = self.deliver(body)
        second = self.deliver(body)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(PaymentEvent.objects.count(), 1)
        self.assertEqual(OrderStatusEvent.objects.count(), 1)
        self.assertEqual(
            OrderStatusEvent.objects.get().trigger,
            TRIGGER_PAYMENT_WEBHOOK,
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_PAID
            ).count(),
            1,
        )
        # One customer notification, not two: the outbox dedup_key collapses the second delivery's
        # enqueue, and nothing is sent on the delivery thread.
        self.assertEqual(
            NotificationOutbox.objects.filter(
                event_type=AuditEvent.EventType.ORDER_PAID
            ).count(),
            1,
        )
        self.assertEqual(len(mail.outbox), 0)
        notifications.drain_notifications()
        self.assertEqual(len(mail.outbox), 1)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")

    def test_replay_converts_no_further_stock_holds(self):
        order = self.make_pending_order()
        hold = self.hold_stock(order, quantity=2)
        body = self.capture_event(order)

        self.deliver(body)
        self.deliver(body)

        hold.refresh_from_db()
        self.assertEqual(hold.status, StockReservation.Status.CONVERTED)
        self.assertEqual(
            StockReservation.objects.filter(
                order=order,
                status=StockReservation.Status.CONVERTED,
            ).count(),
            1,
        )

    def test_second_distinct_event_for_an_already_captured_order_is_refused(self):
        order = self.make_pending_order()
        self.deliver(self.capture_event(order))
        order.refresh_from_db()
        paid_at = order.paid_at

        response = self.deliver(self.capture_event(order), event_id="evt_TEST0002")

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.paid_at, paid_at)
        self.assertEqual(OrderStatusEvent.objects.count(), 1)
        second = PaymentEvent.objects.get(event_id="evt_TEST0002")
        self.assertEqual(second.outcome, PaymentEvent.Outcome.REFUSED)
        self.assertEqual(PaymentEvent.objects.count(), 2)


class CaptureReconciliationTests(WebhookTestCase):
    def test_capture_event_for_an_unknown_order_is_recorded_and_refused(self):
        # A validly-signed event about a gateway order this store never recorded is still a fact about
        # a payment: it is kept, with no order to attach, and moves nothing.
        order = self.make_pending_order()
        body = self.capture_event(order)
        body["payload"]["payment"]["order_id"] = "order_NOT_OURS"

        response = self.deliver(body, event_id="evt_TEST0009")

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get(event_id="evt_TEST0009")
        self.assertEqual(event.outcome, PaymentEvent.Outcome.REFUSED)
        self.assertIsNone(event.order)
        self.assertEqual(event.gateway_payment_id, "pay_TEST9")
        self.assertEqual(event.amount, Decimal("1200.50"))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertFalse(OrderStatusEvent.objects.exists())

    def test_event_naming_a_different_payment_is_recorded_and_refused(self):
        order = self.make_pending_order(gateway_payment="pay_REAL")
        body = self.capture_event(order, payment_id="pay_IMPOSTOR")

        response = self.deliver(body)

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.outcome, PaymentEvent.Outcome.REFUSED)
        self.assertEqual(event.order, order)
        order.refresh_from_db()
        self.assertEqual(order.razorpay_payment_id, "pay_REAL")
        self.assertEqual(order.payment_status, "pending")

    def test_capture_for_a_different_amount_is_recorded_and_refused(self):
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order, amount_minor=999))

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.outcome, PaymentEvent.Outcome.REFUSED)
        self.assertEqual(event.amount, Decimal("9.99"))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertIsNone(order.paid_at)

    def test_capture_of_a_cancelled_order_is_recorded_and_refused(self):
        order = self.make_pending_order()
        order.status = "cancelled"
        order.cancelled_at = order.created_at
        order.save(update_fields=["status", "cancelled_at"])

        response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            PaymentEvent.objects.get().outcome, PaymentEvent.Outcome.REFUSED
        )
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_capture_with_a_malformed_entity_is_recorded_and_refused(self):
        order = self.make_pending_order()
        cases = [
            {"entity": "payment", "id": "pay_X", "amount": 120050},
            {
                "entity": "payment",
                "order_id": order.razorpay_order_id,
                "amount": 120050,
            },
            {
                "entity": "payment",
                "order_id": order.razorpay_order_id,
                "id": "pay_X",
                "amount": "1200.50",
            },
        ]

        for index, payment in enumerate(cases):
            with self.subTest(index=index):
                response = self.deliver(
                    {"event": "payment.captured", "payload": {"payment": payment}},
                    event_id=f"evt_MALFORMED{index}",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    PaymentEvent.objects.get(event_id=f"evt_MALFORMED{index}").outcome,
                    PaymentEvent.Outcome.REFUSED,
                )

        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertEqual(PaymentEvent.objects.count(), 3)
        self.assertEqual(
            PaymentEvent.objects.get(event_id="evt_MALFORMED2").amount,
            None,
        )

    def test_capture_with_a_negative_amount_is_recorded_and_refused(self):
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order, amount_minor=-100))

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.outcome, PaymentEvent.Outcome.REFUSED)
        self.assertIsNone(event.amount)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_database_failure_that_is_not_a_replay_is_surfaced_for_retry(self):
        # An IntegrityError that is not the event_id collision cannot be classified as a replay, so it
        # is raised instead of acknowledged -- a 5xx tells the provider to redeliver.
        order = self.make_pending_order()
        self.client.raise_request_exception = False
        self.addCleanup(setattr, self.client, "raise_request_exception", True)

        with patch.object(
            PaymentEvent.objects,
            "create",
            side_effect=IntegrityError("some other constraint"),
        ):
            response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 500)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertFalse(PaymentEvent.objects.exists())

    def test_capture_converted_the_orders_live_stock_holds(self):
        order = self.make_pending_order()
        active = self.hold_stock(order)
        released = self.hold_stock(order, status=StockReservation.Status.RELEASED)

        self.deliver(self.capture_event(order))

        active.refresh_from_db()
        released.refresh_from_db()
        self.assertEqual(active.status, StockReservation.Status.CONVERTED)
        self.assertEqual(released.status, StockReservation.Status.RELEASED)

    def test_the_customer_is_notified_by_the_reconciled_capture(self):
        order = self.make_pending_order()

        self.deliver(self.capture_event(order))

        self.assertEqual(len(mail.outbox), 0)
        row = NotificationOutbox.objects.get(event_type=AuditEvent.EventType.ORDER_PAID)
        self.assertEqual(row.status, NotificationOutbox.Status.PENDING)
        notifications.drain_notifications()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(str(order.id), mail.outbox[0].subject)

    def test_the_outbox_row_is_written_inside_the_capture_transaction(self):
        """Rollback-together: the outbox row is written inside the capture transaction, so it disappears with it."""
        order = self.make_pending_order()

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.deliver(self.capture_event(order))
                self.assertEqual(
                    NotificationOutbox.objects.filter(
                        event_type=AuditEvent.EventType.ORDER_PAID
                    ).count(),
                    1,
                )
                raise RuntimeError("force rollback after the enqueue")
        self.assertEqual(NotificationOutbox.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_an_enqueue_failure_never_breaks_the_capture(self):
        order = self.make_pending_order()

        with patch(
            "orders.webhooks.notifications.enqueue",
            side_effect=RuntimeError("outbox unavailable"),
        ):
            response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    def test_capture_audit_rows_name_the_delivery(self):
        order = self.make_pending_order()

        self.deliver(self.capture_event(order))

        verified = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.PAYMENT_VERIFIED
        )
        self.assertEqual(verified.detail["event_id"], "evt_TEST0001")
        self.assertEqual(verified.detail["razorpay_payment_id"], "pay_TEST9")
        paid = AuditEvent.objects.get(event_type=AuditEvent.EventType.ORDER_PAID)
        self.assertEqual(paid.order, order)
        self.assertEqual(paid.detail["total_amount"], "1200.50")


class WebhookSaleCommitTests(WebhookTestCase):
    """A capture with no browser callback behind it must still decrement stock and write the ledger, not just convert the holds."""

    def test_capture_decrements_the_stock_its_order_sold(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)

        self.deliver(self.capture_event(order))

        product.refresh_from_db()
        self.assertEqual(product.stock, 8)

    def test_capture_writes_a_sale_ledger_row_for_each_line(self):
        # stock_after is the REAL post-decrement quantity and the actor is the system, not the buyer:
        # naming the customer as actor would misattribute the mutation.
        order = self.make_pending_order()
        product = self.add_item(order, quantity=3)

        self.deliver(self.capture_event(order))

        movements = list(StockMovement.objects.all())
        self.assertEqual(len(movements), 1)
        movement = movements[0]
        self.assertEqual(movement.product_id, product.id)
        self.assertEqual(movement.delta, -3)
        self.assertEqual(movement.reason, StockMovement.Reason.SALE)
        self.assertEqual(movement.stock_after, 7)
        self.assertIsNone(movement.created_by)
        self.assertEqual(movement.note, f"Order #{order.id}")

    def test_capture_commits_every_line_of_a_multi_line_order(self):
        order = self.make_pending_order()
        first = self.add_item(order, quantity=1)
        second = self.make_product(name="Oud Royale", stock=4)
        self.add_item(order, product=second, quantity=2)

        self.deliver(self.capture_event(order))

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.stock, 9)
        self.assertEqual(second.stock, 2)
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_capture_commits_the_sale_of_a_line_whose_product_row_is_gone(self):
        # OrderItem.product is on_delete=SET_NULL, so the line survives a catalogue delete; it must
        # still be refused as a shortage rather than silently skipped.
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)
        product.delete()

        self.deliver(self.capture_event(order))

        self.assertEqual(
            PaymentEvent.objects.get().outcome, PaymentEvent.Outcome.REFUSED
        )
        self.assertEqual(StockMovement.objects.count(), 0)


class WebhookStockSufficiencyTests(WebhookTestCase):
    def test_capture_with_stock_gone_sells_nothing_and_is_recorded(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=4)
        products.objects.filter(pk=product.pk).update(stock=1)

        response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            PaymentEvent.objects.get().outcome, PaymentEvent.Outcome.REFUSED
        )
        product.refresh_from_db()
        self.assertEqual(product.stock, 1)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_the_stock_conflict_is_recorded_for_reconciliation(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=4)
        products.objects.filter(pk=product.pk).update(stock=1)

        self.deliver(self.capture_event(order), event_id="evt_RACE1")

        conflict = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.PAYMENT_STOCK_CONFLICT
        )
        self.assertEqual(conflict.order, order)
        self.assertEqual(conflict.detail["product_id"], product.id)
        self.assertEqual(conflict.detail["requested"], 4)
        self.assertEqual(conflict.detail["available"], 1)
        self.assertEqual(conflict.detail["event_id"], "evt_RACE1")

    def test_an_unsellable_capture_does_not_convert_the_holds(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=4)
        hold = self.hold_for(order, product, quantity=4)
        products.objects.filter(pk=product.pk).update(stock=1)

        self.deliver(self.capture_event(order))

        hold.refresh_from_db()
        self.assertEqual(hold.status, StockReservation.Status.RELEASED)

    def test_a_conflict_does_not_strand_the_order_as_paid(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=4)
        products.objects.filter(pk=product.pk).update(stock=1)

        self.deliver(self.capture_event(order))

        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertIsNone(order.paid_at)


class SharedSaleCommitIdempotencyTests(WebhookTestCase):
    def _capture_after_callback(self, order):
        self.api_login()
        self.razorpay_mock(order_id=order.razorpay_order_id)
        verify = self._verify(order)
        self.assertEqual(verify.status_code, 200, verify.data)
        return self.deliver(self.capture_event(order), event_id="evt_AFTER")

    def test_a_delivery_after_the_customer_callback_decrements_once(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)

        self._capture_after_callback(order)

        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_a_delivery_after_the_customer_callback_writes_no_second_ledger_row(
        self,
    ):
        order = self.make_pending_order()
        self.add_item(order, quantity=2)

        self._capture_after_callback(order)

        movement = StockMovement.objects.get()
        self.assertEqual(movement.delta, -2)
        self.assertEqual(movement.stock_after, 8)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_a_replayed_delivery_after_the_callback_records_its_refusal(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)

        response = self._capture_after_callback(order)

        self.assertEqual(response.status_code, 200)
        second = PaymentEvent.objects.get(event_id="evt_AFTER")
        self.assertEqual(second.outcome, PaymentEvent.Outcome.REFUSED)
        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_the_callback_after_a_delivery_decrements_once(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)
        self.api_login()
        self.razorpay_mock(order_id=order.razorpay_order_id)

        self.assertEqual(self.deliver(self.capture_event(order)).status_code, 200)
        verify = self._verify(order)

        self.assertEqual(verify.status_code, 400, verify.data)
        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        self.assertEqual(StockMovement.objects.count(), 1)


class RefundEventTests(WebhookTestCase):
    def test_refund_event_is_recorded_without_issuing_a_refund(self):
        order = self.make_pending_order(gateway_payment="pay_TEST9")
        order.payment_status = "captured"
        order.status = "confirmed"
        order.save(update_fields=["payment_status", "status"])

        response = self.deliver(
            {
                "event": "payment.refunded",
                "payload": {
                    "refund": {
                        "id": "rfnd_TEST1",
                        "payment_id": "pay_TEST9",
                        "amount": 120050,
                        "status": "processed",
                    }
                },
            }
        )

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.outcome, PaymentEvent.Outcome.RECORDED)
        self.assertEqual(event.order, order)
        self.assertEqual(event.gateway_payment_id, "pay_TEST9")
        self.assertEqual(event.amount, Decimal("1200.50"))
        self.assertFalse(Refund.objects.exists())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertIsNone(order.refunded_at)

    def test_provider_spelling_of_a_settled_refund_is_also_recorded(self):
        order = self.make_pending_order(gateway_payment="pay_TEST9")

        response = self.deliver(
            {
                "event": "refund.processed",
                "payload": {
                    "refund": {
                        "id": "rfnd_TEST2",
                        "payment_id": "pay_TEST9",
                        "amount": 5000,
                    }
                },
            },
            event_id="evt_REFUND2",
        )

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.outcome, PaymentEvent.Outcome.RECORDED)
        self.assertEqual(event.order, order)
        self.assertEqual(event.gateway_payment_id, "pay_TEST9")
        self.assertEqual(event.amount, Decimal("50.00"))
        self.assertFalse(Refund.objects.exists())

    def test_refund_event_without_a_payment_reference_is_recorded_bare(self):
        response = self.deliver(
            {"event": "payment.refunded", "payload": {"refund": {"id": "rfnd_X"}}},
            event_id="evt_REFUND3",
        )

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.gateway_payment_id, "")
        self.assertIsNone(event.amount)
        self.assertIsNone(event.order)
        self.assertFalse(Refund.objects.exists())


class UnhandledEventTests(WebhookTestCase):
    def test_unhandled_event_type_is_recorded_and_moves_nothing(self):
        order = self.make_pending_order()

        response = self.deliver(
            {"event": "subscription.charged", "payload": {"subscription": {}}},
            event_id="evt_OTHER1",
        )

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.outcome, PaymentEvent.Outcome.RECORDED)
        self.assertEqual(event.event_type, "subscription.charged")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_payload_without_an_event_type_is_recorded_bare(self):
        response = self.deliver({"entity": "event"}, event_id="evt_OTHER2")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            PaymentEvent.objects.get().outcome,
            PaymentEvent.Outcome.RECORDED,
        )


class MalformedDeliveryTests(WebhookTestCase):
    def test_oversized_event_id_is_refused_and_writes_no_row(self):
        # event_id is the replay key AND a bounded column: an oversized one is refused rather than
        # truncated, because SQLite would accept it and Postgres would raise DataError.
        order = self.make_pending_order()
        oversized = "evt_" + ("X" * PaymentEvent._meta.get_field("event_id").max_length)

        response = self.deliver(self.capture_event(order), event_id=oversized)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PaymentEvent.objects.exists())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_oversized_event_type_is_refused_and_writes_no_row(self):
        order = self.make_pending_order()
        body = self.capture_event(order)
        body["event"] = "e" * (
            PaymentEvent._meta.get_field("event_type").max_length + 1
        )

        response = self.deliver(body)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PaymentEvent.objects.exists())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_oversized_payment_reference_is_recorded_and_refused(self):
        order = self.make_pending_order()
        oversized = "pay_" + (
            "Y" * PaymentEvent._meta.get_field("gateway_payment_id").max_length
        )

        response = self.deliver(self.capture_event(order, payment_id=oversized))

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.outcome, PaymentEvent.Outcome.REFUSED)
        self.assertEqual(event.gateway_payment_id, "")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertIsNone(order.razorpay_payment_id)

    def test_oversized_refund_reference_is_recorded_with_no_reference(self):
        self.make_pending_order(gateway_payment="pay_TEST9")
        oversized = "pay_" + (
            "Z" * PaymentEvent._meta.get_field("gateway_payment_id").max_length
        )

        response = self.deliver(
            {
                "event": "payment.refunded",
                "payload": {"refund": {"id": "rfnd_X", "payment_id": oversized}},
            },
            event_id="evt_REFUND_BIG",
        )

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.outcome, PaymentEvent.Outcome.RECORDED)
        self.assertEqual(event.gateway_payment_id, "")
        self.assertIsNone(event.order)
        self.assertFalse(Refund.objects.exists())

    def test_signed_but_non_json_body_is_refused(self):
        response = self.deliver(None, event_id="evt_BAD1", raw=b"not json at all")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PaymentEvent.objects.exists())

    def test_signed_but_non_object_json_body_is_refused(self):
        response = self.deliver(None, event_id="evt_BAD2", raw=b"[1, 2, 3]")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PaymentEvent.objects.exists())

    def test_signed_delivery_with_an_undecodable_body_is_refused(self):
        response = self.deliver(None, event_id="evt_BAD3", raw=b"\xff\xfe\x00")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PaymentEvent.objects.exists())


class EndpointShapeTests(WebhookTestCase):
    def test_delivery_needs_no_session_cookie_and_no_csrf_token(self):
        # The gateway holds no session cookie and cannot fetch a csrftoken, so the endpoint is past the
        # CSRF gate; a session cookie alone must not turn a signed delivery into a 403.
        order = self.make_pending_order()
        self.client.force_login(self.make_user(username="session-holder"))

        response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")

    def test_endpoint_declares_no_credential_class(self):
        self.assertEqual(razorpay_webhook.cls.authentication_classes, [])
        self.assertIn(
            AllowAny,
            razorpay_webhook.cls.permission_classes,
        )
        self.assertEqual(razorpay_webhook.cls.throttle_scope, "webhook")

    def test_get_is_not_allowed(self):
        response = self.client.get(WEBHOOK_URL)

        self.assertEqual(response.status_code, 405)


class PaymentMachineHelperTests(ApiTestCase):
    def test_status_for_payment_names_the_earliest_status_it_implies(self):
        self.assertEqual(status_for_payment("captured"), "confirmed")
        self.assertEqual(status_for_payment("pending"), "pending")

    def test_status_for_payment_is_none_where_the_legacy_status_cannot_say_it(self):
        self.assertIsNone(status_for_payment("refunded"))
        self.assertIsNone(status_for_payment("failed"))


class _LockedReadRecorder:
    """Lock-requesting reads are pinned on the QUERY, not SQL text: SQLite never emits FOR UPDATE, so an SQL-text pin would pass on Postgres and fail on SQLite for an unrelated reason."""

    def __enter__(self):
        self.locked_reads = []
        self._original = QuerySet._fetch_all

        def recording_fetch_all(queryset):
            if queryset.query.select_for_update:
                self.locked_reads.append(
                    (queryset.model._meta.db_table, tuple(queryset.query.order_by))
                )
            return self._original(queryset)

        self._patcher = patch.object(QuerySet, "_fetch_all", recording_fetch_all)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info):
        self._patcher.stop()
        return False

    def for_table(self, table):
        return [order_by for name, order_by in self.locked_reads if name == table]


class WebhookCouponUsageTests(WebhookTestCase):
    def test_capture_consumes_the_coupons_usage(self):
        order = self.make_pending_order()
        coupon = self.make_coupon(code="WEBHOOK10", usage_limit=1)
        self.add_item(order, quantity=1)
        self.attach_coupon(order, coupon)

        self.deliver(self.capture_event(order))

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)

    def test_capture_does_not_consume_usage_it_cannot_sell(self):
        order = self.make_pending_order()
        coupon = self.make_coupon(code="WEBHOOK20", usage_limit=1)
        product = self.add_item(order, quantity=4)
        self.attach_coupon(order, coupon)
        products.objects.filter(pk=product.pk).update(stock=1)

        self.deliver(self.capture_event(order))

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 0)

    def test_two_webhook_confirmed_orders_consume_two_uses_of_one_code(self):
        coupon = self.make_coupon(code="SINGLEUSE", usage_limit=1)
        first = self.make_pending_order()
        self.add_item(first, quantity=1)
        self.attach_coupon(first, coupon)
        second = self.make_pending_order(
            gateway_order="order_TEST10", username="second-buyer"
        )
        self.add_item(second, quantity=1)
        self.attach_coupon(second, coupon)

        self.deliver(self.capture_event(first))
        self.deliver(
            self.capture_event(second, payment_id="pay_TEST10"),
            event_id="evt_SECOND",
        )

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 2)
        self.assertEqual(PaymentEvent.objects.count(), 2)

    def test_an_order_without_a_coupon_consumes_nothing(self):
        order = self.make_pending_order()
        self.add_item(order, quantity=1)

        self.deliver(self.capture_event(order))

        self.assertEqual(Coupon.objects.count(), 0)
        self.assertEqual(
            PaymentEvent.objects.get().outcome, PaymentEvent.Outcome.APPLIED
        )

    def test_a_deleted_coupon_leaves_the_order_committable(self):
        # Order.coupon is on_delete=SET_NULL, so deleting the coupon must not make the order
        # unsellable -- the service skips a NULL coupon rather than raising.
        order = self.make_pending_order()
        coupon = self.make_coupon(code="GONE10")
        product = self.add_item(order, quantity=1)
        self.attach_coupon(order, coupon)
        coupon.delete()

        self.deliver(self.capture_event(order))

        product.refresh_from_db()
        self.assertEqual(product.stock, 9)
        self.assertEqual(
            PaymentEvent.objects.get().outcome, PaymentEvent.Outcome.APPLIED
        )


class WebhookCouponIdempotencyTests(WebhookTestCase):
    def setUp(self):
        super().setUp()
        self.coupon = self.make_coupon(code="ONCEONLY", usage_limit=1)

    def _callback_then_delivery(self, order):
        self.api_login()
        self.razorpay_mock(order_id=order.razorpay_order_id)
        verify = self._verify(order)
        self.assertEqual(verify.status_code, 200, verify.data)
        return self.deliver(self.capture_event(order), event_id="evt_AFTER")

    def test_a_delivery_after_the_callback_does_not_consume_twice(self):
        order = self.make_pending_order()
        self.add_item(order, quantity=1)
        self.attach_coupon(order, self.coupon)

        self._callback_then_delivery(order)

        self.coupon.refresh_from_db()
        self.assertEqual(self.coupon.used_count, 1)

    def test_the_callback_after_a_delivery_does_not_consume_twice(self):
        order = self.make_pending_order()
        self.add_item(order, quantity=1)
        self.attach_coupon(order, self.coupon)
        self.api_login()
        self.razorpay_mock(order_id=order.razorpay_order_id)

        self.assertEqual(self.deliver(self.capture_event(order)).status_code, 200)
        verify = self._verify(order)

        self.assertEqual(verify.status_code, 400, verify.data)
        self.coupon.refresh_from_db()
        self.assertEqual(self.coupon.used_count, 1)

    def test_a_replayed_delivery_after_the_callback_consumes_nothing_more(self):
        order = self.make_pending_order()
        self.add_item(order, quantity=1)
        self.attach_coupon(order, self.coupon)

        self._callback_then_delivery(order)

        self.coupon.refresh_from_db()
        self.assertEqual(self.coupon.used_count, 1)
        self.assertEqual(
            PaymentEvent.objects.get(event_id="evt_AFTER").outcome,
            PaymentEvent.Outcome.REFUSED,
        )


class SaleCommitCouponTests(WebhookTestCase):
    def test_committing_twice_consumes_the_coupon_once(self):
        order = self.make_pending_order()
        coupon = self.make_coupon(code="TWICE", usage_limit=1)
        self.add_item(order, quantity=1)
        self.attach_coupon(order, coupon)

        commit_order_sale(order)
        order.payment_status = "captured"
        order.save(update_fields=["payment_status"])
        commit_order_sale(order)

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)

    def test_a_reused_coupon_is_consumed_per_sale_not_once_ever(self):
        coupon = self.make_coupon(code="REUSABLE")
        first = self.make_pending_order()
        self.add_item(first, quantity=1)
        self.attach_coupon(first, coupon)
        second = self.make_pending_order(
            gateway_order="order_TEST11", username="second-buyer"
        )
        self.add_item(second, quantity=1)
        self.attach_coupon(second, coupon)

        commit_order_sale(first)
        commit_order_sale(second)

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 2)

    def test_the_coupon_is_locked_after_the_products_not_before(self):
        # ABBA: both capture writers take the Coupon inside the service, so its lock order must be
        # products first, coupon second -- the order verify_payment already used.
        order = self.make_pending_order()
        coupon = self.make_coupon(code="LOCKORDER")
        self.add_item(order, quantity=1)
        self.attach_coupon(order, coupon)

        with _LockedReadRecorder() as recorded:
            commit_order_sale(order)

        acquired = [name for name, _ in recorded.locked_reads]
        self.assertIn(products._meta.db_table, acquired)
        self.assertIn(Coupon._meta.db_table, acquired)
        self.assertLess(
            acquired.index(products._meta.db_table),
            acquired.index(Coupon._meta.db_table),
            "the Coupon was locked before the products: a lock-order inversion",
        )


class CouponOverLimitAuditTests(WebhookTestCase):
    """The consumption in commit_order_sale is deliberately not a gate; what is missing is a trace an operator can see for an over-limit redemption."""

    def over_limit_events(self):
        return AuditEvent.objects.filter(
            event_type=AuditEvent.EventType.PAYMENT_COUPON_OVER_LIMIT
        )

    def test_a_capture_within_the_limit_writes_no_over_limit_audit_row(self):
        order = self.make_pending_order()
        coupon = self.make_coupon(code="WITHIN1", usage_limit=1)
        self.add_item(order, quantity=1)
        self.attach_coupon(order, coupon)

        response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 200)
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        self.assertEqual(self.over_limit_events().count(), 0)

    def test_a_capture_past_the_limit_records_exactly_one_audit_row(self):
        # The limit was already spent when this capture landed: the increment pushes 1 -> 2 past a
        # limit of 1, and the row carries everything an operator needs.
        order = self.make_pending_order()
        coupon = self.make_coupon(code="OVER1", usage_limit=1, used_count=1)
        self.add_item(order, quantity=1)
        self.attach_coupon(order, coupon)

        self.deliver(self.capture_event(order))

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 2)
        event = self.over_limit_events().get()
        self.assertEqual(event.category, AuditEvent.Category.PAYMENT)
        self.assertEqual(event.order, order)
        self.assertEqual(event.detail["coupon_code"], "OVER1")
        self.assertEqual(event.detail["used_count"], 2)
        self.assertEqual(event.detail["usage_limit"], 1)
        self.assertEqual(event.detail["prevention"], "recorded_not_prevented")

    def test_the_over_limit_capture_still_succeeds(self):
        order = self.make_pending_order()
        coupon = self.make_coupon(code="STILLGO", usage_limit=1, used_count=1)
        product = self.add_item(order, quantity=2)
        self.attach_coupon(order, coupon)

        response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        self.assertEqual(
            PaymentEvent.objects.get(event_id="evt_TEST0001").outcome,
            PaymentEvent.Outcome.APPLIED,
        )
        self.assertEqual(self.over_limit_events().count(), 1)

    def test_the_over_limit_audit_row_rolls_back_with_the_capture(self):
        order = self.make_pending_order()
        coupon = self.make_coupon(code="ROLLBK", usage_limit=1, used_count=1)
        product = self.add_item(order, quantity=1)
        self.attach_coupon(order, coupon)

        class _CaptureRolledBack(Exception):
            pass

        with self.assertRaises(_CaptureRolledBack):
            with transaction.atomic():
                commit_order_sale(order)
                raise _CaptureRolledBack

        self.assertEqual(self.over_limit_events().count(), 0)
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        product.refresh_from_db()
        self.assertEqual(product.stock, 10)

    def test_a_faulting_audit_write_never_breaks_the_capture(self):
        # The never-raise contract: the audit sink going down is a monitoring failure, not a reason to
        # roll back captured money.
        order = self.make_pending_order()
        coupon = self.make_coupon(code="SINKDOWN", usage_limit=1, used_count=1)
        product = self.add_item(order, quantity=2)
        self.attach_coupon(order, coupon)
        original = AuditEvent.record

        def selective_explode(event_type, *args, **kwargs):
            if str(event_type) == str(AuditEvent.EventType.PAYMENT_COUPON_OVER_LIMIT):
                raise RuntimeError("audit sink is down")
            return original(event_type, *args, **kwargs)

        with patch.object(AuditEvent, "record", staticmethod(selective_explode)):
            with self.assertLogs("orders.inventory", level="ERROR") as logs:
                response = self.deliver(self.capture_event(order))

        self.assertIn(
            "over-limit coupon redemption audit write failed",
            "\n".join(logs.output),
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 2)
        self.assertEqual(self.over_limit_events().count(), 0)

    def test_the_second_burn_of_a_single_use_code_records_the_over_limit(self):
        coupon = self.make_coupon(code="DOUBLEBURN", usage_limit=1)
        first = self.make_pending_order()
        self.add_item(first, quantity=1)
        self.attach_coupon(first, coupon)
        second = self.make_pending_order(
            gateway_order="order_TEST12", username="burn-two"
        )
        self.add_item(second, quantity=1)
        self.attach_coupon(second, coupon)

        self.deliver(self.capture_event(first))
        self.deliver(
            self.capture_event(second, payment_id="pay_TEST12"),
            event_id="evt_BURN2",
        )

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 2)
        event = self.over_limit_events().get()
        self.assertEqual(event.order, second)
        self.assertEqual(event.detail["coupon_code"], "DOUBLEBURN")
        self.assertEqual(event.detail["used_count"], 2)
        self.assertEqual(event.detail["usage_limit"], 1)


class SaleCommitServiceTests(WebhookTestCase):
    def test_committing_twice_decrements_once(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)

        commit_order_sale(order)
        order.payment_status = "captured"
        order.save(update_fields=["payment_status"])
        commit_order_sale(order)

        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_a_refunded_order_is_not_resold_by_a_later_commit(self):
        # A refunded order's sale was committed once and then unwound by the refund seam, so a stray
        # re-commit would take the units a second time for a sale that no longer exists.
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)
        order.payment_status = "refunded"
        order.save(update_fields=["payment_status"])

        commit_order_sale(order)

        product.refresh_from_db()
        self.assertEqual(product.stock, 10)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_an_uncaptured_order_commits_even_from_the_failed_payment_value(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)
        order.payment_status = "failed"
        order.save(update_fields=["payment_status"])

        commit_order_sale(order)

        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_insufficient_stock_raises_and_writes_nothing(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=5)
        hold = self.hold_for(order, product, quantity=5)
        products.objects.filter(pk=product.pk).update(stock=2)

        with self.assertRaises(StockUnavailable) as caught:
            commit_order_sale(order)

        self.assertEqual(caught.exception.product_id, product.id)
        self.assertEqual(caught.exception.requested, 5)
        self.assertEqual(caught.exception.available, 2)
        product.refresh_from_db()
        self.assertEqual(product.stock, 2)
        hold.refresh_from_db()
        self.assertEqual(hold.status, StockReservation.Status.ACTIVE)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_a_released_hold_is_not_resurrected_into_the_sale(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)
        hold = self.hold_for(order, product, quantity=2)
        StockReservation.objects.filter(pk=hold.pk).update(
            status=StockReservation.Status.RELEASED
        )

        commit_order_sale(order)

        hold.refresh_from_db()
        self.assertEqual(hold.status, StockReservation.Status.RELEASED)
        product.refresh_from_db()
        self.assertEqual(product.stock, 8)

    def test_an_order_with_no_holds_commits_its_stock_unchanged_by_that(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)

        commit_order_sale(order)

        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        self.assertFalse(StockReservation.objects.exists())

    def test_the_sale_guard_covers_exactly_the_captured_payment_values(self):
        # A hand-written oracle, not a recomputation: an expected value derived from the constant under
        # test agrees with a wrong constant from both sides, and this fails when the machine grows.
        self.assertEqual(
            CAPTURED_SALE_PAYMENT_STATUSES,
            {"captured", "partially_refunded", "refunded"},
        )
        self.assertNotIn("refunded", CAPTURED_MONEY_PAYMENT_STATUSES)
        self.assertIn("refunded", CAPTURED_SALE_PAYMENT_STATUSES)

    def test_two_lines_for_one_product_cannot_oversell_it(self):
        # Coverage measures lines executed, not states reasoned about: one line per product passes every
        # normal-shape test while two lines of 2 still sell a stock of 3.
        order = self.make_pending_order()
        product = self.make_product(name="Twin Lines", stock=3)
        self.add_item(order, product=product, quantity=2)
        self.add_item(order, product=product, quantity=2)

        with self.assertRaises(StockUnavailable) as caught:
            commit_order_sale(order)

        self.assertEqual(caught.exception.product_id, product.id)
        self.assertEqual(caught.exception.requested, 4)
        self.assertEqual(caught.exception.available, 3)
        product.refresh_from_db()
        self.assertEqual(product.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_two_lines_for_one_product_that_is_in_stock_sell_the_sum(self):
        order = self.make_pending_order()
        product = self.make_product(name="Twin Lines In Stock", stock=5)
        self.add_item(order, product=product, quantity=2)
        self.add_item(order, product=product, quantity=2)

        commit_order_sale(order)

        product.refresh_from_db()
        self.assertEqual(product.stock, 1)
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_the_service_locks_products_in_ascending_order(self):
        # Deadlock avoidance: ONE total order over the lock set (products first, coupon second) stops
        # two carts naming the same products in different orders from deadlocking.
        order = self.make_pending_order()
        self.add_item(order, quantity=1)
        self.add_item(order, quantity=1)

        with _LockedReadRecorder() as recorded:
            commit_order_sale(order)

        product_locks = recorded.for_table(products._meta.db_table)
        self.assertEqual(
            len(product_locks),
            1,
            f"expected one locked product read: {product_locks}",
        )
        self.assertEqual(product_locks[0], ("id",))

    def test_the_service_locks_the_order_row_it_decides_on(self):
        # The idempotency guard reads committed payment state, so the Order row must be locked while
        # that read happens, or two writers can both read "not captured" and both decrement.
        order = self.make_pending_order()
        self.add_item(order, quantity=1)

        with _LockedReadRecorder() as recorded:
            commit_order_sale(order)

        order_locks = recorded.for_table(Order._meta.db_table)
        self.assertEqual(
            len(order_locks), 1, f"expected one locked order read: {order_locks}"
        )

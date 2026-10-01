"""[R-1.15] SPEC-1-06: payment webhooks — the server-to-server payment truth.

Spec 11.2's payment requirements are "verify webhook signatures" and "handle
duplicate webhook deliveries"; the threat table's answer to webhook spoofing is
"signature verification and replay/idempotency controls"; and the invariant is
that "duplicate payment webhooks do not duplicate financial or inventory
effects". Every test here is written against what the store DID (order state,
audit trail, recorded events, money) rather than against response text, because
the text is not the property the spec asks for.

The signature is computed by the test with the same HMAC the provider uses —
that is the contract, not a mock — so no test needs the network and no
production code is bypassed.
"""

import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.db import IntegrityError
from django.test import override_settings
from django.utils import timezone

from rest_framework.permissions import AllowAny

from common.models import AuditEvent
from common.testing import ApiTestCase
from orders.models import Order, OrderStatusEvent, PaymentEvent, Refund
from orders.state import (
    TRIGGER_PAYMENT_WEBHOOK,
    status_for_payment,
)
from orders.webhooks import razorpay_webhook
from products.models import StockReservation

WEBHOOK_URL = "/api/v1/webhooks/razorpay/"
# A recognizable fixture value, never a real credential (V-01).
WEBHOOK_SECRET = "TESTINGONLY-WEBHOOK-SECRET-DO-NOT-USE"


@override_settings(RAZORPAY_WEBHOOK_SECRET=WEBHOOK_SECRET)
class WebhookTestCase(ApiTestCase):
    """Base: the endpoint's own contract, plus signed-delivery helpers."""

    def sign(self, raw_body):
        """The provider's HMAC-SHA256 hex digest over the exact bytes sent."""
        return hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

    def deliver(self, body, event_id="evt_TEST0001", signature=None, raw=None):
        """POST a delivery to the webhook endpoint exactly as the provider does.

        ``raw`` overrides the serialized bytes (for the malformed-body cases);
        ``signature`` overrides the header (for the unsigned / wrongly-signed
        cases). Both default to a correct signature over the exact bytes sent.
        """
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
        self, total="1200.50", gateway_order="order_TEST9", gateway_payment=None
    ):
        """An order awaiting its capture, as checkout + create_payment leave it."""
        user = self.make_user()
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
        """A stock hold in the order's name, as checkout mints it."""
        return StockReservation.objects.create(
            product=self.make_product(stock=10),
            order=order,
            owner=order.user,
            quantity=quantity,
            status=status,
            expires_at=timezone.now() + timedelta(hours=1),
        )


class SignatureVerificationTests(WebhookTestCase):
    """Property 1: the signature is the credential, and it is mandatory."""

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
        # The row reads as a fact about one delivery when an operator meets it
        # in the admin or a log line.
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
        # An unauthenticated caller must not be able to write rows into the
        # financial trail either.
        self.assertFalse(PaymentEvent.objects.exists())

    def test_missing_signature_header_is_refused(self):
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order), signature="")

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertFalse(PaymentEvent.objects.exists())

    def test_signature_over_different_bytes_is_refused(self):
        # The signature covers the raw body, so a payload edited after signing
        # (or a signature copied from another delivery) does not verify.
        order = self.make_pending_order()
        body = self.capture_event(order)
        signature = self.sign(json.dumps(body).encode("utf-8"))
        body["payload"]["payment"]["amount"] = 1

        response = self.deliver(body, signature=signature)

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_unsigned_forged_payload_cannot_mark_an_order_paid(self):
        # The whole point of the endpoint existing: knowing an order id is not
        # enough to mark it paid.
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
        # No secret means no signature could be valid: the endpoint must not
        # degrade into accepting whatever arrives.
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 503)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertFalse(PaymentEvent.objects.exists())

    def test_delivery_without_an_event_id_is_refused(self):
        # Nothing to deduplicate on means the replay defence is absent.
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order), event_id=None)

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertFalse(PaymentEvent.objects.exists())

    def test_non_ascii_signature_header_is_refused_not_crashed(self):
        # compare_digest raises TypeError on a non-ASCII str, so comparing the
        # header verbatim would make an anonymous caller able to force a 500 on
        # a money endpoint (and a 500 tells the provider to retry).
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order), signature="é" * 64)

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        self.assertFalse(PaymentEvent.objects.exists())

    def test_signature_with_surrounding_whitespace_is_refused(self):
        # The provider signs and compares a hex digest literally; a value with a
        # trailing newline is not what it sent.
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
    """Property 2: one delivery, one effect (spec 11.2's duplicate handling)."""

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
        # One customer notification, not two.
        self.assertEqual(len(mail.outbox), 1)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")

    def test_replay_converts_no_further_stock_holds(self):
        # The inventory effect is a held resource: converting it twice would
        # be a duplicate effect even though the row writes are idempotent.
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
        # A duplicate FACT arriving under a new delivery id: recorded, applied
        # to nothing.
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
    """Property 3: reconcile against what the order actually recorded."""

    def test_capture_event_for_an_unknown_order_is_recorded_and_refused(self):
        # A validly-signed event about a gateway order this store never
        # recorded is still a fact about a payment, so it is kept — with no
        # order to attach — and moves nothing.
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
        # Captured money that is not this order's money must never reconcile.
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
        # A missing gateway order id, a missing payment id, and an amount that
        # is not a provider minor-unit count: each is a delivery whose money
        # cannot be tied to this order.
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
        # The string amount is not money: the row keeps no amount rather than a
        # Decimal guessed out of it.
        self.assertEqual(
            PaymentEvent.objects.get(event_id="evt_MALFORMED2").amount,
            None,
        )

    def test_capture_with_a_negative_amount_is_recorded_and_refused(self):
        # A negative count of minor units is not money in any currency, and a
        # Decimal guessed out of it would be.
        order = self.make_pending_order()

        response = self.deliver(self.capture_event(order, amount_minor=-100))

        self.assertEqual(response.status_code, 200)
        event = PaymentEvent.objects.get()
        self.assertEqual(event.outcome, PaymentEvent.Outcome.REFUSED)
        self.assertIsNone(event.amount)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")

    def test_database_failure_that_is_not_a_replay_is_surfaced_for_retry(self):
        # The loop's safety net: an IntegrityError that is not the event_id
        # collision cannot be classified as a replay, so it is raised instead
        # of acknowledged — a 5xx tells the provider to redeliver, where a 200
        # would tell it this store heard a delivery it never recorded.
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
        # A hold an earlier failed attempt abandoned is never resurrected.
        self.assertEqual(released.status, StockReservation.Status.RELEASED)

    def test_the_customer_is_notified_by_the_reconciled_capture(self):
        order = self.make_pending_order()

        self.deliver(self.capture_event(order))

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(str(order.id), mail.outbox[0].subject)

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


class RefundEventTests(WebhookTestCase):
    """Property 8: a refund event is recorded, never re-issued as a refund."""

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
        # The SPEC-1-05 seam stays the only writer of refund rows.
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
        self.assertEqual(
            PaymentEvent.objects.get().outcome,
            PaymentEvent.Outcome.RECORDED,
        )
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
    """A genuine delivery this store has no writer for is still recorded."""

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
    """A correctly signed delivery that carries nothing to reconcile."""

    def test_oversized_event_id_is_refused_and_writes_no_row(self):
        # event_id is the replay key AND a bounded column: an oversized one is
        # refused rather than truncated (a truncated key would dedupe two
        # deliveries onto one row). Bounded in code because SQLite would accept
        # the value and Postgres would raise DataError on the same request.
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
        # The reference lives in the payload, so the delivery IS recordable —
        # but the row keeps no reference (a truncated one would read like a real
        # payment id) and no money moves.
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
        order = self.make_pending_order(gateway_payment="pay_TEST9")
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
    """The credential placement the conventions ask for (property 7)."""

    def test_delivery_needs_no_session_cookie_and_no_csrf_token(self):
        # The gateway holds no session cookie and cannot fetch a csrftoken, so
        # the endpoint has to be past the SPEC-17-03 gate. A client with a
        # session cookie and no CSRF token proves it: the cookie alone must not
        # turn a signed delivery into a 403.
        order = self.make_pending_order()
        self.client.force_login(self.make_user(username="session-holder"))

        response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")

    def test_endpoint_declares_no_credential_class(self):
        # Pinned structurally so a future edit cannot quietly reintroduce a
        # credential the gateway does not have (and with it the CSRF gate the
        # endpoint is exempt from for a reason).
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
    """The inverse dimension mapping the capture writer depends on."""

    def test_status_for_payment_names_the_earliest_status_it_implies(self):
        self.assertEqual(status_for_payment("captured"), "confirmed")
        self.assertEqual(status_for_payment("pending"), "pending")

    def test_status_for_payment_is_none_where_the_legacy_status_cannot_say_it(self):
        # The documented [R-10.1] divergence: the refund-dimension values have
        # no legacy single-status equivalent, and a writer must see that rather
        # than assume a mapping exists.
        self.assertIsNone(status_for_payment("refunded"))
        self.assertIsNone(status_for_payment("failed"))

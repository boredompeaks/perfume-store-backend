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
from django.db.models.query import QuerySet
from django.test import override_settings
from django.utils import timezone
from rest_framework.permissions import AllowAny

from common.models import AuditEvent
from common.testing import ApiTestCase
from orders.inventory import StockUnavailable, commit_order_sale
from orders.models import Order, OrderItem, OrderStatusEvent, PaymentEvent, Refund
from orders.state import (
    CAPTURED_MONEY_PAYMENT_STATUSES,
    CAPTURED_SALE_PAYMENT_STATUSES,
    TRIGGER_PAYMENT_WEBHOOK,
    status_for_payment,
)
from orders.webhooks import razorpay_webhook
from products.models import StockMovement, StockReservation, products

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

    def add_item(self, order, product=None, quantity=2):
        """An order line, as checkout leaves it.

        The shared sale commit reads ``order.items``, so the pins about what a
        capture does to INVENTORY need an order that actually has lines - a bare
        ORM order commits an empty sale, which is a true fact about nothing.
        """
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
        """The checkout-minted hold for one line, as create_order writes it."""
        return StockReservation.objects.create(
            product=product,
            order=order,
            owner=order.user,
            quantity=quantity,
            status=StockReservation.Status.ACTIVE,
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


class WebhookSaleCommitTests(WebhookTestCase):
    """Property 9: the delivery commits the SALE, not just the payment.

    This is the property the endpoint lacked. ``_apply_captured`` used to
    convert the order's holds to CONVERTED and stop, deferring the stock
    decrement to a reconciler that does not exist - so a capture with no browser
    callback behind it left the order paid with its stock never decremented and
    no ``StockMovement`` row to reconcile against. The pins drive the real
    endpoint and assert against the inventory tables, not the response text.
    """

    def test_capture_decrements_the_stock_its_order_sold(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)

        self.deliver(self.capture_event(order))

        product.refresh_from_db()
        self.assertEqual(product.stock, 8)  # the line's quantity, decremented

    def test_capture_writes_a_sale_ledger_row_for_each_line(self):
        # SPEC-6-02 [6.5.17]: a stock change without a movement row is a bug.
        # `stock_after` is the REAL post-decrement quantity and the actor is
        # the system, not the buyer - a capture moves stock, the customer does
        # not, so naming them as the actor would misattribute the mutation.
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
        # OrderItem.product is on_delete=SET_NULL: deleting a catalogue row
        # leaves the line unsellable rather than absent. It must still be
        # refused as a shortage rather than silently skipped, or the order
        # confirms with an uncommitted line.
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)
        product.delete()

        self.deliver(self.capture_event(order))

        self.assertEqual(
            PaymentEvent.objects.get().outcome, PaymentEvent.Outcome.REFUSED
        )
        self.assertEqual(StockMovement.objects.count(), 0)


class WebhookStockSufficiencyTests(WebhookTestCase):
    """Property 10: the delivery cannot confirm an order it cannot fill.

    The callback answers 409 on insufficient stock; the webhook has no client
    to answer, but it does have the gateway's money, which it cannot un-capture.
    So the honest outcome is the same no-sale with the conflict RECORDED, and
    the pins assert the order stays unfilled rather than oversold.
    """

    def test_capture_with_stock_gone_sells_nothing_and_is_recorded(self):
        order = self.make_pending_order()
        product = self.add_item(order, quantity=4)
        products.objects.filter(pk=product.pk).update(stock=1)  # sold out

        response = self.deliver(self.capture_event(order))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            PaymentEvent.objects.get().outcome, PaymentEvent.Outcome.REFUSED
        )
        product.refresh_from_db()
        self.assertEqual(product.stock, 1)  # untouched: no oversell
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_the_stock_conflict_is_recorded_for_reconciliation(self):
        # "The provider says this happened and we did nothing" is what the
        # operator reconciles from, so the refusal must name the numbers.
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
        # A hold released rather than converted is the [R-12.8] step-6
        # contract: a failed attempt leaves no phantom pressure on
        # available-to-sell while the operator resolves the paid order.
        order = self.make_pending_order()
        product = self.add_item(order, quantity=4)
        hold = self.hold_for(order, product, quantity=4)
        products.objects.filter(pk=product.pk).update(stock=1)

        self.deliver(self.capture_event(order))

        hold.refresh_from_db()
        self.assertEqual(hold.status, StockReservation.Status.RELEASED)

    def test_a_conflict_does_not_strand_the_order_as_paid(self):
        # The order must stay PENDING, not silently half-confirmed: a
        # confirmed order with no stock is exactly the state this endpoint
        # used to create.
        order = self.make_pending_order()
        product = self.add_item(order, quantity=4)
        products.objects.filter(pk=product.pk).update(stock=1)

        self.deliver(self.capture_event(order))

        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertIsNone(order.paid_at)


class SharedSaleCommitIdempotencyTests(WebhookTestCase):
    """Property 11: the two capture writers are idempotent WITH EACH OTHER.

    ``verify_payment`` (the customer callback) and ``_apply_captured`` (this
    delivery) are the only two writers of a paid order, and for the closed-tab
    customer ONLY the webhook runs. So the same payment can reach both writers,
    and whichever is second must be a no-op - a second decrement is the
    double-decrement SPEC-1-B01 already had to fix once for refunds. These pins
    drive BOTH real endpoints against the same order and payment.
    """

    def _verify(self, order):
        """The customer-callback verify for this order/payment, as the browser
        would send it after a successful gateway checkout."""
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

    def _capture_after_callback(self, order):
        """The callback confirms, then the delivery arrives; return the response.

        ``api_login`` attaches the bearer to ``self.client``, which is the same
        client ``deliver`` posts through, so one session sees both writers -
        which is the point: this is one customer, two writers, one payment.
        """
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
        self.assertEqual(product.stock, 8)  # decremented exactly once
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
        # The second arrival is still RECORDED (the provider must stop
        # retrying) - it is refused, not applied, and moves nothing.
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
        # The reverse order: the closed-tab customer is already confirmed by
        # the gateway's own delivery, and the browser comes back afterwards.
        # The callback's already-processed gate must refuse, and the stock must
        # still show exactly one decrement.
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
        # The pending order is the fixture this delivery is delivered TO; the
        # binding was never read, so the call stays and the name goes.
        self.make_pending_order(gateway_payment="pay_TEST9")

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


class _LockedReadRecorder:
    """Observe the lock-requesting reads the service really issues.

    Pinned on the QUERY rather than on the SQL text, and that is not a
    convenience: SQLite never emits ``FOR UPDATE`` at all
    (``features.has_select_for_update`` is False), so an SQL-text pin would
    pass on Postgres and fail on SQLite for a reason that has nothing to do
    with the service - the same engine blind spot that let a 500 survive a
    green SQLite run. The query object is what the service built, so recording
    it observes the service rather than the engine that compiles it.
    """

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


class SaleCommitServiceTests(WebhookTestCase):
    """The shared service's own contract, driven directly.

    The endpoint pins prove the two writers AGREE through it. These prove the
    service is safe for a caller that has not pre-checked - which is what makes
    it shareable at all, and what the webhook is.
    """

    def test_committing_twice_decrements_once(self):
        # The guard, at the unit level: the caller that already committed the
        # sale is told so by the order's own payment dimension, so a second
        # call is a no-op rather than the double decrement SPEC-1-B01 recorded.
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
        # A refunded order's sale was committed once and then unwound by the
        # refund seam. Its stock was therefore already decremented, so a stray
        # re-commit would take the units a second time for a sale that no
        # longer exists. The machine's CAPTURED_MONEY_PAYMENT_STATUSES answers a
        # different question ("can a refund still move out") and excludes
        # `refunded`; this guard asks whether the sale is committed, and must
        # not inherit that other question's answer.
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)
        order.payment_status = "refunded"
        order.save(update_fields=["payment_status"])

        commit_order_sale(order)

        product.refresh_from_db()
        self.assertEqual(product.stock, 10)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_an_uncaptured_order_commits_even_from_the_failed_payment_value(self):
        # failed -> captured is the machine's declared RETRY edge, so a failed
        # attempt's order is still sellable: the guard must not read "not
        # captured" as "do not sell".
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)
        order.payment_status = "failed"
        order.save(update_fields=["payment_status"])

        commit_order_sale(order)

        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_insufficient_stock_raises_and_writes_nothing(self):
        # The re-check runs BEFORE the first mutation, so a caller that
        # catches the shortage has a clean transaction — no converted holds, no
        # ledger row, no partial decrement.
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
        # [R-12.8]: a hold an earlier failed attempt released is terminal. The
        # conversion filters on ACTIVE, so the retry's sale commits the stock
        # and leaves the dead hold alone.
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
        # Legacy orders (minted before the reservation model, or with their
        # rows swept) still sell: the conversion simply matches nothing.
        order = self.make_pending_order()
        product = self.add_item(order, quantity=2)

        commit_order_sale(order)

        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
        self.assertFalse(StockReservation.objects.exists())

    def test_the_sale_guard_covers_exactly_the_captured_payment_values(self):
        # A hand-written oracle, not a recomputation: an expected value derived
        # from the constant under test agrees with a wrong constant from BOTH
        # sides (the BUG-5 lesson). And it FAILS WHEN THE MACHINE GROWS, which
        # is what makes it a gate rather than a snapshot — add a value
        # downstream of `captured` and the guard must widen or this pin names
        # it.
        self.assertEqual(
            CAPTURED_SALE_PAYMENT_STATUSES,
            {"captured", "partially_refunded", "refunded"},
        )
        # And it is not the returns set wearing the same clothes: that one
        # answers the refund question and so excludes the dead-end value.
        self.assertNotIn("refunded", CAPTURED_MONEY_PAYMENT_STATUSES)
        self.assertIn("refunded", CAPTURED_SALE_PAYMENT_STATUSES)

    def test_two_lines_for_one_product_cannot_oversell_it(self):
        # Coverage measures lines executed, not states reasoned about: the
        # ordinary order has one line per product, so a line-by-line re-check
        # passes every test that drives the normal shape and still lets two
        # lines of 2 sell a stock of 3. OrderItem carries no cart-style unique
        # constraint, so this state is expressible and `stock` is unsigned.
        order = self.make_pending_order()
        product = self.make_product(name="Twin Lines", stock=3)
        self.add_item(order, product=product, quantity=2)
        self.add_item(order, product=product, quantity=2)

        with self.assertRaises(StockUnavailable) as caught:
            commit_order_sale(order)

        self.assertEqual(caught.exception.product_id, product.id)
        # The demand reported is the TOTAL the order places on that product,
        # not whichever line happened to be walked first.
        self.assertEqual(caught.exception.requested, 4)
        self.assertEqual(caught.exception.available, 3)
        product.refresh_from_db()
        self.assertEqual(product.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_two_lines_for_one_product_that_is_in_stock_sell_the_sum(self):
        # The companion: the aggregated check must not refuse an order the
        # per-line check would have allowed.
        order = self.make_pending_order()
        product = self.make_product(name="Twin Lines In Stock", stock=5)
        self.add_item(order, product=product, quantity=2)
        self.add_item(order, product=product, quantity=2)

        commit_order_sale(order)

        product.refresh_from_db()
        self.assertEqual(product.stock, 1)
        # One ledger row per line, as the callback path has always written them.
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_the_service_locks_products_in_ascending_order(self):
        # Deadlock avoidance: unordered lock acquisition lets the plan pick the
        # sequence, so two carts naming the same products in different
        # insertion orders could deadlock across the handoff (the section-12
        # verified-facts advisory). ONE total order over the lock set is the
        # fix, and this is the pin that keeps it.
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
        # Ascending by the product's own primary key - a hand-written literal,
        # not the service's own expression recomputed here.
        self.assertEqual(product_locks[0], ("id",))

    def test_the_service_locks_the_order_row_it_decides_on(self):
        # The idempotency guard reads committed payment state, so the Order
        # row must be locked while that read happens - otherwise two writers
        # can both read "not captured" and both decrement.
        order = self.make_pending_order()
        self.add_item(order, quantity=1)

        with _LockedReadRecorder() as recorded:
            commit_order_sale(order)

        order_locks = recorded.for_table(Order._meta.db_table)
        self.assertEqual(
            len(order_locks), 1, f"expected one locked order read: {order_locks}"
        )

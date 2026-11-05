"""NEW-P1-1a: the read-only local-vs-gateway payment reconciliation.

Every test drives the command with the razorpay client patched by the
shared ``razorpay_mock`` fixture, so no test can reach the network. The
divergence classes are the ones the requirement names: a local capture the
gateway does not have, a gateway capture no local captured order records,
an amount/currency disagreement, and a refund disagreement in either
direction. The alert channel is ``ops.alerts`` (the existing admin alert
path); a run with divergences exits non-zero and a clean run exits zero.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest import mock

import razorpay
from django.conf import settings
from django.core import mail
from django.core.cache import caches
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings, tag
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from common.testing import ApiTestCase
from ops.management.commands import reconcile_payments as command_module
from orders.models import Order, Refund
from orders.webhooks import CAPTURED_PAYMENT_STATES


def alert_recipients(value="staff@x.com"):
    return override_settings(ALERT_RECIPIENTS=value)


def fetch_by_id(mapping):
    """A `fetch` side effect keyed by object id, raising configured errors."""

    def fetch(object_id, **_kwargs):
        result = mapping[object_id]
        if isinstance(result, Exception):
            raise result
        return result

    return fetch


@tag("ops")
class ReconcilePaymentsBase(ApiTestCase):
    def setUp(self):
        self.buyer = self.make_user("buyer")

    def gateway(self):
        client = self.razorpay_mock()
        client.payment.all.return_value = {"items": []}
        client.refund.all.return_value = {"items": []}
        return client

    def order(
        self,
        *,
        payment_id="pay_TEST1",
        gateway_order_id="order_TEST1",
        total="900.00",
        status="confirmed",
        payment_status="captured",
        payment_method="prepaid",
        paid_at=None,
    ):
        return Order.objects.create(
            user=self.buyer,
            full_name="Buyer Person",
            phone="9876543210",
            address="12 Rose Lane",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            status=status,
            payment_status=payment_status,
            payment_method=payment_method,
            total_amount=Decimal(total),
            paid_at=timezone.now() if paid_at is None else paid_at,
            razorpay_order_id=gateway_order_id,
            razorpay_payment_id=payment_id,
        )

    def refund(self, order, *, gateway_refund_id=None, amount="100.00", status=None):
        return Refund.objects.create(
            order=order,
            amount=Decimal(amount),
            reason="Damaged in transit",
            kind=Refund.Kind.PARTIAL,
            status=status or Refund.Status.PROCESSED,
            gateway_refund_id=gateway_refund_id,
        )

    def gateway_payment(self, **overrides):
        payload = {
            "id": "pay_TEST1",
            "order_id": "order_TEST1",
            "status": "captured",
            "amount": 90000,
            "currency": "INR",
        }
        payload.update(overrides)
        return payload

    def run_command(self, **options):
        options.setdefault("stdout", StringIO())
        return call_command("reconcile_payments", **options)

    def reset_alert_channel(self):
        """Clear the outbox AND the per-class cooldown between matrix rows.

        The cooldown is a production property (pinned in the alert tests), but
        one test method walking several rows of a classification matrix must
        not let row N's mail suppress row N+1's and then read the suppression
        as a classification bug.
        """
        mail.outbox.clear()
        caches["alerts"].clear()

    def assert_divergence(self, fragment, **options):
        with alert_recipients(), self.assertRaises(CommandError):
            self.run_command(**options)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(fragment, mail.outbox[0].subject.lower())
        return mail.outbox[0]


@tag("ops")
class CapturedVocabularyTests(ReconcilePaymentsBase):
    def test_the_reconciled_vocabulary_matches_the_webhook_reconciler(self):
        # Two readers of the same provider vocabulary: the webhook reconciler
        # knows which payment states mean "the money is in", and this command
        # must not hold a narrower answer -- a narrower one reports every
        # refunded gateway payment as a capture the provider never made.
        self.assertEqual(
            command_module.GATEWAY_CAPTURED_STATUSES,
            frozenset(CAPTURED_PAYMENT_STATES),
        )


@tag("ops")
class CleanWindowTests(ReconcilePaymentsBase):
    def test_clean_window_exits_zero_with_no_alert(self):
        client = self.gateway()
        order = self.order()
        client.payment.fetch.return_value = self.gateway_payment()
        refund = self.refund(order, gateway_refund_id="rfnd_TEST1")
        client.refund.fetch.return_value = {
            "id": "rfnd_TEST1",
            "amount": 10000,
            "currency": "INR",
        }
        client.refund.all.return_value = {
            "items": [
                {
                    "id": "rfnd_TEST1",
                    "payment_id": "pay_TEST1",
                    "amount": 10000,
                    "currency": "INR",
                }
            ]
        }
        with alert_recipients():
            self.run_command()
        self.assertEqual(mail.outbox, [])
        client.payment.fetch.assert_called_once_with(
            "pay_TEST1", timeout=settings.RAZORPAY_TIMEOUT
        )
        client.refund.fetch.assert_called_once_with(
            refund.gateway_refund_id, timeout=settings.RAZORPAY_TIMEOUT
        )

    def test_default_window_is_the_last_24_hours(self):
        client = self.gateway()
        self.order(paid_at=timezone.now() - timedelta(hours=25))
        with alert_recipients():
            self.run_command()
        self.assertEqual(mail.outbox, [])
        client.payment.fetch.assert_not_called()

    def test_since_until_narrow_the_window(self):
        client = self.gateway()
        self.order()
        now = timezone.now()
        with alert_recipients():
            self.run_command(
                since=(now - timedelta(hours=3)).isoformat(),
                until=(now - timedelta(hours=2)).isoformat(),
            )
        self.assertEqual(mail.outbox, [])
        client.payment.fetch.assert_not_called()

    def test_the_local_and_gateway_captured_vocabularies_reconcile(self):
        # The reconciled captured family is the same three values on both
        # sides: captured, partially_refunded and refunded. A payment the
        # customer has partially or fully had refunded is still a capture the
        # two sides must agree on, so no cell of this matrix is a divergence.
        statuses = ("captured", "partially_refunded", "refunded")
        client = self.gateway()
        payments = {}
        index = 0
        for local_status in statuses:
            for gateway_status in statuses:
                payment_id = f"pay_MATRIX{index}"
                gateway_order_id = f"order_MATRIX{index}"
                self.order(
                    payment_id=payment_id,
                    gateway_order_id=gateway_order_id,
                    payment_status=local_status,
                )
                payments[payment_id] = self.gateway_payment(
                    id=payment_id,
                    order_id=gateway_order_id,
                    status=gateway_status,
                )
                index += 1
        client.payment.fetch.side_effect = fetch_by_id(payments)
        with alert_recipients():
            self.run_command()
        self.assertEqual(mail.outbox, [])

    def test_the_razorpay_client_is_constructed_with_auth_as_a_keyword(self):
        # POSITIONAL AUTH IS THE REFUND-SEAM FAILURE: the SDK then reads the
        # tuple as base_url and every call dies on an AttributeError. The
        # keyword form is part of the construction contract, not a style
        # choice, so the call shape is pinned rather than assumed.
        self.gateway()
        with alert_recipients():
            self.run_command()
        client_class = razorpay.Client
        self.assertIsInstance(client_class, mock.MagicMock)
        client_class.assert_called_once_with(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
        self.assertEqual(client_class.call_args.args, ())

    def test_client_is_built_from_settings_and_the_window_is_passed_to_the_gateway(
        self,
    ):
        real_client = razorpay.Client
        client = self.gateway()
        with alert_recipients():
            self.run_command(
                since="2026-11-01T00:00:00+00:00",
                until="2026-11-02T00:00:00+00:00",
            )
        self.assertIsNot(razorpay.Client, real_client)
        self.assertIsInstance(razorpay.Client, mock.MagicMock)
        client.payment.all.assert_called_once_with(
            {
                "from": 1793491200,
                "to": 1793577600,
                "count": command_module.PAGE_SIZE,
                "skip": 0,
            },
            timeout=settings.RAZORPAY_TIMEOUT,
        )


@tag("ops")
class LocalCaptureTests(ReconcilePaymentsBase):
    def test_local_capture_with_no_gateway_payment_alerts(self):
        client = self.gateway()
        order = self.order()
        client.payment.fetch.side_effect = razorpay.errors.BadRequestError(
            "The payment id provided does not exist"
        )
        message = self.assert_divergence("local capture without gateway payment")
        self.assertIn(f"order #{order.id}", message.body)

    def test_local_capture_without_a_payment_reference_alerts(self):
        client = self.gateway()
        order = self.order(payment_id=None, gateway_order_id=None)
        message = self.assert_divergence("local capture without gateway payment")
        self.assertIn(f"order #{order.id}", message.body)
        client.payment.fetch.assert_not_called()

    def test_local_capture_the_gateway_has_not_captured_alerts(self):
        client = self.gateway()
        self.order()
        client.payment.fetch.return_value = self.gateway_payment(status="authorized")
        message = self.assert_divergence("local capture without gateway payment")
        self.assertIn("authorized", message.body)

    # One row per gateway status this class can meet. `diverges` is the
    # classification the captured vocabulary implies: the three captured-family
    # values reconcile, and only a payment the provider has NOT captured --
    # absent, merely authorized, or failed -- is a local capture the gateway
    # has no captured payment for.
    GATEWAY_STATUS_MATRIX = (
        ("captured", False),
        ("partially_refunded", False),
        ("refunded", False),
        ("authorized", True),
        ("failed", True),
        (None, True),
    )

    def test_each_gateway_payment_status_is_classified_against_a_local_capture(
        self,
    ):
        client = self.gateway()
        order = self.order()
        for gateway_status, diverges in self.GATEWAY_STATUS_MATRIX:
            with self.subTest(gateway_status=gateway_status):
                self.reset_alert_channel()
                payment = self.gateway_payment()
                if gateway_status is None:
                    payment.pop("status")
                else:
                    payment["status"] = gateway_status
                client.payment.fetch.return_value = payment
                if diverges:
                    message = self.assert_divergence(
                        "local capture without gateway payment"
                    )
                    self.assertIn(f"order #{order.id}", message.body)
                    # The template HTML-escapes quotes, so pin the status word
                    # itself rather than its repr.
                    self.assertIn(str(gateway_status), message.body)
                else:
                    with alert_recipients():
                        self.run_command()
                    self.assertEqual(mail.outbox, [])

    def test_a_fully_refunded_local_capture_is_still_reconciled(self):
        client = self.gateway()
        self.order(payment_status="refunded")
        client.payment.fetch.side_effect = razorpay.errors.BadRequestError("gone")
        self.assert_divergence("local capture without gateway payment")

    def test_a_cod_capture_with_no_gateway_payment_is_not_a_divergence(self):
        client = self.gateway()
        self.order(payment_id=None, gateway_order_id=None, payment_method="cod")
        with alert_recipients():
            self.run_command()
        self.assertEqual(mail.outbox, [])
        client.payment.fetch.assert_not_called()

    def test_gateway_listing_failure_aborts_without_a_divergence_alert(self):
        client = self.gateway()
        client.payment.all.side_effect = razorpay.errors.ServerError("gateway 500")
        with alert_recipients(), self.assertRaises(CommandError) as caught:
            self.run_command()
        self.assertIn("gateway read failed", str(caught.exception))
        self.assertEqual(mail.outbox, [])

    def test_gateway_fetch_failure_aborts_without_a_divergence_alert(self):
        client = self.gateway()
        self.order()
        client.payment.fetch.side_effect = razorpay.errors.GatewayError("timed out")
        with alert_recipients(), self.assertRaises(CommandError) as caught:
            self.run_command()
        self.assertIn("gateway read failed", str(caught.exception))
        self.assertEqual(mail.outbox, [])

    def test_a_fetch_that_returns_no_object_is_refused(self):
        client = self.gateway()
        self.order()
        client.payment.fetch.return_value = None
        with alert_recipients(), self.assertRaises(CommandError) as caught:
            self.run_command()
        self.assertIn("returned no object", str(caught.exception))
        self.assertEqual(mail.outbox, [])

    def test_a_fetch_that_returns_a_different_object_is_refused(self):
        client = self.gateway()
        self.order()
        client.payment.fetch.return_value = self.gateway_payment(id="pay_OTHER")
        with alert_recipients(), self.assertRaises(CommandError) as caught:
            self.run_command()
        self.assertIn("mismatched object", str(caught.exception))
        self.assertEqual(mail.outbox, [])


@tag("ops")
class GatewayOnlyCaptureTests(ReconcilePaymentsBase):
    def test_gateway_capture_with_no_local_order_alerts(self):
        client = self.gateway()
        client.payment.all.return_value = {
            "items": [self.gateway_payment(id="pay_ORPHAN", order_id="order_ORPHAN")]
        }
        message = self.assert_divergence("gateway capture without local")
        self.assertIn("pay_ORPHAN", message.body)

    def test_gateway_capture_for_a_pending_local_order_alerts(self):
        client = self.gateway()
        local = self.order(
            payment_id="pay_PENDING1",
            gateway_order_id="order_PENDING1",
            status="pending",
            payment_status="pending",
            paid_at=None,
        )
        client.payment.all.return_value = {
            "items": [
                self.gateway_payment(id="pay_PENDING1", order_id="order_PENDING1")
            ]
        }
        message = self.assert_divergence("gateway capture without local")
        self.assertIn(f"order #{local.id}", message.body)
        client.payment.fetch.assert_not_called()

    def test_gateway_capture_for_a_matching_captured_order_is_not_a_divergence(self):
        client = self.gateway()
        self.order()
        client.payment.fetch.return_value = self.gateway_payment()
        client.payment.all.return_value = {"items": [self.gateway_payment()]}
        with alert_recipients():
            self.run_command()
        self.assertEqual(mail.outbox, [])

    def test_non_captured_gateway_payments_are_ignored(self):
        client = self.gateway()
        client.payment.all.return_value = {
            "items": [self.gateway_payment(id="pay_AUTH", status="authorized")]
        }
        with alert_recipients():
            self.run_command()
        self.assertEqual(mail.outbox, [])

    # The gateway-only class is fed by the same captured vocabulary: every
    # captured-family status is an orphan when no local order records it, and
    # only the not-captured values are ignored.
    GATEWAY_ONLY_STATUS_MATRIX = (
        ("captured", True),
        ("partially_refunded", True),
        ("refunded", True),
        ("authorized", False),
        ("failed", False),
        (None, False),
    )

    def test_each_gateway_payment_status_without_a_local_order_is_classified(self):
        client = self.gateway()
        for gateway_status, diverges in self.GATEWAY_ONLY_STATUS_MATRIX:
            with self.subTest(gateway_status=gateway_status):
                self.reset_alert_channel()
                payment = self.gateway_payment(id="pay_ORPHAN", order_id="order_ORPHAN")
                if gateway_status is None:
                    payment.pop("status")
                else:
                    payment["status"] = gateway_status
                client.payment.all.return_value = {"items": [payment]}
                if diverges:
                    message = self.assert_divergence("gateway capture without local")
                    self.assertIn("pay_ORPHAN", message.body)
                else:
                    with alert_recipients():
                        self.run_command()
                    self.assertEqual(mail.outbox, [])

    def test_each_captured_family_status_matching_a_pending_order_alerts(self):
        client = self.gateway()
        local = self.order(
            payment_id="pay_PENDING1",
            gateway_order_id="order_PENDING1",
            status="pending",
            payment_status="pending",
        )
        for gateway_status in ("captured", "partially_refunded", "refunded"):
            with self.subTest(gateway_status=gateway_status):
                self.reset_alert_channel()
                client.payment.all.return_value = {
                    "items": [
                        self.gateway_payment(
                            id="pay_PENDING1",
                            order_id="order_PENDING1",
                            status=gateway_status,
                        )
                    ]
                }
                message = self.assert_divergence("gateway capture without local")
                self.assertIn(f"order #{local.id}", message.body)

    def test_gateway_listing_is_paginated(self):
        client = self.gateway()
        page = [
            self.gateway_payment(
                id=f"pay_AUTH{i}", order_id=f"order_AUTH{i}", status="authorized"
            )
            for i in range(command_module.PAGE_SIZE)
        ]
        client.payment.all.side_effect = [{"items": page}, {"items": []}]
        with alert_recipients():
            self.run_command()
        self.assertEqual(client.payment.all.call_count, 2)
        self.assertEqual(mail.outbox, [])

    def test_a_listing_past_the_page_cap_is_refused_not_truncated(self):
        client = self.gateway()
        page = [
            self.gateway_payment(
                id=f"pay_AUTH{i}", order_id=f"order_AUTH{i}", status="authorized"
            )
            for i in range(command_module.PAGE_SIZE)
        ]
        client.payment.all.return_value = {"items": page}
        with mock.patch.object(command_module, "MAX_PAGES", 1):
            with alert_recipients(), self.assertRaises(CommandError) as caught:
                self.run_command()
        self.assertIn("exceeded", str(caught.exception))
        self.assertEqual(mail.outbox, [])

    def test_a_listing_without_an_items_list_is_refused(self):
        client = self.gateway()
        client.payment.all.return_value = {"count": 0}
        with alert_recipients(), self.assertRaises(CommandError) as caught:
            self.run_command()
        self.assertIn("no items list", str(caught.exception))
        self.assertEqual(mail.outbox, [])

    def test_a_listing_with_a_malformed_item_is_refused(self):
        client = self.gateway()
        client.payment.all.return_value = {"items": ["not-a-payment"]}
        with alert_recipients(), self.assertRaises(CommandError) as caught:
            self.run_command()
        self.assertIn("without an id", str(caught.exception))
        self.assertEqual(mail.outbox, [])

    def test_a_listing_item_without_an_id_is_refused(self):
        client = self.gateway()
        client.refund.all.return_value = {"items": [{"payment_id": "pay_X"}]}
        with alert_recipients(), self.assertRaises(CommandError) as caught:
            self.run_command()
        self.assertIn("without an id", str(caught.exception))
        self.assertEqual(mail.outbox, [])


@tag("ops")
class AmountCurrencyTests(ReconcilePaymentsBase):
    def test_amount_mismatch_alerts(self):
        client = self.gateway()
        order = self.order()
        client.payment.fetch.return_value = self.gateway_payment(amount=89999)
        message = self.assert_divergence("amount or currency mismatch")
        self.assertIn(f"order #{order.id}", message.body)
        self.assertIn("89999", message.body)

    def test_currency_mismatch_alerts(self):
        client = self.gateway()
        self.order()
        client.payment.fetch.return_value = self.gateway_payment(currency="USD")
        message = self.assert_divergence("amount or currency mismatch")
        self.assertIn("USD", message.body)

    def test_a_gateway_amount_that_is_not_integer_minor_units_alerts(self):
        client = self.gateway()
        self.order()
        client.payment.fetch.return_value = self.gateway_payment(amount="90000")
        message = self.assert_divergence("amount or currency mismatch")
        self.assertIn("not integer minor units", message.body)


@tag("ops")
class RefundDivergenceTests(ReconcilePaymentsBase):
    def test_processed_refund_without_a_gateway_id_alerts(self):
        client = self.gateway()
        refund = self.refund(self.order(payment_status="pending", status="pending"))
        message = self.assert_divergence("refund mismatch")
        self.assertIn(f"refund #{refund.id}", message.body)
        client.payment.fetch.assert_not_called()

    def test_local_refund_the_gateway_does_not_have_alerts(self):
        client = self.gateway()
        self.refund(
            self.order(payment_status="pending", status="pending"),
            gateway_refund_id="rfnd_GONE",
        )
        client.refund.fetch.side_effect = razorpay.errors.BadRequestError(
            "The refund id provided does not exist"
        )
        message = self.assert_divergence("refund mismatch")
        self.assertIn("rfnd_GONE", message.body)

    def test_gateway_refund_with_no_local_row_alerts(self):
        client = self.gateway()
        client.refund.all.return_value = {
            "items": [
                {
                    "id": "rfnd_ORPHAN",
                    "payment_id": "pay_ORPHAN",
                    "amount": 10000,
                    "currency": "INR",
                }
            ]
        }
        message = self.assert_divergence("refund mismatch")
        self.assertIn("rfnd_ORPHAN", message.body)

    def test_refund_amount_mismatch_alerts(self):
        client = self.gateway()
        refund = self.refund(
            self.order(payment_status="pending", status="pending"),
            gateway_refund_id="rfnd_TEST1",
        )
        client.refund.fetch.return_value = {
            "id": "rfnd_TEST1",
            "amount": 9999,
            "currency": "INR",
        }
        message = self.assert_divergence("refund mismatch")
        self.assertIn(f"refund #{refund.id}", message.body)
        self.assertIn("9999", message.body)

    def test_a_pending_refund_without_a_gateway_id_is_not_reported(self):
        self.gateway()
        self.refund(
            self.order(payment_status="pending", status="pending"),
            status=Refund.Status.PENDING,
        )
        with alert_recipients():
            self.run_command()
        self.assertEqual(mail.outbox, [])


@tag("ops")
class AlertChannelTests(ReconcilePaymentsBase):
    def _seed_all_classes(self):
        client = self.gateway()
        missing = self.order(payment_id="pay_MISSING", gateway_order_id="order_MISSING")
        mismatched = self.order(
            payment_id="pay_MISMATCH", gateway_order_id="order_MISMATCH"
        )
        client.payment.fetch.side_effect = fetch_by_id(
            {
                "pay_MISSING": razorpay.errors.BadRequestError("gone"),
                "pay_MISMATCH": self.gateway_payment(
                    id="pay_MISMATCH",
                    order_id="order_MISMATCH",
                    amount=1,
                ),
            }
        )
        client.payment.all.return_value = {
            "items": [self.gateway_payment(id="pay_ORPHAN", order_id="order_ORPHAN")]
        }
        self.refund(missing)
        return client, missing, mismatched

    def test_one_alert_per_divergence_class(self):
        self._seed_all_classes()
        with alert_recipients(), self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(len(mail.outbox), 4)
        subjects = sorted(message.subject for message in mail.outbox)
        self.assertEqual(len(set(subjects)), 4)
        self.assertEqual(
            subjects,
            [
                "Payment reconciliation divergence: amount or currency mismatch",
                "Payment reconciliation divergence: gateway capture without local "
                "captured order",
                "Payment reconciliation divergence: local capture without gateway "
                "payment",
                "Payment reconciliation divergence: refund mismatch",
            ],
        )

    def test_alerts_disabled_still_exits_nonzero(self):
        client = self.gateway()
        self.order()
        client.payment.fetch.side_effect = razorpay.errors.BadRequestError("gone")
        with alert_recipients(""), self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(mail.outbox, [])

    def test_verbosity_zero_still_detects_and_exits_nonzero(self):
        client = self.gateway()
        self.order()
        client.payment.fetch.side_effect = razorpay.errors.BadRequestError("gone")
        with alert_recipients(), self.assertRaises(CommandError):
            self.run_command(verbosity=0)

    def test_no_writes_to_business_state(self):
        self._seed_all_classes()
        orders_before = list(
            Order.objects.values_list("id", "payment_status", "razorpay_payment_id")
        )
        refunds_before = list(
            Refund.objects.values_list("id", "status", "gateway_refund_id", "amount")
        )
        with alert_recipients():
            with CaptureQueriesContext(connection) as queries:
                with self.assertRaises(CommandError):
                    self.run_command()
        writes = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"]
            .lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        ]
        self.assertEqual(writes, [])
        self.assertEqual(
            list(
                Order.objects.values_list("id", "payment_status", "razorpay_payment_id")
            ),
            orders_before,
        )
        self.assertEqual(
            list(
                Refund.objects.values_list(
                    "id", "status", "gateway_refund_id", "amount"
                )
            ),
            refunds_before,
        )


@tag("ops")
class ArgumentTests(ReconcilePaymentsBase):
    def test_invalid_since_is_refused_before_any_gateway_call(self):
        client = self.gateway()
        with self.assertRaises(CommandError):
            self.run_command(since="not-a-date")
        client.payment.all.assert_not_called()

    def test_inverted_window_is_refused_before_any_gateway_call(self):
        client = self.gateway()
        with self.assertRaises(CommandError):
            self.run_command(
                since="2026-11-02T00:00:00+00:00",
                until="2026-11-01T00:00:00+00:00",
            )
        client.payment.all.assert_not_called()

    @override_settings(RAZORPAY_KEY_ID=None, RAZORPAY_KEY_SECRET=None)
    def test_missing_credentials_are_refused_before_the_client_is_built(self):
        with mock.patch("razorpay.Client") as client_class:
            with self.assertRaises(CommandError) as caught:
                self.run_command()
        client_class.assert_not_called()
        self.assertIn("RAZORPAY_KEY_ID", str(caught.exception))

    def test_datetime_objects_are_accepted_as_window_bounds(self):
        self.gateway()
        now = timezone.now()
        with alert_recipients():
            self.run_command(
                since=now - timedelta(hours=1), until=now + timedelta(hours=1)
            )
        self.assertEqual(mail.outbox, [])

    @override_settings(TIME_ZONE="Asia/Kolkata")
    def test_a_naive_window_bound_is_read_in_the_current_timezone(self):
        # The bounds below are wall-clock in Asia/Kolkata (UTC+05:30), so
        # 2026-11-01 00:00 naive is 2026-10-31 18:30 UTC. The inside row
        # (19:00 UTC) is therefore only selected when make_aware uses the
        # CURRENT timezone; read as UTC, the same bounds select neither row.
        # This is a real in/out classification, not just "nothing raised".
        client = self.gateway()
        inside = self.order(
            payment_id="pay_INSIDE",
            gateway_order_id="order_INSIDE",
            paid_at=datetime(2026, 10, 31, 19, 0, tzinfo=dt_timezone.utc),
        )
        outside = self.order(
            payment_id="pay_OUTSIDE",
            gateway_order_id="order_OUTSIDE",
            paid_at=datetime(2026, 10, 31, 17, 0, tzinfo=dt_timezone.utc),
        )
        client.payment.fetch.side_effect = razorpay.errors.BadRequestError("gone")
        naive_since = datetime(2026, 11, 1, 0, 0)
        naive_until = datetime(2026, 11, 2, 0, 0)

        message = self.assert_divergence(
            "local capture without gateway payment",
            since=naive_since,
            until=naive_until,
        )
        self.assertIn(f"order #{inside.id}", message.body)
        self.assertNotIn(f"order #{outside.id}", message.body)
        client.payment.fetch.assert_called_once_with(
            "pay_INSIDE", timeout=settings.RAZORPAY_TIMEOUT
        )

        # The same naive bounds resolved in UTC select no local row at all:
        # the timezone, not the wall clock, is what decides the window.
        self.reset_alert_channel()
        with override_settings(TIME_ZONE="UTC"), alert_recipients():
            self.run_command(since=naive_since, until=naive_until)
        self.assertEqual(mail.outbox, [])

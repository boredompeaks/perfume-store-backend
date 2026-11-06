"""SPEC-1-05 (spec section 1, lines 29-151, [1.14]): refunds.

A paid order must be refundable in full or in part, the money must go back
through the payment gateway, and the attempt must be atomic and auditable.
Razorpay is mocked at the seam (``razorpay.Client``, which both
``orders.refunds`` and ``orders.views`` resolve to the one module attribute
the shared ``razorpay_mock`` helper patches), so no test here touches the
network or the real keys from ``.env`` (V-01 containment).

What is pinned is behaviour, not prose: the balance gate can never be passed
twice (a second attempt for the same money is refused whether or not it
carries an ``Idempotency-Key``), a PENDING reservation is committed before
the gateway call and reduces the balance while it lives, a gateway failure
leaves an honest FAILED row that releases its reservation, the money reaching
the gateway is quantized the way the codebase quantizes money everywhere
else, and authority is ``refunds.create`` — a support agent who may read and
fulfil orders still cannot move money.
"""

import itertools
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

import razorpay
import requests
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory, SimpleTestCase, override_settings, tag
from django.utils import timezone

from common.roles import ROLE_FINANCE, ROLE_SUPPORT
from common.testing import TEST_RAZORPAY_KEY_ID, TEST_RAZORPAY_KEY_SECRET, ApiTestCase
from orders.models import Order, OrderItem, Refund
from orders.refunds import (
    RefundGatewayError,
    RefundGatewayRefused,
    issue_refund,
    refund_payment,
)


@tag("orders")
class RefundGatewaySeamTests(SimpleTestCase):
    """The seam itself: env-driven credentials, minor units, typed failures.

    No DB and no request: everything the gateway can do to a refund is
    decided in this one function, so it is tested on its own rather than
    only through the endpoint that calls it.
    """

    @staticmethod
    def _patched_client(**response):
        """Patch the razorpay Client the seam builds and configure one
        refund response for it."""
        patcher = mock.patch("orders.refunds.razorpay.Client")
        client_cls = patcher.start()
        client = client_cls.return_value
        client.refund.create.return_value = response or {"id": "rfnd_ENV1"}
        return patcher, client_cls, client

    def test_the_client_is_built_from_the_env_backed_settings(self):
        patcher, client_cls, _ = self._patched_client()
        self.addCleanup(patcher.stop)

        # Distinct from the ambient test keys, so the assertion can prove the
        # seam read the settings rather than a literal of its own - and
        # composed from the suite's documented dummy credentials rather than
        # a new key-shaped literal, which the secret scan must never see.
        with override_settings(
            RAZORPAY_KEY_ID=f"seam-{TEST_RAZORPAY_KEY_ID}",
            RAZORPAY_KEY_SECRET=f"seam-{TEST_RAZORPAY_KEY_SECRET}",
        ):
            refund_id = refund_payment(
                payment_id="pay_1",
                amount=Decimal("10.00"),
                idempotency_key="k-seam-test",
            )

        # No credential is written in the code: the client is built from the
        # settings, which read RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET from env.
        # The keyword form is the POINT of this assertion, not a detail: the
        # SDK reads ``__init__(self, session=None, auth=None, **options)``, so
        # the same two values passed positionally bind the key id to
        # ``session`` and every refund dies with an AttributeError that this
        # seam reports to the operator as "the gateway declined".
        client_cls.assert_called_once_with(
            auth=(f"seam-{TEST_RAZORPAY_KEY_ID}", f"seam-{TEST_RAZORPAY_KEY_SECRET}")
        )
        self.assertEqual(refund_id, "rfnd_ENV1")

    def test_the_real_sdk_client_receives_the_credentials_as_its_auth(self):
        """The construction path with ``razorpay.Client`` itself NOT mocked.

        Every other test in this class replaces the client class wholesale, so
        ``Client.__init__`` never runs and no assertion here can see WHICH
        parameter the credentials landed in - which is precisely the defect
        that shipped: the pair was passed positionally, bound the key id to
        ``session``, and turned every real refund into an AttributeError this
        seam reported to the operator as "the gateway declined". Mocking the
        class cannot catch that, because a mock accepts any call shape.

        So this patches the SDK's own default session instead and lets the real
        ``__init__`` run, then observes the credentials where they actually
        matter - on the ``requests`` call. Expected values are hand-written
        literals passed in through ``override_settings``, never re-derived from
        whatever ``settings`` happens to hold.
        """
        response = mock.Mock(status_code=200)
        response.json.return_value = {"id": "rfnd_REALSESSION"}

        with mock.patch("razorpay.client.requests.Session") as session_cls:
            session_cls.return_value.post.return_value = response

            with override_settings(
                RAZORPAY_KEY_ID=f"seam-{TEST_RAZORPAY_KEY_ID}",
                RAZORPAY_KEY_SECRET=f"seam-{TEST_RAZORPAY_KEY_SECRET}",
                RAZORPAY_TIMEOUT=7,
            ):
                refund_id = refund_payment(
                    payment_id="pay_1",
                    amount=Decimal("10.00"),
                    idempotency_key="k-seam-test",
                )

            post = session_cls.return_value.post

        # The refund the provider acknowledged came back through the REAL
        # client: proof that the whole chain ran, not just the seam's own call.
        self.assertEqual(refund_id, "rfnd_REALSESSION")
        # The credentials reached requests as the HTTP auth pair...
        self.assertEqual(
            post.call_args.kwargs["auth"],
            (f"seam-{TEST_RAZORPAY_KEY_ID}", f"seam-{TEST_RAZORPAY_KEY_SECRET}"),
        )
        # ...and the env-driven timeout reached the socket alongside them.
        self.assertEqual(post.call_args.kwargs["timeout"], 7)
        # The idempotency key rides THIS call as the provider's header, so a
        # retry after a lost response cannot move the same money twice. The
        # SDK adds Content-type/User-Agent beside it, so assert the key.
        self.assertEqual(
            post.call_args.kwargs["headers"]["Idempotency-Key"],
            "k-seam-test",
        )

    def test_the_money_reaches_the_gateway_in_the_currencies_minor_units(self):
        patcher, _, client = self._patched_client()
        self.addCleanup(patcher.stop)

        refund_payment(
            payment_id="pay_1",
            amount=Decimal("10.00"),
            idempotency_key="k-seam-test",
        )

        client.refund.create.assert_called_once_with(
            {"payment_id": "pay_1", "amount": 1000},
            headers={"Idempotency-Key": "k-seam-test"},
            timeout=10,
        )

    def test_a_half_paise_amount_is_quantized_before_it_is_scaled(self):
        patcher, _, client = self._patched_client()
        self.addCleanup(patcher.stop)

        # 0.015 rounds HALF_EVEN to 0.02 (the rounding common.money applies
        # everywhere in this codebase), so the provider is asked for 2 units
        # and never for a half-paise it would reject.
        refund_payment(
            payment_id="pay_1",
            amount=Decimal("0.015"),
            idempotency_key="k-seam-test",
        )

        client.refund.create.assert_called_once_with(
            {"payment_id": "pay_1", "amount": 2},
            headers={"Idempotency-Key": "k-seam-test"},
            timeout=10,
        )

    def test_a_missing_payment_reference_is_a_typed_error(self):
        patcher, _, client = self._patched_client()
        self.addCleanup(patcher.stop)

        with self.assertRaises(RefundGatewayError):
            refund_payment(
                payment_id="",
                amount=Decimal("10.00"),
                idempotency_key="k-seam-test",
            )

        # Refused before any call: there is nothing at the gateway to refund.
        client.refund.create.assert_not_called()

    def test_a_provider_error_becomes_a_typed_error_naming_it(self):
        patcher, _, client = self._patched_client()
        self.addCleanup(patcher.stop)
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "The payment id provided does not exist"
        )

        with self.assertRaises(RefundGatewayError) as caught:
            refund_payment(
                payment_id="pay_gone",
                amount=Decimal("10.00"),
                idempotency_key="k-seam-test",
            )

        # The provider's own words ride the exception for the caller's log
        # line; the caller decides what may reach an API response.
        self.assertIn("BadRequestError", str(caught.exception))

    def test_a_response_without_a_refund_id_is_a_typed_error(self):
        patcher, _, _ = self._patched_client(**{"status": "processed"})
        self.addCleanup(patcher.stop)

        with self.assertRaises(RefundGatewayError):
            refund_payment(
                payment_id="pay_1",
                amount=Decimal("10.00"),
                idempotency_key="k-seam-test",
            )


@tag("orders")
class RefundTestBase(ApiTestCase):
    """Fixtures: a captured order and the finance operator who may refund it."""

    REFUND_PATH = "/api/admin/orders/{order_id}/refund/"

    def setUp(self):
        self.buyer = self.make_user("buyer")
        self.finance = self.user_with_role("finmgr", ROLE_FINANCE, staff=True)
        self.client.force_authenticate(self.finance)

    @staticmethod
    def user_with_role(username, role, staff=False):
        user = User.objects.create_user(
            username, f"{username}@example.com", "S3cure-Passphrase!"
        )
        if staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    def captured_order(
        self, total="900.00", payment_id="pay_CAPTURED1", payment_status="captured"
    ):
        return Order.objects.create(
            user=self.buyer,
            full_name="Buyer Person",
            phone="9876543210",
            address="12 Rose Lane",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            status="confirmed",
            payment_status=payment_status,
            total_amount=Decimal(total),
            paid_at=timezone.now(),
            razorpay_order_id=f"order_{payment_id or 'NONE'}",
            razorpay_payment_id=payment_id,
        )

    def with_items(self, order, product=None, quantity=1):
        """Give an ORM-created order the checkout lines the shipped edge
        requires (SPEC-10-03 makes items a shipped precondition)."""
        product = product or self.make_product(name="Rose Aurum", price="900.00")
        OrderItem.objects.create(
            order=order,
            product=product,
            product_name=product.name,
            price=product.price,
            quantity=quantity,
            subtotal=product.price * quantity,
        )
        return order

    def gateway(self, prefix="rfnd_TEST"):
        """The mocked Razorpay client, whose refunds succeed.

        Each call answers with its own gateway refund id (``rfnd_TEST1``,
        ``rfnd_TEST2``, ...) the way the provider does, so a test that issues
        several refunds exercises the real unique-column behaviour instead of
        colliding on one reused id.
        """
        client = self.razorpay_mock()
        counter = itertools.count(1)
        client.refund.create.side_effect = lambda *_args, **_kwargs: {
            "id": f"{prefix}{next(counter)}"
        }
        return client

    def refund(self, order, amount=None, reason="Damaged in transit", **extra):
        payload = {"reason": reason}
        if amount is not None:
            payload["amount"] = amount
        payload.update(extra)
        return self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            payload,
            format="json",
            # Every production attempt names one operator action; the tests
            # use a fresh literal per call unless a replay test supplies its
            # own header explicitly.
            HTTP_IDEMPOTENCY_KEY=f"test-refund-{uuid.uuid4().hex}",
        )


@tag("orders")
class FullRefundTests(RefundTestBase):
    def test_a_full_refund_returns_every_paisa_and_closes_the_payment(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.refund(order)

        self.assertEqual(res.status_code, 201, res.data)
        order.refresh_from_db()
        # The payment dimension moves to the choice orders.state already
        # declares; nothing new was invented for refunds.
        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(Decimal(res.data["refunded_total"]), Decimal("900.00"))
        self.assertEqual(Decimal(res.data["refundable_remaining"]), Decimal("0.00"))
        self.assertIsNotNone(order.refunded_at)

        refund = Refund.objects.get()
        self.assertEqual(refund.order_id, order.id)
        self.assertEqual(refund.amount, Decimal("900.00"))
        self.assertEqual(refund.kind, Refund.Kind.FULL)
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        self.assertEqual(refund.gateway_refund_id, "rfnd_TEST1")
        self.assertEqual(refund.actor_id, self.finance.pk)
        client.refund.create.assert_called_once_with(
            {"payment_id": "pay_CAPTURED1", "amount": 90000},
            headers={"Idempotency-Key": f"refund-{refund.pk}"},
            timeout=10,
        )

    def test_the_privileged_action_is_audited_against_the_refund(self):
        order = self.captured_order()
        self.gateway()

        self.refund(order)

        entry = LogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(Refund)
        )
        self.assertEqual(entry.user_id, self.finance.pk)
        self.assertEqual(entry.action_flag, ADDITION)
        # The trail has to answer "who refunded how much, on which order"
        # without joining the row the message names.
        self.assertIn(f"order #{order.id}", entry.change_message)
        self.assertIn("900.00", entry.change_message)

    def test_a_second_full_refund_finds_no_balance_left(self):
        order = self.captured_order()
        client = self.gateway()
        self.refund(order)

        res = self.refund(order)

        self.assertEqual(res.status_code, 409, res.data)
        # The second attempt is refused for the true reason - the money is
        # already all back with the customer - and it moves nothing.
        self.assertEqual(res.data["error"], "Order has no refundable balance left")
        self.assertEqual(Refund.objects.count(), 1)
        client.refund.create.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")


@tag("orders")
class PartialRefundTests(RefundTestBase):
    def test_a_partial_refund_keeps_the_order_partially_refunded(self):
        order = self.captured_order()
        client = self.gateway(prefix="rfnd_PART")

        res = self.refund(order, amount="300.00")

        self.assertEqual(res.status_code, 201, res.data)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")
        self.assertEqual(Decimal(res.data["refunded_total"]), Decimal("300.00"))
        self.assertEqual(Decimal(res.data["refundable_remaining"]), Decimal("600.00"))
        refund = Refund.objects.get()
        self.assertEqual(refund.kind, Refund.Kind.PARTIAL)
        self.assertEqual(refund.amount, Decimal("300.00"))
        client.refund.create.assert_called_once_with(
            {"payment_id": "pay_CAPTURED1", "amount": 30000},
            headers={"Idempotency-Key": f"refund-{refund.pk}"},
            timeout=10,
        )

    def test_partials_accumulate_and_the_last_one_closes_the_order(self):
        order = self.captured_order()
        self.gateway()

        for amount, expected_status in (
            ("100.00", "partially_refunded"),
            ("200.00", "partially_refunded"),
            ("600.00", "refunded"),
        ):
            res = self.refund(order, amount=amount)
            self.assertEqual(res.status_code, 201, res.data)
            order.refresh_from_db()
            self.assertEqual(order.payment_status, expected_status)

        self.assertEqual(Refund.refunded_total(order), Decimal("900.00"))
        self.assertEqual(order.refundable_remaining, Decimal("0.00"))
        self.assertEqual(Refund.objects.count(), 3)

    def test_an_omitted_amount_refunds_exactly_what_is_left(self):
        order = self.captured_order()
        self.gateway()
        self.refund(order, amount="700.00")

        res = self.refund(order)

        self.assertEqual(res.status_code, 201, res.data)
        refund = Refund.objects.order_by("id").last()
        self.assertEqual(refund.amount, Decimal("200.00"))
        # Exhausting the remaining balance is a full refund of the rest.
        self.assertEqual(refund.kind, Refund.Kind.FULL)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")

    def test_the_first_refund_stamps_refunded_at_and_later_ones_do_not_move_it(self):
        order = self.captured_order()
        self.gateway()

        self.refund(order, amount="100.00")
        order.refresh_from_db()
        first_stamp = order.refunded_at
        self.assertIsNotNone(first_stamp)

        self.refund(order, amount="100.00")

        order.refresh_from_db()
        self.assertEqual(order.refunded_at, first_stamp)

    def test_a_half_paise_amount_is_quantized_before_it_moves_any_money(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.refund(order, amount="0.015")

        self.assertEqual(res.status_code, 201, res.data)
        # HALF_EVEN, the rounding the database adapter would apply anyway:
        # 0.015 -> 0.02, and 2 minor units reach the provider.
        refund = Refund.objects.get()
        self.assertEqual(refund.amount, Decimal("0.02"))
        client.refund.create.assert_called_once_with(
            {"payment_id": "pay_CAPTURED1", "amount": 2},
            headers={"Idempotency-Key": f"refund-{refund.pk}"},
            timeout=10,
        )
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")
        self.assertEqual(order.refundable_remaining, Decimal("899.98"))


@tag("orders")
class OverRefundTests(RefundTestBase):
    def test_refunding_more_than_the_captured_amount_is_refused(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.refund(order, amount="900.01")

        self.assertEqual(res.status_code, 409, res.data)
        # Nothing moved: no row, no payment-dimension change, no gateway call.
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertIsNone(order.refunded_at)

    def test_a_partial_beyond_the_remaining_balance_is_refused(self):
        order = self.captured_order()
        self.gateway()
        self.refund(order, amount="700.00")

        res = self.refund(order, amount="300.00")

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(Refund.refunded_total(order), Decimal("700.00"))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")

    def test_amounts_that_are_not_money_are_refused(self):
        order = self.captured_order()
        client = self.gateway()

        # NaN/Infinity parse as Decimals but are not money (and comparing one
        # raises), and a value too large for 2-dp money cannot be quantized:
        # both are 400s, never a 500 out of the arithmetic.
        for amount in ("ten", "12,50", True, "", "NaN", "1e30"):
            with self.subTest(amount=amount):
                res = self.refund(order, amount=amount)
                self.assertEqual(res.status_code, 400, res.data)

        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_a_non_positive_amount_is_refused(self):
        order = self.captured_order()
        client = self.gateway()

        for amount in ("0.00", "-50.00", "0"):
            with self.subTest(amount=amount):
                res = self.refund(order, amount=amount)
                self.assertEqual(res.status_code, 400, res.data)

        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_a_refund_without_a_reason_is_refused(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="no-reason-key",
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()


@tag("orders")
class RefundEligibilityTests(RefundTestBase):
    def test_an_unpaid_order_cannot_be_refunded(self):
        order = self.captured_order(payment_status="pending", payment_id=None)
        client = self.gateway()

        res = self.refund(order)

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_a_failed_payment_cannot_be_refunded(self):
        order = self.captured_order(payment_status="failed")
        client = self.gateway()

        res = self.refund(order)

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_a_captured_order_with_no_gateway_payment_is_refused(self):
        # A cash-on-delivery order has no provider payment to reverse: the
        # seam has nothing to call, so the writer refuses before it writes.
        order = self.captured_order(payment_id=None)
        client = self.gateway()

        res = self.refund(order)

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_an_unknown_order_is_a_uniform_404(self):
        self.gateway()

        res = self.client.post(
            self.REFUND_PATH.format(order_id=999999),
            {"reason": "x"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="unknown-order-key",
        )

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Order not found")


@tag("orders")
class GatewayFailureTests(RefundTestBase):
    def test_a_gateway_failure_is_recorded_as_failed_and_releases_the_reservation(
        self,
    ):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "The payment id provided does not exist"
        )

        res = self.refund(order, amount="300.00")

        self.assertEqual(res.status_code, 502, res.data)
        # The attempt is durable and honest: one FAILED row, its reservation
        # released, the payment dimension untouched, no timestamp stamped.
        refund = Refund.objects.get()
        self.assertEqual(refund.amount, Decimal("300.00"))
        self.assertEqual(refund.status, Refund.Status.FAILED)
        self.assertEqual(refund.failure_code, Refund.FailureCode.PROVIDER_REFUSED)
        self.assertIsNone(refund.reserved_until)
        self.assertIsNone(refund.gateway_refund_id)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertIsNone(order.refunded_at)
        self.assertEqual(order.refundable_remaining, Decimal("900.00"))
        # A money attempt leaves its trail even when the provider refuses.
        entry = LogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(Refund)
        )
        self.assertEqual(entry.action_flag, ADDITION)
        self.assertIn(f"order #{order.id}", entry.change_message)
        self.assertIn("300.00", entry.change_message)
        self.assertIn("refused", entry.change_message.lower())
        client.refund.create.assert_called_once()

    def test_a_gateway_response_without_a_refund_id_is_ambiguous(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = None
        client.refund.create.return_value = {"status": "processed"}

        res = self.refund(order)

        # A 2xx with no id may or may not have moved money. The reservation
        # stays held exactly as committed; nothing silently releases it.
        self.assertEqual(res.status_code, 502, res.data)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertIsNone(refund.failure_code)
        self.assertIsNotNone(refund.reserved_until)
        self.assertGreater(refund.reserved_until, timezone.now())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertEqual(order.refundable_remaining, Decimal("0.00"))
        self.assertIsNone(order.refunded_at)

    def test_the_provider_message_stays_out_of_the_response(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "secret-ish provider internals"
        )

        res = self.refund(order)

        self.assertNotIn("secret-ish", str(res.data))
        # ...and the audit trail records the fact without the provider text.
        entry = LogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(Refund)
        )
        self.assertNotIn("secret-ish", entry.change_message)


@tag("orders")
class RefundFailureTaxonomyTests(RefundTestBase):
    """Definitive refusal vs ambiguous outcome, and the required action key.

    A provider BadRequest is the provider saying no before money moved, so the
    reservation is released and the row records ``provider_refused``. Every
    other failure - transport timeout, gateway/server error, a 2xx without an
    id - is ambiguous: the row stays PENDING with its reservation held
    exactly as committed, and only reconciliation or the sweep may release it.
    """

    def writer_request(self):
        request = RequestFactory().post("/api/admin/orders/1/refund/")
        request.user = self.finance
        return request

    def test_a_provider_bad_request_is_refused_and_the_row_records_why(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "payment does not exist"
        )

        res = self.refund(order, amount="300.00")

        self.assertEqual(res.status_code, 502, res.data)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.FAILED)
        self.assertEqual(refund.failure_code, Refund.FailureCode.PROVIDER_REFUSED)
        self.assertIsNone(refund.reserved_until)
        order.refresh_from_db()
        self.assertEqual(order.refundable_remaining, Decimal("900.00"))

    def test_a_transport_timeout_is_ambiguous_and_holds_the_reservation(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = requests.exceptions.Timeout("slow")

        res = self.refund(order, amount="300.00")

        self.assertEqual(res.status_code, 502, res.data)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertIsNone(refund.failure_code)
        self.assertIsNotNone(refund.reserved_until)
        self.assertGreater(refund.reserved_until, timezone.now())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertIsNone(order.refunded_at)
        self.assertEqual(order.refundable_remaining, Decimal("600.00"))
        entry = LogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(Refund)
        )
        self.assertIn("unknown gateway outcome", entry.change_message)
        self.assertIn("reservation held", entry.change_message)

    def test_a_gateway_server_error_is_ambiguous_too(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = razorpay.errors.ServerError("5xx")

        res = self.refund(order, amount="300.00")

        self.assertEqual(res.status_code, 502, res.data)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertIsNone(refund.failure_code)
        self.assertIsNotNone(refund.reserved_until)

    def test_a_response_without_an_id_is_ambiguous_and_holds_the_reservation(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = None
        client.refund.create.return_value = {"status": "processed"}

        res = self.refund(order)

        self.assertEqual(res.status_code, 502, res.data)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertIsNone(refund.failure_code)
        self.assertIsNotNone(refund.reserved_until)

    def test_a_missing_header_is_a_400_before_any_core_call(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"reason": "Damaged in transit", "amount": "300.00"},
            format="json",
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Idempotency-Key header is required")
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_a_blank_header_is_also_refused(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"reason": "Damaged in transit"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="   ",
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_issue_refund_without_a_key_is_a_programming_error(self):
        order = self.captured_order()
        client = self.gateway()

        for key in (None, "", "   "):
            with self.subTest(key=key):
                with self.assertRaisesMessage(
                    ValueError,
                    "issue_refund requires an operator action id (idempotency_key)",
                ):
                    issue_refund(
                        self.writer_request(),
                        order_id=order.id,
                        reason="Damaged in transit",
                        idempotency_key=key,
                    )

        # Refused before any database work: no row and no gateway call.
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_the_seam_only_calls_a_bad_request_a_definitive_refusal(self):
        client = self.gateway()

        client.refund.create.side_effect = razorpay.errors.BadRequestError("no")
        with self.assertRaises(RefundGatewayRefused):
            refund_payment(
                payment_id="pay_CAPTURED1",
                amount=Decimal("10.00"),
                idempotency_key="seam-refused",
            )

        client.refund.create.side_effect = razorpay.errors.GatewayError("unknown")
        with self.assertRaises(RefundGatewayError) as caught:
            refund_payment(
                payment_id="pay_CAPTURED1",
                amount=Decimal("10.00"),
                idempotency_key="seam-ambiguous",
            )
        self.assertNotIsInstance(caught.exception, RefundGatewayRefused)


@tag("orders")
class RefundReplayTests(RefundTestBase):
    def test_a_replayed_key_returns_the_original_refund_and_calls_the_gateway_once(
        self,
    ):
        order = self.captured_order()
        client = self.gateway(prefix="rfnd_REPLAY")
        headers = {"HTTP_IDEMPOTENCY_KEY": "refund-attempt-1"}

        first = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"reason": "Damaged in transit", "amount": "300.00"},
            format="json",
            **headers,
        )
        second = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"reason": "Damaged in transit", "amount": "300.00"},
            format="json",
            **headers,
        )

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["refund"]["id"], first.data["refund"]["id"])
        # The money moved exactly once: one gateway call, one refund row.
        self.assertEqual(Refund.objects.count(), 1)
        client.refund.create.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")
        self.assertEqual(Refund.refunded_total(order), Decimal("300.00"))

    def test_an_oversized_idempotency_key_is_refused(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"reason": "Damaged in transit"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="k" * 129,
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_the_same_key_on_another_order_is_an_independent_refund(self):
        # Replay identity is scoped per order: a finance operator working
        # through a queue under one key must not have the second order's
        # refund collapse onto the first.
        first = self.captured_order(payment_id="pay_CAPTURED1")
        second = self.captured_order(payment_id="pay_CAPTURED2")
        self.gateway()

        for order in (first, second):
            res = self.client.post(
                self.REFUND_PATH.format(order_id=order.id),
                {"reason": "Damaged in transit"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="queue-key",
            )
            self.assertEqual(res.status_code, 201, res.data)

        self.assertEqual(Refund.objects.count(), 2)
        self.assertEqual(
            set(Refund.objects.values_list("order_id", flat=True)),
            {first.id, second.id},
        )

    def test_a_keyed_retry_while_pending_is_refused_without_a_gateway_call(self):
        order = self.captured_order()
        Refund.objects.create(
            order=order,
            amount=Decimal("300.00"),
            reason="first attempt still in flight",
            kind=Refund.Kind.PARTIAL,
            status=Refund.Status.PENDING,
            actor=self.finance,
            idempotency_key="in-flight-key",
            reserved_until=timezone.now() + timedelta(minutes=5),
        )
        client = self.gateway()

        res = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"reason": "Damaged in transit", "amount": "300.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="in-flight-key",
        )

        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("already in progress", res.data["error"])
        # No second row and no second gateway call: the first reservation is
        # still the one promise on that money.
        self.assertEqual(Refund.objects.count(), 1)
        client.refund.create.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.refundable_remaining, Decimal("600.00"))

    def test_a_keyed_retry_after_failed_reuses_the_same_row(self):
        order = self.captured_order()
        client = self.gateway(prefix="rfnd_RETRY")
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "provider was sad"
        )

        first = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"reason": "Damaged in transit", "amount": "300.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="retry-in-place",
        )
        self.assertEqual(first.status_code, 502, first.data)
        failed = Refund.objects.get()
        failed_pk = failed.pk
        self.assertEqual(failed.status, Refund.Status.FAILED)

        client.refund.create.side_effect = None
        client.refund.create.return_value = {"id": "rfnd_RETRY_OK"}
        second = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"reason": "Damaged in transit", "amount": "300.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="retry-in-place",
        )

        self.assertEqual(second.status_code, 201, second.data)
        # One row, the SAME row: the unique index owns the key, so the retry
        # resets it instead of appending a second attempt.
        self.assertEqual(Refund.objects.count(), 1)
        refund = Refund.objects.get()
        self.assertEqual(refund.pk, failed_pk)
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        self.assertEqual(refund.gateway_refund_id, "rfnd_RETRY_OK")
        self.assertIsNone(refund.reserved_until)
        self.assertEqual(Refund.refunded_total(order), Decimal("300.00"))
        self.assertEqual(client.refund.create.call_count, 2)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")

    def test_the_same_key_with_a_different_amount_is_refused(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "provider was sad"
        )
        first = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"reason": "Damaged in transit", "amount": "300.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="different-amount",
        )
        self.assertEqual(first.status_code, 502, first.data)

        second = self.client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"reason": "Damaged in transit", "amount": "200.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="different-amount",
        )

        self.assertEqual(second.status_code, 409, second.data)
        self.assertIn("different amount", second.data["error"])
        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(Refund.objects.get().status, Refund.Status.FAILED)
        client.refund.create.assert_called_once()


@tag("orders")
class ReservationBalanceTests(RefundTestBase):
    """A PENDING row is a reservation; only PROCESSED money is "refunded"."""

    def _refund(self, order, *, amount, status, reserved_until):
        return Refund.objects.create(
            order=order,
            amount=Decimal(amount),
            reason="reservation fixture",
            kind=Refund.Kind.PARTIAL,
            status=status,
            actor=self.finance,
            reserved_until=reserved_until,
        )

    def test_a_pending_reservation_reduces_the_balance_not_the_refunded_total(self):
        order = self.captured_order()

        self._refund(
            order,
            amount="600.00",
            status=Refund.Status.PENDING,
            reserved_until=timezone.now() + timedelta(minutes=5),
        )

        self.assertEqual(Refund.refunded_total(order), Decimal("0.00"))
        self.assertEqual(Refund.reserved_total(order), Decimal("600.00"))
        self.assertEqual(order.refundable_remaining, Decimal("300.00"))

    def test_reserved_total_counts_only_pending_rows(self):
        order = self.captured_order()
        future = timezone.now() + timedelta(minutes=5)

        self._refund(
            order,
            amount="100.00",
            status=Refund.Status.PENDING,
            reserved_until=future,
        )
        self._refund(
            order,
            amount="200.00",
            status=Refund.Status.PROCESSED,
            reserved_until=None,
        )
        self._refund(
            order,
            amount="50.00",
            status=Refund.Status.FAILED,
            reserved_until=None,
        )

        self.assertEqual(Refund.reserved_total(order), Decimal("100.00"))
        self.assertEqual(Refund.refunded_total(order), Decimal("200.00"))
        self.assertEqual(order.refundable_remaining, Decimal("600.00"))

    def test_an_expired_pending_reservation_stops_reducing_the_balance(self):
        order = self.captured_order()

        self._refund(
            order,
            amount="600.00",
            status=Refund.Status.PENDING,
            reserved_until=timezone.now() - timedelta(seconds=1),
        )

        self.assertEqual(Refund.reserved_total(order), Decimal("0.00"))
        self.assertEqual(order.refundable_remaining, Decimal("900.00"))
        # Still a row, still forensic: lapsing is not deletion.
        self.assertEqual(Refund.objects.count(), 1)

    def test_a_null_reserved_until_is_held_indefinitely(self):
        order = self.captured_order()

        self._refund(
            order,
            amount="600.00",
            status=Refund.Status.PENDING,
            reserved_until=None,
        )

        self.assertEqual(Refund.reserved_total(order), Decimal("600.00"))
        self.assertEqual(order.refundable_remaining, Decimal("300.00"))

    def test_a_failed_row_does_not_reduce_the_balance(self):
        order = self.captured_order()

        self._refund(
            order,
            amount="600.00",
            status=Refund.Status.FAILED,
            reserved_until=None,
        )

        self.assertEqual(Refund.reserved_total(order), Decimal("0.00"))
        self.assertEqual(Refund.refunded_total(order), Decimal("0.00"))
        self.assertEqual(order.refundable_remaining, Decimal("900.00"))


@tag("orders")
class RefundReservationSweepTests(RefundTestBase):
    """The expiry sweep settles lapsed reservations visibly, never silently."""

    def _refund(
        self,
        order,
        *,
        amount="600.00",
        status=Refund.Status.PENDING,
        reserved_until=None,
    ):
        return Refund.objects.create(
            order=order,
            amount=Decimal(amount),
            reason="sweep fixture",
            kind=Refund.Kind.PARTIAL,
            status=status,
            actor=self.finance,
            reserved_until=reserved_until,
        )

    def run_sweep(self, *args):
        out = StringIO()
        call_command("sweep_refund_reservations", *args, stdout=out)
        return out.getvalue()

    def test_an_expired_reservation_is_swept_and_the_balance_restored(self):
        order = self.captured_order()
        row = self._refund(order, reserved_until=timezone.now() - timedelta(seconds=1))

        with self.assertLogs(
            "orders.management.commands.sweep_refund_reservations",
            level="WARNING",
        ) as logs:
            output = self.run_sweep()

        self.assertIn("swept 1 expired refund reservation(s)", output)
        self.assertIn(f"swept refund {row.pk}", output)
        self.assertIn(f"order {order.pk}", output)
        self.assertIn("600.00", output)
        self.assertIn("failure_code=reservation_expired", logs.output[0])
        row.refresh_from_db()
        self.assertEqual(row.status, Refund.Status.FAILED)
        self.assertEqual(row.failure_code, Refund.FailureCode.RESERVATION_EXPIRED)
        self.assertIsNone(row.reserved_until)
        # Still visible as forensic evidence, never deleted.
        self.assertTrue(Refund.objects.filter(pk=row.pk).exists())
        self.assertEqual(order.refundable_remaining, Decimal("900.00"))

    def test_a_non_expired_reservation_is_untouched(self):
        order = self.captured_order()
        future = timezone.now() + timedelta(minutes=5)
        row = self._refund(order, reserved_until=future)

        output = self.run_sweep()

        self.assertIn("swept 0 expired refund reservation(s)", output)
        row.refresh_from_db()
        self.assertEqual(row.status, Refund.Status.PENDING)
        self.assertIsNone(row.failure_code)
        self.assertEqual(row.reserved_until, future)
        self.assertEqual(order.refundable_remaining, Decimal("300.00"))

    def test_a_null_expiry_is_held_indefinitely_and_never_swept(self):
        order = self.captured_order()
        row = self._refund(order, reserved_until=None)

        output = self.run_sweep()

        self.assertIn("swept 0 expired refund reservation(s)", output)
        row.refresh_from_db()
        self.assertEqual(row.status, Refund.Status.PENDING)
        self.assertIsNone(row.failure_code)
        self.assertIsNone(row.reserved_until)
        self.assertEqual(order.refundable_remaining, Decimal("300.00"))

    def test_dry_run_reports_the_candidate_and_changes_nothing(self):
        order = self.captured_order()
        row = self._refund(order, reserved_until=timezone.now() - timedelta(seconds=1))

        output = self.run_sweep("--dry-run")

        self.assertIn(f"would sweep refund {row.pk}", output)
        self.assertIn("swept 0 expired refund reservation(s)", output)
        self.assertIn("1 would be swept", output)
        row.refresh_from_db()
        self.assertEqual(row.status, Refund.Status.PENDING)
        self.assertIsNone(row.failure_code)
        self.assertIsNotNone(row.reserved_until)
        # An expired reservation already stopped reducing the balance; the
        # dry run only reports it, the row and the balance are unchanged.
        self.assertEqual(order.refundable_remaining, Decimal("900.00"))

    def test_the_sweep_is_idempotent(self):
        order = self.captured_order()
        row = self._refund(order, reserved_until=timezone.now() - timedelta(seconds=1))

        first = self.run_sweep()
        second = self.run_sweep()

        self.assertIn("swept 1 expired refund reservation(s)", first)
        self.assertIn("swept 0 expired refund reservation(s)", second)
        row.refresh_from_db()
        self.assertEqual(row.status, Refund.Status.FAILED)
        self.assertEqual(row.failure_code, Refund.FailureCode.RESERVATION_EXPIRED)
        self.assertEqual(order.refundable_remaining, Decimal("900.00"))


@tag("orders")
class SettlementSavepointTests(RefundTestBase):
    """SQL failures in settle-path guards must never undo a settlement.

    PostgreSQL aborts the whole transaction on a SQL error, so every guard on
    the success and FAILED paths runs under an inner ``transaction.atomic()``
    SAVEPOINT. These pins hold that contract on every engine.
    """

    INVALID_SQL = "SELECT 1 FROM this_table_definitely_does_not_exist"

    def failing_sql(self):
        with connection.cursor() as cursor:
            cursor.execute(self.INVALID_SQL)

    def writer_request(self):
        request = RequestFactory().post("/api/admin/orders/1/refund/")
        request.user = self.finance
        return request

    def test_sql_failure_in_on_settled_does_not_roll_back_settlement(self):
        order = self.captured_order()
        client = self.gateway()

        def boom(_receipt):
            self.failing_sql()

        receipt = issue_refund(
            self.writer_request(),
            order_id=order.id,
            reason="Damaged in transit",
            idempotency_key="savepoint-settle-hook",
            on_settled=boom,
        )

        # The hook's SQL error was contained; the provider's money move and
        # the PROCESSED row survive it.
        self.assertFalse(receipt.replayed)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        self.assertIsNone(refund.reserved_until)
        self.assertEqual(refund.gateway_refund_id, "rfnd_TEST1")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")
        self.assertIsNotNone(order.refunded_at)
        client.refund.create.assert_called_once()

    def test_sql_failure_in_success_audit_does_not_roll_back_settlement(self):
        order = self.captured_order()
        client = self.gateway()

        def bad_audit(*_args, **_kwargs):
            self.failing_sql()

        with mock.patch("orders.refunds.log_api_action", bad_audit):
            receipt = issue_refund(
                self.writer_request(),
                order_id=order.id,
                reason="Damaged in transit",
                idempotency_key="savepoint-success-audit",
            )

        # The audit insert died with its savepoint; the settlement did not.
        self.assertFalse(receipt.replayed)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        self.assertIsNone(refund.reserved_until)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")
        self.assertFalse(
            LogEntry.objects.filter(
                content_type=ContentType.objects.get_for_model(Refund)
            ).exists()
        )
        client.refund.create.assert_called_once()

    def test_sql_failure_in_failed_flip_audit_keeps_failed_row(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = razorpay.errors.BadRequestError("boom")

        def bad_audit(*_args, **_kwargs):
            self.failing_sql()

        with mock.patch("orders.refunds.log_api_action", bad_audit):
            with self.assertRaises(RefundGatewayError):
                issue_refund(
                    self.writer_request(),
                    order_id=order.id,
                    amount="300.00",
                    reason="Damaged in transit",
                    idempotency_key="savepoint-failed-audit",
                )

        # The audit failure did not turn the FAILED flip back into a live
        # reservation, and it did not touch the payment dimension.
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.FAILED)
        self.assertIsNone(refund.reserved_until)
        self.assertIsNone(refund.gateway_refund_id)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertIsNone(order.refunded_at)
        self.assertEqual(order.refundable_remaining, Decimal("900.00"))
        client.refund.create.assert_called_once()


@tag("orders")
class RefundRaceTests(RefundTestBase):
    """The balance gate under contention.

    The writer holds the Order row lock (and this order's refund rows) for
    the whole attempt, so resolving two attempts in sequence is exactly the
    interleaving those locks permit - the same determinism the oversell and
    coupon races use for verify_payment.
    """

    def test_two_full_refunds_cannot_both_succeed(self):
        order = self.captured_order()
        client = self.gateway()

        first = self.refund(order)
        second = self.refund(order)

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 409, second.data)
        # The order is refunded once, not twice.
        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(Refund.refunded_total(order), Decimal("900.00"))
        client.refund.create.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")

    def test_two_partials_that_would_exceed_the_order_cannot_both_succeed(self):
        order = self.captured_order()
        client = self.gateway()

        first = self.refund(order, amount="600.00")
        second = self.refund(order, amount="600.00")

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 409, second.data)
        self.assertEqual(Refund.refunded_total(order), Decimal("600.00"))
        client.refund.create.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")
        self.assertEqual(order.refundable_remaining, Decimal("300.00"))


@tag("orders")
class RefundPermissionTests(RefundTestBase):
    def test_a_support_agent_cannot_refund(self):
        # Support reads and fulfils orders; refunds are finance's call
        # (spec 1.1's warning that a catalogue manager must not issue refunds
        # is the same split, one role over).
        support = self.user_with_role("supp", ROLE_SUPPORT)
        order = self.captured_order()
        client = self.gateway()
        self.client.force_authenticate(support)

        res = self.refund(order)

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["code"], "permission_denied")
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")

    def test_a_customer_cannot_refund_their_own_order(self):
        order = self.captured_order()
        client = self.gateway()
        self.client.force_authenticate(self.buyer)

        res = self.refund(order)

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_an_anonymous_caller_gets_the_uniform_403(self):
        order = self.captured_order()
        client = self.gateway()
        self.client.force_authenticate(None)

        res = self.refund(order)

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["code"], "permission_denied")
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_the_finance_role_is_the_authorized_writer(self):
        # The positive control for the three refusals above: the same call,
        # the same order, the same body - only the role differs.
        order = self.captured_order()
        self.gateway()

        res = self.refund(order)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Refund.objects.get().actor_id, self.finance.pk)


@tag("orders")
class RefundAdminSurfaceTests(RefundTestBase):
    """The admin surface is a read-only window onto issued refunds."""

    def test_finance_sees_the_refund_changelist(self):
        self.client.force_login(self.finance)

        res = self.client.get("/admin/orders/refund/")

        self.assertEqual(res.status_code, 200)

    def test_support_gets_403_on_the_refund_changelist(self):
        support = self.user_with_role("supp", ROLE_SUPPORT, staff=True)

        self.client.force_login(support)

        self.assertEqual(self.client.get("/admin/orders/refund/").status_code, 403)

    def test_the_admin_surface_refuses_to_write_a_refund(self):
        # Refunds are issued by the API seam, which is the only writer that
        # calls the gateway; a hand-written admin row would be money movement
        # with no payment behind it.
        self.client.force_login(self.finance)

        self.assertEqual(self.client.get("/admin/orders/refund/add/").status_code, 403)

    def test_the_change_view_is_a_read_and_a_posted_save_is_refused(self):
        order = self.captured_order()
        self.gateway()
        self.refund(order)
        refund = Refund.objects.get()
        self.client.force_login(self.finance)

        page = self.client.get(f"/admin/orders/refund/{refund.id}/change/")
        self.assertEqual(page.status_code, 200)  # renders read-only

        denied = self.client.post(
            f"/admin/orders/refund/{refund.id}/change/",
            {
                "order": order.id,
                "amount": "1.00",
                "reason": "rewritten by hand",
                "kind": Refund.Kind.FULL,
                "status": Refund.Status.PROCESSED,
                "gateway_refund_id": "rfnd_FAKED",
                "actor": self.finance.id,
                "_save": "Save",
            },
        )
        self.assertEqual(denied.status_code, 403)
        refund.refresh_from_db()
        self.assertEqual(refund.amount, Decimal("900.00"))
        self.assertEqual(refund.reason, "Damaged in transit")


@tag("orders")
class PartiallyRefundedShipmentTests(RefundTestBase):
    """[R-1.14] A partially-refunded order is still shippable.

    Regression pin for the interaction the first cut missed: the shipped
    precondition accepted ``captured`` only, so the moment a refund moved the
    payment dimension to ``partially_refunded`` the order could never ship
    again - and because cancel is illegal from ``confirmed`` there was no
    operator route out, so paid-for inventory was stranded permanently.
    ``partially_refunded`` is captured money minus a recorded refund, and the
    Refund row is what reconciliation reads, so the remainder ships.
    """

    FULFIL_PATH = "/api/admin/orders/{order_id}/fulfill/"

    def partially_refunded_order(self):
        """A confirmed, itemised order with one partial refund against it."""
        order = self.captured_order()
        self.with_items(order)
        self.gateway()
        self.refund(order, amount="100.00")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")
        return order

    def _support_client(self, username="supp"):
        client = self.fresh_client()
        client.force_authenticate(self.user_with_role(username, ROLE_SUPPORT))
        return client

    def _run_admin_action(self, action, orders):
        """A bulk action through the real admin UI (the house pattern)."""
        User.objects.create_superuser(
            "refundboss", "refundboss@example.com", "S3cure-Passphrase!"
        )
        admin_client = self.fresh_client()
        self.assertTrue(
            admin_client.login(username="refundboss", password="S3cure-Passphrase!")
        )
        return admin_client.post(
            "/admin/orders/order/",
            {
                "action": action,
                "_selected_action": [str(order.id) for order in orders],
                "select_across": "0",
            },
            follow=True,
        )

    def test_a_partially_refunded_order_ships_through_the_fulfil_seam(self):
        order = self.partially_refunded_order()

        res = self._support_client().post(
            self.FULFIL_PATH.format(order_id=order.id), {}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(order.fulfilment_status, "fulfilled")
        # Shipping reconciles against the refund row rather than erasing it:
        # the money returned and the money shipped are both still on record.
        self.assertEqual(Refund.refunded_total(order), Decimal("100.00"))
        self.assertEqual(order.refundable_remaining, Decimal("800.00"))

    def test_a_partially_refunded_order_ships_through_the_admin_bulk_action(self):
        order = self.partially_refunded_order()

        res = self._run_admin_action("mark_shipped", [order])

        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")

    def test_payment_states_that_are_not_captured_money_still_cannot_ship(self):
        for payment_status in ("pending", "authorized", "failed"):
            with self.subTest(payment_status=payment_status):
                order = self.captured_order(
                    payment_id=f"pay_{payment_status}",
                    payment_status=payment_status,
                )
                self.with_items(order)

                res = self._support_client(f"supp-{payment_status}").post(
                    self.FULFIL_PATH.format(order_id=order.id), {}, format="json"
                )

                self.assertEqual(res.status_code, 409, res.data)
                order.refresh_from_db()
                self.assertEqual(order.status, "confirmed")


@tag("orders")
class RefundAdminActionTests(RefundTestBase):
    """SPEC-D-03: refunds are issuable from the order admin.

    The owner's finding was "refund not showing": the refund record page was
    read-only and empty with no way to issue one, so the only writer was the
    DRF POST. These pins hold the admin entry point to the SAME authority and
    the SAME issuance core: an operator with ``refunds.create`` can issue a
    full or partial refund from an order, support cannot, and every refusal
    leaves the order untouched.
    """

    CHANGELIST = "/admin/orders/order/"
    REFUND_PAGE = "/admin/orders/order/{order_id}/issue-refund/"

    def admin_post(self, order, data, user=None, action_id=None):
        self.client.force_login(user or self.finance)
        data = {"action_id": action_id or uuid.uuid4().hex, **data}
        return self.client.post(
            self.REFUND_PAGE.format(order_id=order.id), data, follow=True
        )

    def run_action(self, user, orders, follow=False):
        self.client.force_login(user)
        return self.client.post(
            self.CHANGELIST,
            {
                "action": "issue_refund",
                "_selected_action": [str(order.id) for order in orders],
                "select_across": "0",
            },
            follow=follow,
        )

    def test_the_action_is_visible_to_a_finance_operator(self):
        # A row is needed: Django hides the action bar on an empty changelist.
        self.captured_order()
        self.client.force_login(self.finance)

        res = self.client.get(self.CHANGELIST)

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Issue a refund for the selected order")

    def test_the_action_routes_the_selected_order_to_the_input_page(self):
        order = self.captured_order()

        res = self.run_action(self.finance, [order])

        self.assertEqual(res.status_code, 302)
        self.assertTrue(
            res.headers["Location"].endswith(
                self.REFUND_PAGE.format(order_id=order.id)
            ),
            res.headers["Location"],
        )

    def test_the_input_page_renders_the_balance_and_both_fields(self):
        order = self.captured_order()
        self.client.force_login(self.finance)

        page = self.client.get(self.REFUND_PAGE.format(order_id=order.id))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Refundable balance")
        self.assertContains(page, "900.00")
        self.assertContains(page, 'name="amount"')
        self.assertContains(page, 'name="reason"')
        # The action identity is required on POST and rides the form as a
        # hidden field, so the existing per-field template needs no edit.
        self.assertContains(page, 'name="action_id"')
        self.assertContains(page, 'type="hidden"')
        self.assertRegex(
            page.content.decode(),
            r'name="action_id"[^>]*value="[0-9a-f]{32}"',
        )

    def test_a_finance_operator_issues_a_full_refund_with_a_blank_amount(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.admin_post(order, {"reason": "Damaged in transit"})

        self.assertEqual(res.status_code, 200)
        # The success message names the refund id and the money.
        self.assertContains(res, "Refund #")
        self.assertContains(res, "900.00")
        refund = Refund.objects.get()
        self.assertEqual(refund.amount, Decimal("900.00"))
        self.assertEqual(refund.kind, Refund.Kind.FULL)
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        self.assertEqual(refund.actor_id, self.finance.pk)
        self.assertEqual(refund.gateway_refund_id, "rfnd_TEST1")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(order.refundable_remaining, Decimal("0.00"))
        self.assertIsNotNone(order.refunded_at)
        client.refund.create.assert_called_once_with(
            {"payment_id": "pay_CAPTURED1", "amount": 90000},
            headers={"Idempotency-Key": f"refund-{refund.pk}"},
            timeout=10,
        )

    def test_a_post_without_an_action_id_is_a_form_error_and_issues_nothing(self):
        order = self.captured_order()
        client = self.gateway()
        self.client.force_login(self.finance)

        res = self.client.post(
            self.REFUND_PAGE.format(order_id=order.id),
            {"amount": "100.00", "reason": "No action identity"},
        )

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "This field is required.")
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_the_same_action_id_replays_instead_of_paying_twice(self):
        order = self.captured_order()
        client = self.gateway()
        action_id = uuid.uuid4().hex

        first = self.admin_post(
            order,
            {"amount": "300.00", "reason": "Damaged in transit"},
            action_id=action_id,
        )
        second = self.admin_post(
            order,
            {"amount": "300.00", "reason": "Damaged in transit"},
            action_id=action_id,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Refund.objects.count(), 1)
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        self.assertEqual(refund.idempotency_key, f"admin-refund-{action_id}")
        client.refund.create.assert_called_once()

    def test_the_admin_refund_is_audited_against_the_refund(self):
        order = self.captured_order()
        self.gateway()

        self.admin_post(order, {"amount": "300.00", "reason": "Goodwill"})

        entry = LogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(Refund)
        )
        self.assertEqual(entry.user_id, self.finance.pk)
        self.assertEqual(entry.action_flag, ADDITION)
        self.assertIn(f"order #{order.id}", entry.change_message)
        self.assertIn("300.00", entry.change_message)
        # The audit trail names the surface the money moved through.
        self.assertIn("via admin", entry.change_message)

    def test_a_partial_refund_leaves_the_rest_refundable(self):
        order = self.captured_order()
        self.gateway()

        res = self.admin_post(
            order, {"amount": "300.00", "reason": "One item returned"}
        )

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "300.00")
        refund = Refund.objects.get()
        self.assertEqual(refund.kind, Refund.Kind.PARTIAL)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")
        self.assertEqual(order.refundable_remaining, Decimal("600.00"))

    def test_a_superuser_sees_the_action_and_can_issue_a_refund(self):
        root = User.objects.create_superuser(
            "refundroot", "refundroot@example.com", "S3cure-Passphrase!"
        )
        order = self.captured_order()
        self.gateway()
        self.client.force_login(root)

        grid = self.client.get(self.CHANGELIST)
        self.assertContains(grid, "Issue a refund for the selected order")

        res = self.admin_post(order, {"reason": "Superuser correction"}, user=root)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Refund.objects.get().actor_id, root.pk)

    def test_support_gets_no_action_and_cannot_reach_the_input_page(self):
        support = self.user_with_role("supp-refund", ROLE_SUPPORT, staff=True)
        order = self.captured_order()
        client = self.gateway()

        grid = self.run_action(support, [order], follow=True)
        self.assertEqual(grid.status_code, 200)
        self.assertNotContains(grid, "Issue a refund for the selected order")

        page = self.client.get(self.REFUND_PAGE.format(order_id=order.id))
        posted = self.client.post(
            self.REFUND_PAGE.format(order_id=order.id),
            {"reason": "Support should not move money"},
        )
        self.assertEqual(page.status_code, 403)
        self.assertEqual(posted.status_code, 403)
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_a_blank_reason_is_refused_as_an_admin_error(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.admin_post(order, {"amount": "100.00", "reason": ""})

        self.assertContains(res, "A refund reason is required")
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertIsNone(order.refunded_at)

    def test_an_over_refund_is_refused_as_an_admin_error(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.admin_post(order, {"amount": "900.01", "reason": "Too much"})

        self.assertContains(
            res, "Refund of 900.01 exceeds the refundable balance of 900.00"
        )
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")

    def test_a_gateway_failure_leaves_a_failed_refund_row(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "provider internals"
        )

        res = self.admin_post(order, {"amount": "300.00", "reason": "Damaged"})

        self.assertContains(res, "The payment gateway could not complete this refund")
        self.assertNotContains(res, "provider internals")
        # The attempt is durable: the reservation was released, the payment
        # dimension did not move, and the row records what happened.
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.FAILED)
        self.assertIsNone(refund.reserved_until)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertIsNone(order.refunded_at)
        self.assertEqual(order.refundable_remaining, Decimal("900.00"))

    def test_a_non_numeric_amount_is_a_form_error_not_a_refund(self):
        order = self.captured_order()
        client = self.gateway()

        res = self.admin_post(order, {"amount": "ten", "reason": "Damaged"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_selecting_several_orders_refuses_to_issue_anything(self):
        first = self.captured_order(payment_id="pay_CAPTURED1")
        second = self.captured_order(payment_id="pay_CAPTURED2")
        client = self.gateway()

        res = self.run_action(self.finance, [first, second], follow=True)

        self.assertContains(res, "Select exactly one order to refund")
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_an_unknown_order_is_a_404_on_the_input_page(self):
        self.client.force_login(self.finance)

        res = self.client.get(self.REFUND_PAGE.format(order_id=999999))

        self.assertEqual(res.status_code, 404)

    def test_the_existing_keyed_retry_still_calls_the_gateway_once(self):
        # The replay contract moved into the shared core with the refactor;
        # this is the same call the API suite makes, kept here so the admin
        # extraction cannot quietly drop it.
        order = self.captured_order()
        client = self.gateway(prefix="rfnd_RETRY")
        headers = {"HTTP_IDEMPOTENCY_KEY": "admin-refactor-key"}

        for _ in range(2):
            res = self.client.post(
                self.REFUND_PATH.format(order_id=order.id),
                {"reason": "Damaged in transit", "amount": "300.00"},
                format="json",
                **headers,
            )
            self.assertIn(res.status_code, (200, 201), res.data)

        self.assertEqual(Refund.objects.count(), 1)
        client.refund.create.assert_called_once()

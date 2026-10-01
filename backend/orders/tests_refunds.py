"""SPEC-1-05 (spec section 1, lines 29-151, [1.14]): refunds.

A paid order must be refundable in full or in part, the money must go back
through the payment gateway, and the attempt must be atomic and auditable.
Razorpay is mocked at the seam (``razorpay.Client``, which both
``orders.refunds`` and ``orders.views`` resolve to the one module attribute
the shared ``razorpay_mock`` helper patches), so no test here touches the
network or the real keys from ``.env`` (V-01 containment).

What is pinned is behaviour, not prose: the balance gate can never be passed
twice (a second attempt for the same money is refused whether or not it
carries an ``Idempotency-Key``), a gateway failure leaves nothing behind at
all, the money reaching the gateway is quantized the way the codebase
quantizes money everywhere else, and authority is ``refunds.create`` — a
support agent who may read and fulfil orders still cannot move money.
"""

import itertools
from decimal import Decimal
from unittest import mock

from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase, override_settings, tag
from django.utils import timezone

import razorpay

from common.roles import ROLE_FINANCE, ROLE_SUPPORT
from common.testing import TEST_RAZORPAY_KEY_ID, TEST_RAZORPAY_KEY_SECRET, ApiTestCase
from orders.models import Order, Refund
from orders.refunds import RefundGatewayError, refund_payment


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
            refund_id = refund_payment(payment_id="pay_1", amount=Decimal("10.00"))

        # No credential is written in the code: the client is built from the
        # settings, which read RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET from env.
        client_cls.assert_called_once_with(
            f"seam-{TEST_RAZORPAY_KEY_ID}", f"seam-{TEST_RAZORPAY_KEY_SECRET}"
        )
        self.assertEqual(refund_id, "rfnd_ENV1")

    def test_the_money_reaches_the_gateway_in_the_currencies_minor_units(self):
        patcher, _, client = self._patched_client()
        self.addCleanup(patcher.stop)

        refund_payment(payment_id="pay_1", amount=Decimal("10.00"))

        client.refund.create.assert_called_once_with(
            {"payment_id": "pay_1", "amount": 1000}
        )

    def test_a_half_paise_amount_is_quantized_before_it_is_scaled(self):
        patcher, _, client = self._patched_client()
        self.addCleanup(patcher.stop)

        # 0.015 rounds HALF_EVEN to 0.02 (the rounding common.money applies
        # everywhere in this codebase), so the provider is asked for 2 units
        # and never for a half-paise it would reject.
        refund_payment(payment_id="pay_1", amount=Decimal("0.015"))

        client.refund.create.assert_called_once_with(
            {"payment_id": "pay_1", "amount": 2}
        )

    def test_a_missing_payment_reference_is_a_typed_error(self):
        patcher, _, client = self._patched_client()
        self.addCleanup(patcher.stop)

        with self.assertRaises(RefundGatewayError):
            refund_payment(payment_id="", amount=Decimal("10.00"))

        # Refused before any call: there is nothing at the gateway to refund.
        client.refund.create.assert_not_called()

    def test_a_provider_error_becomes_a_typed_error_naming_it(self):
        patcher, _, client = self._patched_client()
        self.addCleanup(patcher.stop)
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "The payment id provided does not exist"
        )

        with self.assertRaises(RefundGatewayError) as caught:
            refund_payment(payment_id="pay_gone", amount=Decimal("10.00"))

        # The provider's own words ride the exception for the caller's log
        # line; the caller decides what may reach an API response.
        self.assertIn("BadRequestError", str(caught.exception))

    def test_a_response_without_a_refund_id_is_a_typed_error(self):
        patcher, _, _ = self._patched_client(**{"status": "processed"})
        self.addCleanup(patcher.stop)

        with self.assertRaises(RefundGatewayError):
            refund_payment(payment_id="pay_1", amount=Decimal("10.00"))


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
            self.REFUND_PATH.format(order_id=order.id), payload, format="json"
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
            {"payment_id": "pay_CAPTURED1", "amount": 90000}
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
            {"payment_id": "pay_CAPTURED1", "amount": 30000}
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
        self.assertEqual(Refund.objects.get().amount, Decimal("0.02"))
        client.refund.create.assert_called_once_with(
            {"payment_id": "pay_CAPTURED1", "amount": 2}
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
            self.REFUND_PATH.format(order_id=order.id), {}, format="json"
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

        res = self.refund(order)

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(Refund.objects.count(), 0)

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
            self.REFUND_PATH.format(order_id=999999), {"reason": "x"}, format="json"
        )

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Order not found")


@tag("orders")
class GatewayFailureTests(RefundTestBase):
    def test_a_gateway_failure_rolls_the_whole_attempt_back(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "The payment id provided does not exist"
        )

        res = self.refund(order, amount="300.00")

        self.assertEqual(res.status_code, 502, res.data)
        # No partial state anywhere: the in-flight refund row went with the
        # transaction, the payment dimension did not move, no timestamp was
        # stamped and nothing was written to the privileged-action trail.
        self.assertEqual(Refund.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertIsNone(order.refunded_at)
        self.assertEqual(LogEntry.objects.count(), 0)
        client.refund.create.assert_called_once()

    def test_a_gateway_response_without_a_refund_id_also_rolls_back(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = None
        client.refund.create.return_value = {"status": "processed"}

        res = self.refund(order)

        self.assertEqual(res.status_code, 502, res.data)
        self.assertEqual(Refund.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")

    def test_the_provider_message_stays_out_of_the_response(self):
        order = self.captured_order()
        client = self.gateway()
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "secret-ish provider internals"
        )

        res = self.refund(order)

        self.assertNotIn("secret-ish", str(res.data))


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

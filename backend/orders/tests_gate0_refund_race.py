"""Gate 0 / R12 -- the refund reservation must exist before the gateway call.

Two tests, two different proofs:

* ``PendingReservationTests`` is the deterministic interleaving: attempt #1 is
  already in flight (a committed PENDING row, which is exactly what R12
  requires the writer to leave before it calls the provider), then attempt #2
  asks for money that would take the total past the captured amount. The
  second attempt must be refused *before* ``refund_payment`` is called. On
  ``spec-comp`` today ``Refund.refunded_total`` sums PROCESSED only, so the
  PENDING row reserves nothing and the second attempt over-refunds.

* ``ConcurrentPartialRefundTests`` is the threaded form: two partial refunds
  whose sum exceeds the captured amount start together. On PostgreSQL the
  Order row lock serialises them and exactly one wins; on SQLite the lock is
  a no-op, so the same test is the engine-level witness that the suite's
  default engine cannot prove the property.

Run on PostgreSQL:

    $env:DATABASE_URL='postgresql://perfume:pg2a_local_test_only@127.0.0.1:5432/perfume_store'
    venv\\Scripts\\python manage.py test orders.tests_gate0_refund_race -v 2
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from orders.models import Order, Refund
from orders.refunds import RefundRefused, issue_refund

User = get_user_model()


def _captured_order(total="900.00", payment_id="pay_GATE0"):
    return Order.objects.create(
        user=User.objects.create_user(
            username=f"buyer-{payment_id}", password="S3cure-Passphrase!"
        ),
        full_name="Race Buyer",
        phone="9876543210",
        address="12 Rose Lane",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        status="confirmed",
        payment_status="captured",
        total_amount=Decimal(total),
        paid_at=timezone.now(),
        razorpay_order_id=f"order_{payment_id}",
        razorpay_payment_id=payment_id,
    )


def _staff():
    return User.objects.create_user(
        username="gate0-finance",
        password="S3cure-Passphrase!",
        is_staff=True,
    )


def _request(user):
    request = RequestFactory().post("/api/orders/1/refund/")
    request.user = user
    return request


class PendingReservationTests(TestCase):
    """The reservation is the row that must exist while the gateway is out."""

    def test_a_pending_refund_reserves_balance_against_a_second_attempt(self):
        order = _captured_order()
        staff = _staff()

        # Attempt #1 is in flight: its PENDING row is committed before the
        # provider call (the ordering R12 requires; the lane/a writer already
        # commits it, comp-wt's in-transaction writer does not).
        Refund.objects.create(
            order=order,
            amount=Decimal("600.00"),
            reason="first attempt in flight",
            kind=Refund.Kind.PARTIAL,
            status=Refund.Status.PENDING,
            actor=staff,
            idempotency_key="gate0-inflight-1",
        )

        with mock.patch(
            "orders.refunds.refund_payment", return_value="rfnd_GATE0"
        ) as gateway:
            with self.assertRaises(RefundRefused):
                issue_refund(
                    _request(staff),
                    order_id=order.id,
                    amount="600.00",
                    reason="second attempt",
                    idempotency_key="gate0-second-2",
                )

        # The pending row is money already promised: nothing else may move.
        gateway.assert_not_called()
        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(Refund.refunded_total(order), Decimal("0.00"))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")


class ConcurrentPartialRefundTests(TransactionTestCase):
    """Threaded form; green on PostgreSQL, red on SQLite (no row locks)."""

    reset_sequences = True

    def test_two_concurrent_partials_that_exceed_the_capture(self):
        order = _captured_order(total="900.00", payment_id="pay_GATE0_RACE")
        staff = _staff()

        entered = threading.Event()
        release = threading.Event()
        calls: list[Decimal] = []
        calls_lock = threading.Lock()

        def slow_gateway(*, payment_id, amount, idempotency_key=None):
            with calls_lock:
                calls.append(amount)
                first = len(calls) == 1
            if first:
                entered.set()
                # Hold the first attempt open long enough for the second to
                # reach the balance gate. On PostgreSQL the second blocks on
                # the Order lock; on SQLite it reads the same stale balance.
                release.wait(timeout=2)
            return f"rfnd_RACE_{len(calls)}"

        results: list[tuple[str, object]] = []
        results_lock = threading.Lock()

        def attempt(key: str):
            from django.db import connection

            try:
                receipt = issue_refund(
                    _request(staff),
                    order_id=order.id,
                    amount="600.00",
                    reason=f"concurrent attempt {key}",
                    idempotency_key=key,
                )
                with results_lock:
                    results.append(("ok", receipt.refund.amount))
            except RefundRefused as exc:
                with results_lock:
                    results.append(("refused", exc.message))
            finally:
                connection.close()

        with mock.patch("orders.refunds.refund_payment", side_effect=slow_gateway):
            first = threading.Thread(target=attempt, args=("gate0-race-a",))
            second = threading.Thread(target=attempt, args=("gate0-race-b",))
            first.start()
            entered.wait(timeout=5)
            second.start()
            # Let the first commit, then let the second proceed.
            time.sleep(0.2)
            release.set()
            first.join(timeout=15)
            second.join(timeout=15)

        self.assertFalse(first.is_alive(), "first refund thread hung")
        self.assertFalse(second.is_alive(), "second refund thread hung")

        successes = [r for r in results if r[0] == "ok"]
        self.assertEqual(
            len(successes),
            1,
            f"expected exactly one refund, got {results}",
        )
        self.assertEqual(
            Refund.refunded_total(order),
            Decimal("600.00"),
            f"over-refund: gateway calls={calls}",
        )
        self.assertEqual(len(calls), 1, f"gateway called {len(calls)} times: {calls}")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")

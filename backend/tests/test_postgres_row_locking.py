"""PG-2a regression guard: no nullable-FK outer join inside a row lock.

The defect these pin: ``select_for_update()`` combined with a
``select_related()`` over a *nullable* ForeignKey compiles to a
``LEFT OUTER JOIN ... FOR UPDATE``, which PostgreSQL rejects with

    FOR UPDATE cannot be applied to the nullable side of an outer join

so every payment verification and every admin bulk action returned 500 in
production. SQLite never emitted ``FOR UPDATE`` at all
(``features.has_select_for_update`` is False), so no assertion phrased in
terms of that clause could ever fire on the engine this suite also runs
on - which is why the join assertion below is phrased in terms of the
JOIN instead, and holds on both engines.

Two separate things are pinned, and the second is what stops the first
from being "fixed" by deleting the lock:

* no locked statement joins a nullable FK - asserted against the SQL the
  real endpoint and the real admin action actually emit;
* the rows that must be locked are still locked - asserted at the ORM
  level (``query.select_for_update``) so it holds on SQLite too, and on
  the emitted ``FOR UPDATE`` clause where the backend produces one.

Dropping the join must not drop the coupon row lock: the verify path
re-checks that coupon's validity and increments its ``used_count``, and
that read-modify-write needs the row lock to be real.
"""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import tag
from django.test.utils import CaptureQueriesContext

from common.testing import ApiTestCase
from orders.admin import OrderAdmin
from orders.models import Coupon, Order

NULLABLE_FK_JOINS = ('LEFT OUTER JOIN "orders_coupon"', 'LEFT OUTER JOIN "auth_user"')


def _locking_sql(queryset):
    """The SQL Django would send, with ``FOR UPDATE`` as the backend adds it.

    Compiled inside a transaction because ``select_for_update`` refuses to
    compile outside one on the backends that support it.
    """
    from django.db import transaction

    with transaction.atomic():
        sql, _params = queryset.query.get_compiler("default").as_sql()
    return sql


@tag("e2e")
class LockedQuerysetShapeTests(ApiTestCase):
    """The bulk writers' locked statement, engine-independent."""

    def test_admin_bulk_locked_queryset_joins_no_nullable_fk(self):
        """The lock carries no join at all.

        ``list_display`` names both ``user`` and ``coupon``, and Django 6.1's
        ``ChangeList.get_select_related_fields`` joins every ForeignKey named
        there; both are nullable on Order, so both joins are LEFT OUTER
        JOINs. Fails if a join is reintroduced into the locked queryset.
        """
        sql = _locking_sql(OrderAdmin._locked_orders(Order.objects.all(), [1, 2]))
        for fragment in NULLABLE_FK_JOINS:
            self.assertNotIn(fragment, sql)
        self.assertNotIn("JOIN", sql.upper())

    def test_admin_bulk_locked_queryset_still_locks_the_order_rows(self):
        """Dropping the join must not have dropped the lock.

        ORM-level, so it is asserted on SQLite too even though SQLite emits
        no ``FOR UPDATE`` clause.
        """
        queryset = OrderAdmin._locked_orders(Order.objects.all(), [1, 2])
        self.assertIs(queryset.query.select_for_update, True)
        if connection.features.has_select_for_update:
            self.assertIn("FOR UPDATE", _locking_sql(queryset).upper())


@tag("e2e")
class AdminBulkActionEmitsNoNullableJoinTests(ApiTestCase):
    """The same invariant, read off the SQL a real bulk action issues."""

    def setUp(self):
        User = get_user_model()
        User.objects.create_superuser(
            "pg2aboss", "pg2a@example.com", "S3cure-Passphrase!"
        )
        self.assertTrue(
            self.client.login(username="pg2aboss", password="S3cure-Passphrase!")
        )
        self.buyer = self.make_user("buyer")

    def test_bulk_action_locks_orders_without_joining_their_nullable_fks(self):
        order = Order.objects.create(
            user=self.buyer, total_amount="100.00", status="pending"
        )
        with CaptureQueriesContext(connection) as captured:
            res = self.client.post(
                "/admin/orders/order/",
                {
                    "action": "mark_confirmed",
                    "_selected_action": [str(order.pk)],
                    "select_across": "0",
                },
                follow=True,
            )
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")

        sqls = [q["sql"] for q in captured.captured_queries]
        if connection.features.has_select_for_update:
            # Only the LOCKED statement is the defect. The changelist's own
            # display SELECT joins user and coupon quite legitimately - that
            # join is what renders the grid - so it is not an offender and
            # must not be counted as one.
            offenders = [
                s
                for s in sqls
                if "FOR UPDATE" in s.upper() and any(f in s for f in NULLABLE_FK_JOINS)
            ]
            self.assertEqual(
                offenders,
                [],
                "a bulk action locked a query joined to a nullable FK",
            )
        # Locked or not, the action must still do its work: on an engine
        # without row-level locking this is the whole of what is observable,
        # because the joined statement never carries FOR UPDATE there.
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")


@tag("e2e")
class VerifyLocksWhatItMutatesTests(ApiTestCase):
    """The verify path: no nullable join, and the coupon row still locked."""

    def _buy(self, coupon_code=None, stock=5, quantity=1):
        self.make_user("buyer")
        self.api_login("buyer")
        product = self.make_product(price="100.00", stock=stock)
        self.seed_session_cart([(product, quantity)])
        overrides = {"coupon_code": coupon_code} if coupon_code else {}
        res = self.client.post(
            "/api/orders/checkout/", self.checkout_payload(**overrides), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        return Order.objects.get(id=res.data["id"]), product

    def _verify_captured(self, order, gateway_order_id, payment_id):
        with CaptureQueriesContext(connection) as captured:
            res = self.client.post(
                "/api/orders/payment/verify/",
                {
                    "order_id": order.id,
                    "razorpay_order_id": gateway_order_id,
                    "razorpay_payment_id": payment_id,
                    "razorpay_signature": "sig",
                },
                format="json",
            )
        return res, [q["sql"] for q in captured.captured_queries]

    def _pay_then_verify(self, order, gateway_order_id, payment_id):
        self.razorpay_mock().order.create.side_effect = [{"id": gateway_order_id}]
        res = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        return self._verify_captured(order, gateway_order_id, payment_id)

    def test_verify_never_joins_the_nullable_coupon_under_a_lock(self):
        """The regression itself, on the SQL the endpoint really runs.

        Phrased as "no LEFT OUTER JOIN onto orders_coupon" rather than as a
        ``FOR UPDATE`` assertion, so it fires on SQLite too - the join was
        emitted there as well; only the ``FOR UPDATE`` on top of it was not.
        """
        coupon = self.make_coupon(code="LOCKME", discount_value="10")
        order, _product = self._buy(coupon_code="LOCKME")
        self.assertEqual(order.coupon_id, coupon.pk)

        res, sqls = self._pay_then_verify(order, "order_LOCK1", "pay_LOCK1")
        self.assertEqual(res.status_code, 200, res.data)

        offenders = [s for s in sqls if 'LEFT OUTER JOIN "orders_coupon"' in s]
        self.assertEqual(
            offenders, [], "verify joined the nullable coupon FK inside a lock"
        )

    def test_verify_still_locks_the_order_row_it_mutates(self):
        """Dropping the coupon join must not have dropped the order lock."""
        coupon = self.make_coupon(code="LOCKORD", discount_value="10")
        order, _product = self._buy(coupon_code="LOCKORD")

        res, sqls = self._pay_then_verify(order, "order_LOCK2", "pay_LOCK2")
        self.assertEqual(res.status_code, 200, res.data)

        if connection.features.has_select_for_update:
            locked = [s for s in sqls if "FOR UPDATE" in s.upper()]
            self.assertTrue(locked, "verify issued no row lock at all")
            for sql in locked:
                self.assertNotIn("LEFT OUTER JOIN", sql.upper())

        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")

    def test_verify_locks_the_coupon_row_it_reads_and_increments(self):
        """The coupon lock is what makes the ``used_count`` bump safe.

        The clause assertion is gated on the backend emitting ``FOR UPDATE``
        (SQLite cannot); the value assertion below runs on every engine.
        """
        coupon = self.make_coupon(code="LOCKCPN", discount_value="10", usage_limit=5)
        order, _product = self._buy(coupon_code="LOCKCPN")
        self.assertEqual(order.coupon_id, coupon.pk)

        res, sqls = self._pay_then_verify(order, "order_CPNLOCK", "pay_CPNLOCK")
        self.assertEqual(res.status_code, 200, res.data)

        if connection.features.has_select_for_update:
            coupon_locks = [
                s for s in sqls if "FOR UPDATE" in s.upper() and "orders_coupon" in s
            ]
            self.assertTrue(
                coupon_locks,
                "verify read and incremented the coupon without locking it",
            )
            for sql in coupon_locks:
                self.assertNotIn("LEFT OUTER JOIN", sql.upper())

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)

    def test_verify_succeeds_with_a_null_coupon(self):
        """The other state of the same nullable FK: ``coupon`` NULL.

        The verify path filters on ``user=request.user``, so a guest order
        (user NULL) is refused here by design and never reaches this
        statement - leaving ``coupon`` as the one nullable FK this lock can
        meet. ``CouponRaceTests`` covers the populated state; this is the
        NULL one, where the join matched nothing.
        """
        order, product = self._buy(stock=3, quantity=2)
        self.assertIsNone(order.coupon_id)

        res, _sqls = self._pay_then_verify(order, "order_NOCPN", "pay_NOCPN")
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        product.refresh_from_db()
        self.assertEqual(product.stock, 1)
        self.assertEqual(Coupon.objects.count(), 0)

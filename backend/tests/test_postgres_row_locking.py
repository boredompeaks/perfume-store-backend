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
* the rows that must be locked are still locked.

**BOTH LOCKS ARE PINNED ENGINE-INDEPENDENTLY, and the two levels are not
interchangeable.** Read this before trusting coverage of either lock:

* the admin bulk writers' **Order** lock is asserted at the ORM level
  (``queryset.query.select_for_update``);
* the verify path's **coupon** lock is asserted at the ORM level by
  ``recording_locks``, which records the ``select_for_update()`` CALL.

Neither could have been pinned from the emitted SQL alone, and an earlier
version of this docstring claimed the ORM-level assertion covered the
coupon lock when it did not - there was no such assertion, and the
clause check it pointed at is gated on
``connection.features.has_select_for_update``, so on SQLite deleting
``select_for_update()`` from the coupon fetch left this module green. What
IS still PostgreSQL-only is the weaker half: that the clause is actually
EMITTED. SQLite's compiler drops ``FOR UPDATE`` entirely, so no
SQL-phrased assertion can fire there. The split is deliberate and each
half is asserted where it can actually fail.

Dropping the join must not drop the coupon row lock: the verify path
re-checks that coupon's validity and increments its ``used_count``, and
that read-modify-write needs the row lock to be real.
"""

from contextlib import contextmanager
from unittest import mock

from django.contrib import admin as admin_site
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, tag
from django.test.utils import CaptureQueriesContext

from common.testing import ApiTestCase
from orders.admin import OrderAdmin
from orders.models import Coupon, Order

NULLABLE_FK_JOINS = ('LEFT OUTER JOIN "orders_coupon"', 'LEFT OUTER JOIN "auth_user"')


@contextmanager
def recording_locks():
    """Yield the set of models whose queryset had ``select_for_update()`` called.

    This is the ORM-level observation SQLite cannot hide. SQLite's COMPILER
    drops the ``FOR UPDATE`` clause, so no assertion phrased in terms of the
    emitted SQL can see the lock there - but the CALL is still made by the
    code under test, and intercepting the call records it on every backend.
    That is what lets the coupon lock below be pinned engine-independently
    instead of only where the clause happens to survive.
    """
    from django.db.models import QuerySet

    locked = set()
    original = QuerySet.select_for_update

    def spy(queryset, *args, **kwargs):
        locked.add(queryset.model)
        return original(queryset, *args, **kwargs)

    with mock.patch.object(QuerySet, "select_for_update", spy):
        yield locked


def _locking_sql(queryset):
    """The SQL Django would send, with ``FOR UPDATE`` as the backend adds it.

    Compiled inside a transaction because ``select_for_update`` refuses to
    compile outside one on the backends that support it.
    """
    from django.db import transaction

    with transaction.atomic():
        sql, _params = queryset.query.get_compiler("default").as_sql()
    return sql


def changelist_orders(request):
    """The queryset the changelist really hands a bulk action.

    Deliberately NOT ``Order.objects.all()``. That manager chain never
    carried a join, so any assertion built on it passes whatever
    ``_locked_orders`` does with ``select_related`` - it cannot fail for the
    change it is meant to guard. The defect arrived on the changelist
    queryset, because ``ModelAdmin.get_changelist_instance`` builds the real
    ``ChangeList`` and ``ChangeList.get_queryset`` applies the
    ``select_related`` that ``get_select_related_fields()`` derives from
    ``list_display``. Building it the same way is the only way for an
    assertion about "no join" to mean anything.
    """
    model_admin = OrderAdmin(Order, admin_site.site)
    changelist = model_admin.get_changelist_instance(request)
    return changelist.get_queryset(request)


@tag("e2e")
class LockedQuerysetShapeTests(ApiTestCase):
    """The bulk writers' locked statement, engine-independent."""

    def setUp(self):
        self.staff = self.make_staff(username="pg2achangelist")
        self.request = RequestFactory().get("/admin/orders/order/")
        self.request.user = self.staff

    def test_admin_bulk_locked_queryset_joins_no_nullable_fk(self):
        """The lock carries no join at all.

        ``list_display`` names both ``user`` and ``coupon``, and Django 6.1's
        ``ChangeList.get_select_related_fields`` joins every ForeignKey named
        there; both are nullable on Order, so both joins are LEFT OUTER
        JOINs. Fails if a join is reintroduced into the locked queryset.

        Driven from the real changelist queryset, and the premise is asserted
        first: if the input ever stopped carrying the joins, the "no join"
        assertion below would pass for the wrong reason, so this also fails
        when the fixture stops resembling production.
        """
        incoming = changelist_orders(self.request)

        premise = _locking_sql(incoming.filter(pk__in=[1, 2]))
        for fragment in NULLABLE_FK_JOINS:
            self.assertIn(
                fragment,
                premise,
                "the changelist queryset no longer joins the nullable FKs this "
                "guard is about, so the assertion below would be vacuous",
            )

        sql = _locking_sql(OrderAdmin._locked_orders(incoming, [1, 2]))
        for fragment in NULLABLE_FK_JOINS:
            self.assertNotIn(fragment, sql)
        self.assertNotIn("JOIN", sql.upper())

    def test_admin_bulk_locked_queryset_still_locks_the_order_rows(self):
        """Dropping the join must not have dropped the lock.

        ORM-level, so it is asserted on SQLite too even though SQLite emits
        no ``FOR UPDATE`` clause. This is the one lock in this module with a
        genuine engine-independent pin.
        """
        queryset = OrderAdmin._locked_orders(changelist_orders(self.request), [1, 2])
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
        # The coupon row must exist before _buy resolves the code; only the
        # binding was unused, so the call (and its row) stays.
        self.make_coupon(code="LOCKORD", discount_value="10")
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

        TWO CLAIMS AT TWO LEVELS, because no single level covers both
        engines on its own:

        * engine-INDEPENDENT - the coupon is fetched by its OWN statement,
          never through the order's join (visible in the SQL everywhere),
          and that fetch asks the ORM for a row lock
          (``recording_locks``, which sees the CALL that SQLite's compiler
          would otherwise drop). Both fire on SQLite as well as PostgreSQL.
        * PostgreSQL-ONLY - that the fetch actually emits ``FOR UPDATE``.
          SQLite emits no such clause, so THIS half cannot be pinned there
          and is not claimed to be.
        """
        coupon = self.make_coupon(code="LOCKCPN", discount_value="10", usage_limit=5)
        order, _product = self._buy(coupon_code="LOCKCPN")
        self.assertEqual(order.coupon_id, coupon.pk)

        with recording_locks() as locked_models:
            res, sqls = self._pay_then_verify(order, "order_CPNLOCK", "pay_CPNLOCK")
        self.assertEqual(res.status_code, 200, res.data)

        # Engine-independent #1: a standalone read of orders_coupon exists...
        own_reads = [
            s
            for s in sqls
            if 'FROM "orders_coupon"' in s and "LEFT OUTER JOIN" not in s.upper()
        ]
        self.assertTrue(
            own_reads,
            "verify read the coupon only through the order's join, so there is "
            "no independent statement to lock",
        )
        # ...and no statement joins the coupon at all.
        joined = [s for s in sqls if 'LEFT OUTER JOIN "orders_coupon"' in s]
        self.assertEqual(joined, [], "verify joined the nullable coupon FK")

        # Engine-independent #2: the ORM was actually asked to lock it.
        self.assertIn(
            Coupon,
            locked_models,
            "verify read and incremented the coupon without asking the ORM for "
            "a row lock",
        )

        if connection.features.has_select_for_update:
            coupon_locks = [s for s in own_reads if "FOR UPDATE" in s.upper()]
            self.assertTrue(
                coupon_locks,
                "the coupon statement carried no FOR UPDATE on an engine that "
                "emits one",
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

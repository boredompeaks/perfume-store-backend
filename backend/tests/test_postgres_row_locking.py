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
    """SQLite's compiler drops FOR UPDATE, but the select_for_update() CALL is still made, so intercepting it records the lock on every backend."""
    from django.db.models import QuerySet

    locked = set()
    original = QuerySet.select_for_update

    def spy(queryset, *args, **kwargs):
        locked.add(queryset.model)
        return original(queryset, *args, **kwargs)

    with mock.patch.object(QuerySet, "select_for_update", spy):
        yield locked


def _locking_sql(queryset):
    from django.db import transaction

    with transaction.atomic():
        sql, _params = queryset.query.get_compiler("default").as_sql()
    return sql


def changelist_orders(request):
    """Deliberately not Order.objects.all(): the real changelist applies select_related derived from list_display, which is where the defect arrived."""
    model_admin = OrderAdmin(Order, admin_site.site)
    changelist = model_admin.get_changelist_instance(request)
    return changelist.get_queryset(request)


@tag("e2e")
class LockedQuerysetShapeTests(ApiTestCase):
    def setUp(self):
        self.staff = self.make_staff(username="pg2achangelist")
        self.request = RequestFactory().get("/admin/orders/order/")
        self.request.user = self.staff

    def test_admin_bulk_locked_queryset_joins_no_nullable_fk(self):
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
        queryset = OrderAdmin._locked_orders(changelist_orders(self.request), [1, 2])
        self.assertIs(queryset.query.select_for_update, True)
        if connection.features.has_select_for_update:
            self.assertIn("FOR UPDATE", _locking_sql(queryset).upper())


@tag("e2e")
class AdminBulkActionEmitsNoNullableJoinTests(ApiTestCase):
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
            # Only the LOCKED statement is the defect; the changelist's display SELECT legitimately joins user and coupon.
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
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")


@tag("e2e")
class VerifyLocksWhatItMutatesTests(ApiTestCase):
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
        coupon = self.make_coupon(code="LOCKCPN", discount_value="10", usage_limit=5)
        order, _product = self._buy(coupon_code="LOCKCPN")
        self.assertEqual(order.coupon_id, coupon.pk)

        with recording_locks() as locked_models:
            res, sqls = self._pay_then_verify(order, "order_CPNLOCK", "pay_CPNLOCK")
        self.assertEqual(res.status_code, 200, res.data)

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
        joined = [s for s in sqls if 'LEFT OUTER JOIN "orders_coupon"' in s]
        self.assertEqual(joined, [], "verify joined the nullable coupon FK")

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
        order, product = self._buy(stock=3, quantity=2)
        self.assertIsNone(order.coupon_id)

        res, _sqls = self._pay_then_verify(order, "order_NOCPN", "pay_NOCPN")
        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        product.refresh_from_db()
        self.assertEqual(product.stock, 1)
        self.assertEqual(Coupon.objects.count(), 0)

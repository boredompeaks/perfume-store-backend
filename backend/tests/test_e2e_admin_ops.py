"""E2E admin/ops tests - docs/test-gaps.md e2e item 12 plus admin surface.

The order admin enforces a legal status flow (ALLOWED_TRANSITIONS in
orders/admin.py): paid orders can never reach 'cancelled' because there is
no refund flow yet (V-03) - this suite pins that contract.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import tag
from django.utils import timezone

from common.testing import ApiTestCase
from orders.models import Order, OrderItem


def _attach_order_item(order):
    """SPEC-10-03 fixture helper: give an ORM-created order the checkout
    line a real checkout always leaves behind (items are a shipped
    precondition); product is nullable so no catalogue row is needed."""
    return OrderItem.objects.create(
        order=order,
        product_name="Fixture perfume",
        price=order.total_amount,
        quantity=1,
        subtotal=order.total_amount,
    )


def make_order(user, status="pending", total="100.00"):
    return Order.objects.create(
        user=user,
        full_name="Admin Test",
        phone="9876543210",
        address="1 Admin Way",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        status=status,
        total_amount=Decimal(total),
    )


@tag("e2e")
class AdminOrderLifecycleTests(ApiTestCase):
    # e2e 12. admin transitions confirmed -> shipped -> delivered; cancelling
    # a paid order must be blocked (V-03: no refund flow to reconcile with)
    def setUp(self):
        User.objects.create_superuser("opsboss", "ops@example.com", "S3cure-Passphrase!")
        self.assertTrue(self.client.login(username="opsboss", password="S3cure-Passphrase!"))
        self.buyer = self.make_user("buyer")

    def _change_status(self, order, new_status):
        return self.client.post(
            f"/admin/orders/order/{order.id}/change/",
            {
                "user": order.user_id,
                "full_name": order.full_name,
                "phone": order.phone,
                "address": order.address,
                "city": order.city,
                "state": order.state,
                "pincode": order.pincode,
                "status": new_status,
                "_save": "Save",  # Django 4.1+ requires the submit button name
                # the (read-only) OrderItemInline still needs its management form
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
            follow=True,
        )

    def test_legal_lifecycle_pending_confirmed_shipped_delivered(self):
        order = make_order(self.buyer)
        # SPEC-10-03 fixture: the shipped step requires items + captured
        # payment, i.e. the row a real checkout + verify_payment produces.
        _attach_order_item(order)
        Order.objects.filter(pk=order.pk).update(payment_status="captured")

        res = self._change_status(order, "shipped")
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")  # pending -> shipped is illegal

        res = self._change_status(order, "confirmed")
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")

        res = self._change_status(order, "shipped")
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")

        res = self._change_status(order, "delivered")
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "delivered")

    def test_cancelling_a_paid_order_is_blocked(self):
        for paid_status in ("confirmed", "shipped", "delivered"):
            with self.subTest(status=paid_status):
                order = make_order(self.buyer, status=paid_status)
                res = self._change_status(order, "cancelled")
                self.assertEqual(res.status_code, 200)
                order.refresh_from_db()
                self.assertEqual(order.status, paid_status)  # unchanged

    def test_cancelling_an_unpaid_order_is_allowed(self):
        order = make_order(self.buyer, status="pending")
        res = self._change_status(order, "cancelled")
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")

    def test_terminal_states_cannot_move(self):
        for current in ("delivered", "cancelled"):
            with self.subTest(status=current):
                order = make_order(self.buyer, status=current)
                res = self._change_status(order, "pending")
                self.assertEqual(res.status_code, 200)
                order.refresh_from_db()
                self.assertEqual(order.status, current)

    def test_paid_order_cannot_be_reprocessed_through_the_payment_api(self):
        """The API side of the same guard: verify_payment can never move an
        order out of a post-pending state (idempotency block)."""
        order = make_order(self.buyer, status="confirmed")
        buyer_client = self.fresh_client()
        self.api_login("buyer", client=buyer_client)
        self.razorpay_mock(order_id="order_ADMIN")

        res = buyer_client.post(
            "/api/orders/payment/verify/",
            {
                "order_id": order.id,
                "razorpay_order_id": "order_ADMIN",
                "razorpay_payment_id": "pay_ADMIN",
                "razorpay_signature": "sig",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")


@tag("e2e")
class AdminBulkActionsTests(ApiTestCase):
    def setUp(self):
        User.objects.create_superuser("opsboss", "ops@example.com", "S3cure-Passphrase!")
        self.assertTrue(self.client.login(username="opsboss", password="S3cure-Passphrase!"))
        self.buyer = self.make_user("buyer")

    def _run_action(self, action, orders):
        return self.client.post(
            "/admin/orders/order/",
            {
                "action": action,
                "_selected_action": [str(order.id) for order in orders],
                "select_across": "0",
            },
            follow=True,
        )

    def test_mark_shipped_bulk_respects_transitions(self):
        confirmed = make_order(self.buyer, status="confirmed")
        # SPEC-10-03 fixture: the shipped edge requires items + captured.
        _attach_order_item(confirmed)
        Order.objects.filter(pk=confirmed.pk).update(payment_status="captured")
        delivered = make_order(self.buyer, status="delivered")

        res = self._run_action("mark_shipped", [confirmed, delivered])
        self.assertEqual(res.status_code, 200)
        confirmed.refresh_from_db()
        delivered.refresh_from_db()
        self.assertEqual(confirmed.status, "shipped")   # legal
        self.assertEqual(delivered.status, "delivered") # skipped

    def test_mark_confirmed_and_mark_delivered_bulk(self):
        pending = make_order(self.buyer, status="pending")
        shipped = make_order(self.buyer, status="shipped")
        delivered = make_order(self.buyer, status="delivered")

        res = self._run_action("mark_confirmed", [pending])
        self.assertEqual(res.status_code, 200)
        pending.refresh_from_db()
        self.assertEqual(pending.status, "confirmed")

        res = self._run_action("mark_delivered", [shipped, delivered])
        self.assertEqual(res.status_code, 200)
        shipped.refresh_from_db()
        delivered.refresh_from_db()
        self.assertEqual(shipped.status, "delivered")     # legal
        self.assertEqual(delivered.status, "delivered")   # skipped

        res = self._run_action("mark_confirmed", [shipped])
        self.assertEqual(res.status_code, 200)
        shipped.refresh_from_db()
        self.assertEqual(shipped.status, "delivered")     # terminal states skipped

    def test_cancel_pending_bulk_spares_paid_orders(self):
        pending = make_order(self.buyer, status="pending")
        confirmed = make_order(self.buyer, status="confirmed")

        res = self._run_action("cancel_pending", [pending, confirmed])
        self.assertEqual(res.status_code, 200)
        pending.refresh_from_db()
        confirmed.refresh_from_db()
        self.assertEqual(pending.status, "cancelled")
        self.assertEqual(confirmed.status, "confirmed")

    def test_export_csv_streams_selected_orders(self):
        order = make_order(self.buyer, status="confirmed", total="250.00")
        res = self._run_action("export_csv", [order])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "text/csv")
        body = res.content.decode()
        self.assertIn("id,customer,status,total,discount,coupon,created_at", body)
        self.assertIn(str(order.id), body)
        self.assertIn("confirmed", body)


@tag("e2e")
class AdminSurfaceSmokeTests(ApiTestCase):
    """The admin customizations render for staff and are closed to others."""

    def setUp(self):
        User.objects.create_superuser("opsboss", "ops@example.com", "S3cure-Passphrase!")
        self.buyer = self.make_user("buyer")
        self.order = make_order(self.buyer, status="confirmed", total="300.00")
        self.product = self.make_product(name="Admin Smoke", stock=2)
        self.client.login(username="opsboss", password="S3cure-Passphrase!")

    def test_order_changelist_and_change_page_render(self):
        res = self.client.get("/admin/orders/order/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, f"/admin/orders/order/{self.order.id}/change/")
        res = self.client.get(f"/admin/orders/order/{self.order.id}/change/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Order items (snapshot)")

    def test_coupon_changelist_states(self):
        from orders.models import Coupon

        Coupon.objects.create(
            code="RUNNING", discount_type="percentage", discount_value=10,
            valid_from=timezone.now() - timedelta(days=1), valid_until=timezone.now() + timedelta(days=1),
        )
        Coupon.objects.create(
            code="EXPIRED", discount_type="fixed", discount_value=10,
            valid_from=timezone.now() - timedelta(days=10), valid_until=timezone.now() - timedelta(days=5),
        )
        Coupon.objects.create(
            code="SCHEDULED", discount_type="percentage", discount_value=10,
            valid_from=timezone.now() + timedelta(days=2), valid_until=timezone.now() + timedelta(days=5),
        )
        Coupon.objects.create(
            code="INACTIVE", discount_type="percentage", discount_value=10,
            active=False,
            valid_from=timezone.now() - timedelta(days=1), valid_until=timezone.now() + timedelta(days=1),
        )
        Coupon.objects.create(
            code="UNLIMITED", discount_type="percentage", discount_value=10, usage_limit=None,
            valid_from=timezone.now() - timedelta(days=1), valid_until=timezone.now() + timedelta(days=1),
        )
        Coupon.objects.create(
            code="LIMITED", discount_type="percentage", discount_value=10,
            usage_limit=50, used_count=7,
            valid_from=timezone.now() - timedelta(days=1), valid_until=timezone.now() + timedelta(days=1),
        )
        res = self.client.get("/admin/orders/coupon/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "running")
        self.assertContains(res, "expired")
        self.assertContains(res, "scheduled")
        self.assertContains(res, "inactive")
        self.assertContains(res, "0 / ∞")  # usage display without a limit
        self.assertContains(res, "7 / 50")  # usage display with a limit

    def test_product_and_cart_changelists_render(self):
        from cart.models import Cart, CartItem

        cart = Cart.objects.create(session_id="session-abc-123")
        CartItem.objects.create(cart=cart, product=self.product, quantity=2)

        res = self.client.get("/admin/products/products/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "low (2)")  # stock_flag branch
        res = self.client.get("/admin/cart/cart/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "session-abc")  # session_short branch

    def test_user_admin_lists_order_stats(self):
        res = self.client.get(f"/admin/auth/user/{self.buyer.id}/change/")
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/admin/auth/user/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "buyer")

    def test_admin_closed_to_anonymous_and_customers(self):
        self.client.logout()
        res = self.client.get("/admin/orders/order/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("login", res.headers["Location"])

        customer = self.fresh_client()
        customer.credentials(HTTP_AUTHORIZATION="Bearer bogus-token")
        res = customer.get("/admin/orders/order/")
        self.assertEqual(res.status_code, 302)

from django.db.models import Count
from django.test import tag

from cart.models import CartItem
from common.models import AuditEvent
from common.testing import ApiTestCase
from orders.models import Order, OrderItem


@tag("orders")
class CheckoutRefreshSafetyTests(ApiTestCase):
    def setUp(self):
        self.buyer = self.make_user("buyer")
        _, self.token = self.api_login("buyer")
        self.product = self.make_product(name="Rose Aurum", price="500.00", stock=10)
        self.seed_session_cart([(self.product, 2)])

    def cart_row(self):
        return self.current_cart()

    def submit(self, **overrides):
        return self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(**overrides),
            format="json",
        )

    def test_refresh_while_pending_collapses_onto_the_same_order(self):
        first = self.submit()
        self.assertEqual(first.status_code, 201, first.data)

        refreshed = self.submit()

        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        self.assertEqual(refreshed.data["id"], first.data["id"])
        self.assertEqual(refreshed.data["order_number"], first.data["order_number"])
        self.assertEqual(refreshed.data["total_amount"], first.data["total_amount"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_CREATED
            ).count(),
            1,
        )

    def test_repeated_refreshes_never_accumulate_orders(self):
        first = self.submit()
        self.assertEqual(first.status_code, 201, first.data)

        for _ in range(3):
            refreshed = self.submit()
            self.assertEqual(refreshed.status_code, 200, refreshed.data)
            self.assertEqual(refreshed.data["id"], first.data["id"])

        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)

    def test_refreshed_order_is_the_single_one_that_can_be_charged(self):
        first = self.submit()
        self.submit()

        client_mock = self.razorpay_mock(order_id="order_REFRESH")
        payment = self.client.post(
            "/api/orders/payment/", {"order_id": first.data["id"]}, format="json"
        )

        self.assertEqual(payment.status_code, 200, payment.data)
        self.assertEqual(payment.data["razorpay_order_id"], "order_REFRESH")
        client_mock.order.create.assert_called_once()

    def test_refresh_leaves_every_cart_line_and_the_coupon_intact(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10")
        applied = self.client.post(
            "/api/cart/coupon/", {"code": "SAVE10"}, format="json"
        )
        self.assertEqual(applied.status_code, 200, applied.data)

        first = self.submit()
        self.assertEqual(first.status_code, 201, first.data)

        refreshed = self.submit()

        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        cart = self.cart_row()
        self.assertEqual(
            sorted(
                CartItem.objects.filter(cart=cart).values_list("product_id", "quantity")
            ),
            [(self.product.id, 2)],
        )
        self.assertEqual(cart.coupon_id, coupon.id)
        body = self.client.get("/api/cart/").data
        self.assertEqual(
            [(item["product"]["name"], item["quantity"]) for item in body["items"]],
            [(self.product.name, 2)],
        )
        self.assertEqual(body["coupon_code"], "SAVE10")

    def test_refreshed_order_carries_the_same_lines_and_discount(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        first = self.submit(coupon_code="SAVE10")
        self.assertEqual(first.status_code, 201, first.data)

        refreshed = self.submit(coupon_code="SAVE10")

        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        self.assertEqual(refreshed.data["coupon"], "SAVE10")
        self.assertEqual(refreshed.data["total_amount"], "900.00")
        self.assertEqual(
            refreshed.data["discount_amount"], first.data["discount_amount"]
        )
        self.assertEqual(
            list(
                OrderItem.objects.filter(order_id=first.data["id"])
                .order_by("product_id")
                .values_list("product_id", "quantity", "price", "subtotal")
            ),
            [(self.product.id, 2, self.product.price, self.product.price * 2)],
        )

    def test_refresh_after_settlement_does_not_block_a_new_distinct_order(self):
        first = self.submit()
        self.assertEqual(first.status_code, 201, first.data)
        Order.objects.filter(pk=first.data["id"]).update(
            status="confirmed", razorpay_payment_id="pay_SETTLED"
        )

        refreshed = self.submit()
        self.assertEqual(refreshed.status_code, 201, refreshed.data)
        self.assertNotEqual(refreshed.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 2)

    def test_refresh_after_settlement_still_collapses_on_a_second_identical_submit(
        self,
    ):
        first = self.submit()
        Order.objects.filter(pk=first.data["id"]).update(
            status="confirmed", razorpay_payment_id="pay_SETTLED"
        )
        second = self.submit()
        self.assertEqual(second.status_code, 201, second.data)

        again = self.submit()

        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(again.data["id"], second.data["id"])
        self.assertEqual(Order.objects.count(), 2)

    def test_a_changed_basket_mid_flow_is_a_new_purchase_not_a_refresh(self):
        self.make_coupon(code="SAVE10", discount_value="10")
        first = self.submit()
        self.assertEqual(first.status_code, 201, first.data)

        recouponed = self.submit(coupon_code="SAVE10")

        self.assertEqual(recouponed.status_code, 201, recouponed.data)
        self.assertNotEqual(recouponed.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(
            Order.objects.get(pk=recouponed.data["id"]).coupon.code, "SAVE10"
        )

    def test_two_refreshes_by_another_user_do_not_collapse_together(self):
        first = self.submit()
        self.assertEqual(first.status_code, 201, first.data)

        self.make_user("other")
        other_client = self.fresh_client()
        self.api_login("other", client=other_client)
        self.seed_session_cart([(self.product, 2)], client=other_client)

        theirs = other_client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )

        self.assertEqual(theirs.status_code, 201, theirs.data)
        self.assertNotEqual(theirs.data["id"], first.data["id"])
        self.assertEqual(Order.objects.aggregate(n=Count("id"))["n"], 2)

    def test_collapsed_refresh_does_not_mint_a_second_payment_intent(self):
        first = self.submit()
        refreshed = self.submit()

        client_mock = self.razorpay_mock(order_id="order_ONE")
        first_intent = self.client.post(
            "/api/orders/payment/", {"order_id": first.data["id"]}, format="json"
        )
        self.assertEqual(first_intent.status_code, 200, first_intent.data)
        second_intent = self.client.post(
            "/api/orders/payment/", {"order_id": refreshed.data["id"]}, format="json"
        )

        self.assertEqual(second_intent.status_code, 200, second_intent.data)
        self.assertEqual(
            second_intent.data["razorpay_order_id"],
            first_intent.data["razorpay_order_id"],
        )
        self.assertEqual(second_intent.data["amount"], first_intent.data["amount"])
        client_mock.order.create.assert_called_once()

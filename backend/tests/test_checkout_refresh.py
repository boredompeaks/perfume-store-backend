"""SPEC-21-5 [R-21.2.6]: checkout-refresh safety, server side.

A browser refresh (or a re-submitted form, or a second tab) re-issues the
checkout GET/POST surface mid-flow. The BROWSER leg belongs to SPEC-3-01 and
cannot be pinned here -- Playwright e2e is non-hermetic and its CI job
self-skips -- so this module pins only the SERVER-side invariant the browser
leg depends on: re-issuing checkout while a pending order for the same
submission exists collapses onto that order and never mints a second charge
target, and it never costs the shopper their cart.

The collapse mechanism itself is NOT re-specified here. It is owned by
SPEC-21-1 (``orders.views._find_duplicate_pending_order``, the pending-order
fingerprint guard) and SPEC-9-01 (the Idempotency-Key layer behind it); the
pins below assert the refresh-shape outcomes those two contracts already
guarantee, reusing their fixtures/helpers rather than restating the rules.
No test here hits the network -- Razorpay is untouched (mocked where a
payment intent is minted at all).
"""

from django.db.models import Count
from django.test import tag

from cart.models import Cart, CartItem
from common.models import AuditEvent
from common.testing import ApiTestCase
from orders.models import Order, OrderItem


@tag("orders")
class CheckoutRefreshSafetyTests(ApiTestCase):
    """Server-side invariants behind the checkout page's refresh."""

    def setUp(self):
        self.buyer = self.make_user("buyer")
        _, self.token = self.api_login("buyer")
        self.product = self.make_product(name="Rose Aurum", price="500.00", stock=10)
        self.seed_session_cart([(self.product, 2)])  # subtotal 1000.00

    # -- helpers ---------------------------------------------------------

    def cart_row(self):
        return Cart.objects.get(session_id=self.client.session.session_key)

    def submit(self, **overrides):
        """Re-issue POST /api/orders/checkout/ with the same form a refresh
        re-posts."""
        return self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(**overrides),
            format="json",
        )

    # -- 1. refresh mid-flow does not mint a second order ---------------

    def test_refresh_while_pending_collapses_onto_the_same_order(self):
        """The SPEC-21-1 fingerprint guard answering a refreshed submit: the
        still-payable order for the identical submission is returned, so the
        refreshed tab and the original tab share ONE charge target."""
        first = self.submit()
        self.assertEqual(first.status_code, 201, first.data)

        refreshed = self.submit()

        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        self.assertEqual(refreshed.data["id"], first.data["id"])
        self.assertEqual(refreshed.data["order_number"], first.data["order_number"])
        self.assertEqual(refreshed.data["total_amount"], first.data["total_amount"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        # exactly one creation audit row: the refresh is a replay, not a
        # second order's worth of history
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_CREATED
            ).count(),
            1,
        )

    def test_repeated_refreshes_never_accumulate_orders(self):
        """Impatient reload: many re-issues, still exactly one order."""
        first = self.submit()
        self.assertEqual(first.status_code, 201, first.data)

        for _ in range(3):
            refreshed = self.submit()
            self.assertEqual(refreshed.status_code, 200, refreshed.data)
            self.assertEqual(refreshed.data["id"], first.data["id"])

        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)

    def test_refreshed_order_is_the_single_one_that_can_be_charged(self):
        """The collapse matters because a second payable order would be a
        second charge target: only the collapsed order's id reaches the
        gateway, and only once."""
        first = self.submit()
        self.submit()  # the refresh

        client_mock = self.razorpay_mock(order_id="order_REFRESH")
        payment = self.client.post(
            "/api/orders/payment/", {"order_id": first.data["id"]}, format="json"
        )

        self.assertEqual(payment.status_code, 200, payment.data)
        self.assertEqual(payment.data["razorpay_order_id"], "order_REFRESH")
        client_mock.order.create.assert_called_once()

    # -- 2. the cart survives the refresh --------------------------------

    def test_refresh_leaves_every_cart_line_and_the_coupon_intact(self):
        """Checkout never clears the cart (cleanup is a post-payment step),
        so a refresh mid-flow must find the cart exactly as the shopper left
        it -- lines, quantities and the applied coupon."""
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
        # the SPA's boot read agrees with the row: the badge a refresh paints
        # shows the cart the customer still has
        body = self.client.get("/api/cart/").data
        self.assertEqual(
            [(item["product"]["name"], item["quantity"]) for item in body["items"]],
            [(self.product.name, 2)],
        )
        self.assertEqual(body["coupon_code"], "SAVE10")

    def test_refreshed_order_carries_the_same_lines_and_discount(self):
        """The replay is the same order, not a rebuilt one: its line
        snapshots and its coupon/discount are byte-identical, so the money
        the customer is charged cannot shift under them."""
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

    # -- 3. a settled refresh must not block the next real purchase ------

    def test_refresh_after_settlement_does_not_block_a_new_distinct_order(self):
        """Once the first order is settled it is out of the fingerprint
        guard's payable set, so a genuinely different basket is free to mint
        its own order -- the guard never becomes a permanent block on a
        shopper who simply buys again."""
        first = self.submit()
        self.assertEqual(first.status_code, 201, first.data)
        Order.objects.filter(pk=first.data["id"]).update(
            status="confirmed", razorpay_payment_id="pay_SETTLED"
        )

        # the refresh of the OLD page (identical submission) now starts a
        # fresh order instead of being collapsed onto the settled one
        refreshed = self.submit()
        self.assertEqual(refreshed.status_code, 201, refreshed.data)
        self.assertNotEqual(refreshed.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 2)

    def test_refresh_after_settlement_still_collapses_on_a_second_identical_submit(
        self,
    ):
        """The new order is itself pending, so the very next refresh
        collapses again -- the gate is re-armed per order, not opened."""
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
        """The other half of the boundary: a refresh re-posts what is on
        screen, so it can only ever be the SAME submission. A basket the
        shopper deliberately changed (here: a coupon applied after the first
        order) is a different fingerprint and must mint its own order."""
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
        """The guard is per-user: a second buyer refreshing their own
        checkout keeps their own order, so a busy refresh storm never
        collapses two shoppers into one another."""
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

    # -- 4. the refresh surface is honest about what it returns ----------

    def test_collapsed_refresh_does_not_mint_a_second_payment_intent(self):
        """A refreshed tab that then re-runs the payment step for the order
        it was handed reuses the gateway intent already on that row, so the
        shopper is never shown a second payable amount for one order."""
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

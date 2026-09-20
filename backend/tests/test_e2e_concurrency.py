"""E2E concurrency/pagination tests - docs/test-gaps.md e2e items 5, 6, 9.

The verify endpoint serializes concurrent payments via
``transaction.atomic`` + ``select_for_update`` on Order/Products/Coupon, so
the race is exercised by resolving the two verifications against the same
rows in sequence - exactly the interleaving the row locks permit.
"""
from django.test import tag
from django.utils import timezone

from cart.models import Cart, CartItem
from common.testing import ApiTestCase
from orders.models import Order
from products.models import StockReservation, products as Product


@tag("e2e")
class OversellRaceTests(ApiTestCase):
    # e2e 5. two checkouts, one item left -> exactly one verify succeeds
    def test_oversell_allows_exactly_one_successful_verify(self):
        product = self.make_product(price="100.00", stock=1)
        clients = {}
        for name in ("alice", "bob"):
            self.make_user(name)
            clients[name] = self.fresh_client()
            self.api_login(name, client=clients[name])
            self.seed_session_cart([(product, 1)], client=clients[name])

        # [R-21.2.6] deliberate contract: the checkout dedup guard (SPEC-21-1)
        # collapses only SAME-user identical resubmissions onto one order; two
        # DIFFERENT users keep two orders (both 201) and stock stays untouched
        # until verify -- the oversell decision deliberately remains at
        # verify_payment, where exactly one row-locked verify wins. The
        # same-user collapse is pinned in orders/tests.py CheckoutDedupTests.
        order_ids = {}
        for name, client in clients.items():
            res = client.post("/api/orders/checkout/", self.checkout_payload(), format="json")
            self.assertEqual(res.status_code, 201, res.data)
            order_ids[name] = res.data["id"]
        self.assertEqual(Order.objects.count(), 2)

        # both payments get a razorpay order (mocked; ids are unique per order)
        client_mock = self.razorpay_mock()
        client_mock.order.create.side_effect = [{"id": "order_RACE1"}, {"id": "order_RACE2"}]
        for name, client in clients.items():
            res = client.post("/api/orders/payment/", {"order_id": order_ids[name]}, format="json")
            self.assertEqual(res.status_code, 200, res.data)

        # first verify wins: stock 1 -> 0
        first = clients["alice"].post(
            "/api/orders/payment/verify/",
            {
                "order_id": order_ids["alice"],
                "razorpay_order_id": "order_RACE1",
                "razorpay_payment_id": "pay_RACE1",
                "razorpay_signature": "sig",
            },
            format="json",
        )
        self.assertEqual(first.status_code, 200, first.data)

        # second verify hits the same locked rows: stock already gone -> 409
        second = clients["bob"].post(
            "/api/orders/payment/verify/",
            {
                "order_id": order_ids["bob"],
                "razorpay_order_id": "order_RACE2",
                "razorpay_payment_id": "pay_RACE2",
                "razorpay_signature": "sig",
            },
            format="json",
        )
        self.assertEqual(second.status_code, 409, second.data)
        self.assertEqual(second.data["error"], "An item is no longer available in the requested quantity")

        # exactly one confirmed, stock sold out once, loser stays pending
        statuses = {row["id"]: row["status"] for row in Order.objects.values("id", "status")}
        self.assertEqual(statuses[order_ids["alice"]], "confirmed")
        self.assertEqual(statuses[order_ids["bob"]], "pending")
        product.refresh_from_db()
        self.assertEqual(product.stock, 0)

        # [SPEC-12-02] the same race through the reservation lens: both
        # checkouts minted a hold on the last unit (soft overbooking,
        # by-design); the winner's hold converted into the committed sale
        # and the 409 backstop released the loser's, so no phantom active
        # hold survives the race.
        alice_hold = Order.objects.get(id=order_ids["alice"]).stock_reservations.get()
        bob_hold = Order.objects.get(id=order_ids["bob"]).stock_reservations.get()
        self.assertEqual(alice_hold.status, StockReservation.Status.CONVERTED)
        self.assertEqual(alice_hold.quantity, 1)
        self.assertEqual(bob_hold.status, StockReservation.Status.RELEASED)
        self.assertEqual(bob_hold.quantity, 1)
        self.assertEqual(
            StockReservation.objects.filter(status=StockReservation.Status.ACTIVE).count(),
            0,
        )


@tag("e2e")
class CouponRaceTests(ApiTestCase):
    # e2e 6. two concurrent verifications on usage_limit=1 -> exactly one increments
    def test_single_use_coupon_incremented_exactly_once(self):
        coupon = self.make_coupon(code="ONCE", discount_value="50", usage_limit=1)
        product = self.make_product(price="200.00", stock=5)
        clients = {}
        for name in ("alice", "bob"):
            self.make_user(name)
            clients[name] = self.fresh_client()
            self.api_login(name, client=clients[name])
            self.seed_session_cart([(product, 1)], client=clients[name])

        # both checkouts pass: used_count(0) < usage_limit(1) at checkout time
        order_ids = {}
        for name, client in clients.items():
            res = client.post(
                "/api/orders/checkout/", self.checkout_payload(coupon_code="ONCE"), format="json"
            )
            self.assertEqual(res.status_code, 201, res.data)
            self.assertEqual(res.data["total_amount"], "100.00")
            order_ids[name] = res.data["id"]

        client_mock = self.razorpay_mock()
        client_mock.order.create.side_effect = [{"id": "order_CPN1"}, {"id": "order_CPN2"}]
        for name, client in clients.items():
            res = client.post("/api/orders/payment/", {"order_id": order_ids[name]}, format="json")
            self.assertEqual(res.status_code, 200, res.data)

        first = clients["alice"].post(
            "/api/orders/payment/verify/",
            {
                "order_id": order_ids["alice"],
                "razorpay_order_id": "order_CPN1",
                "razorpay_payment_id": "pay_CPN1",
                "razorpay_signature": "sig",
            },
            format="json",
        )
        self.assertEqual(first.status_code, 200, first.data)

        second = clients["bob"].post(
            "/api/orders/payment/verify/",
            {
                "order_id": order_ids["bob"],
                "razorpay_order_id": "order_CPN2",
                "razorpay_payment_id": "pay_CPN2",
                "razorpay_signature": "sig",
            },
            format="json",
        )
        self.assertEqual(second.status_code, 409, second.data)
        self.assertEqual(second.data["error"], "The coupon is no longer valid")

        # exactly one increment, exactly one confirmed order
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        statuses = {row["id"]: row["status"] for row in Order.objects.values("id", "status")}
        self.assertEqual(statuses[order_ids["alice"]], "confirmed")
        self.assertEqual(statuses[order_ids["bob"]], "pending")
        product.refresh_from_db()
        self.assertEqual(product.stock, 4)  # only the winner's line was fulfilled


@tag("e2e")
class PaginationStabilityTests(ApiTestCase):
    # e2e 9. pagination is stable across pages even with equal created_at (F-12 context)
    def test_pages_partition_the_catalog_stably_across_repeated_requests(self):
        names = [f"Perfume {i}" for i in range(1, 6)]
        for name in names:
            self.make_product(name=name)
        # identical timestamps: ordering must not depend on timestamp jitter
        Product.objects.update(created_at=timezone.make_aware(timezone.datetime(2026, 1, 1, 12, 0, 0)))

        all_ids = []
        page = 1
        while True:
            res = self.client.get("/api/products/", {"page": page})
            self.assertEqual(res.status_code, 200, res.data)
            all_ids.extend(row["id"] for row in res.data["results"])
            if not res.data["next_page"]:
                break
            page += 1

        # every product appears exactly once across pages (no skips/repeats)
        self.assertEqual(sorted(all_ids), sorted(range(1, 6)))

        # a repeat walk yields the identical partition (stable across requests)
        repeat = []
        page = 1
        while True:
            res = self.client.get("/api/products/", {"page": page})
            repeat.extend(row["id"] for row in res.data["results"])
            if not res.data["next_page"]:
                break
            page += 1
        self.assertEqual(repeat, all_ids)

from django.conf import settings
from django.test import tag
from django.utils import timezone

from common.testing import ApiTestCase
from orders.models import Order
from products.models import StockReservation
from products.models import products as Product


@tag("e2e")
class OversellRaceTests(ApiTestCase):
    def test_oversell_allows_exactly_one_successful_verify(self):
        product = self.make_product(price="100.00", stock=1)
        clients = {}
        for name in ("alice", "bob"):
            self.make_user(name)
            clients[name] = self.fresh_client()
            self.api_login(name, client=clients[name])
            self.seed_session_cart([(product, 1)], client=clients[name])

        # The checkout dedup guard collapses only same-user identical resubmissions; the oversell decision deliberately stays at verify_payment, where one row-locked verify wins.
        order_ids = {}
        for name, client in clients.items():
            res = client.post(
                "/api/orders/checkout/", self.checkout_payload(), format="json"
            )
            self.assertEqual(res.status_code, 201, res.data)
            order_ids[name] = res.data["id"]
        self.assertEqual(Order.objects.count(), 2)

        client_mock = self.razorpay_mock()
        client_mock.order.create.side_effect = [
            {"id": "order_RACE1"},
            {"id": "order_RACE2"},
        ]
        for name, client in clients.items():
            res = client.post(
                "/api/orders/payment/", {"order_id": order_ids[name]}, format="json"
            )
            self.assertEqual(res.status_code, 200, res.data)

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
        self.assertEqual(
            second.data["error"],
            "An item is no longer available in the requested quantity",
        )

        statuses = {
            row["id"]: row["status"] for row in Order.objects.values("id", "status")
        }
        self.assertEqual(statuses[order_ids["alice"]], "confirmed")
        self.assertEqual(statuses[order_ids["bob"]], "pending")
        product.refresh_from_db()
        self.assertEqual(product.stock, 0)

        # The same race through the reservation lens: both checkouts hold the last unit (soft overbooking by design); the loser's hold is released by the 409 backstop.
        alice_hold = Order.objects.get(id=order_ids["alice"]).stock_reservations.get()
        bob_hold = Order.objects.get(id=order_ids["bob"]).stock_reservations.get()
        self.assertEqual(alice_hold.status, StockReservation.Status.CONVERTED)
        self.assertEqual(alice_hold.quantity, 1)
        self.assertEqual(bob_hold.status, StockReservation.Status.RELEASED)
        self.assertEqual(bob_hold.quantity, 1)
        self.assertEqual(
            StockReservation.objects.filter(
                status=StockReservation.Status.ACTIVE
            ).count(),
            0,
        )


@tag("e2e")
class CouponRaceTests(ApiTestCase):
    def test_single_use_coupon_incremented_exactly_once(self):
        coupon = self.make_coupon(code="ONCE", discount_value="50", usage_limit=1)
        product = self.make_product(price="200.00", stock=5)
        clients = {}
        for name in ("alice", "bob"):
            self.make_user(name)
            clients[name] = self.fresh_client()
            self.api_login(name, client=clients[name])
            self.seed_session_cart([(product, 1)], client=clients[name])

        order_ids = {}
        for name, client in clients.items():
            res = client.post(
                "/api/orders/checkout/",
                self.checkout_payload(coupon_code="ONCE"),
                format="json",
            )
            self.assertEqual(res.status_code, 201, res.data)
            self.assertEqual(res.data["total_amount"], "100.00")
            order_ids[name] = res.data["id"]

        client_mock = self.razorpay_mock()
        client_mock.order.create.side_effect = [
            {"id": "order_CPN1"},
            {"id": "order_CPN2"},
        ]
        for name, client in clients.items():
            res = client.post(
                "/api/orders/payment/", {"order_id": order_ids[name]}, format="json"
            )
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

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        statuses = {
            row["id"]: row["status"] for row in Order.objects.values("id", "status")
        }
        self.assertEqual(statuses[order_ids["alice"]], "confirmed")
        self.assertEqual(statuses[order_ids["bob"]], "pending")
        product.refresh_from_db()
        self.assertEqual(product.stock, 4)


@tag("e2e")
class PaginationStabilityTests(ApiTestCase):
    def test_pages_partition_the_catalog_stably_across_repeated_requests(self):
        # Enough rows to cross a page boundary; derived from the configured page size so a page-size change cannot quietly stop crossing it.
        page_size = settings.PRODUCTS_PAGE_SIZE
        names = [f"Perfume {i}" for i in range(1, 2 * page_size + 1)]
        for name in names:
            self.make_product(name=name)
        Product.objects.update(
            created_at=timezone.make_aware(timezone.datetime(2026, 1, 1, 12, 0, 0))
        )

        all_ids = []
        page = 1
        pages_walked = 0
        while True:
            res = self.client.get("/api/products/", {"page": page})
            self.assertEqual(res.status_code, 200, res.data)
            self.assertTrue(res.data["results"], f"page {page} came back empty")
            all_ids.extend(row["id"] for row in res.data["results"])
            pages_walked += 1
            if not res.data["next_page"]:
                break
            page += 1

        # The walk really did cross a page boundary, or the rest of this test
        # is asserting that one page is returned intact.
        self.assertGreater(pages_walked, 1, "the catalogue fit on a single page")

        # Measured against the rows this test created, not a literal id range: a sequence is not transactional, so PostgreSQL does not start at pk 1.
        self.assertEqual(
            sorted(all_ids),
            sorted(Product.objects.values_list("id", flat=True)),
        )

        repeat = []
        page = 1
        while True:
            res = self.client.get("/api/products/", {"page": page})
            repeat.extend(row["id"] for row in res.data["results"])
            if not res.data["next_page"]:
                break
            page += 1
        self.assertEqual(repeat, all_ids)

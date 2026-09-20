"""SPEC-8-01 [R-8.4]/[R-8.5]: customer-facing order numbers.

``order_number`` (ORD-YYYY-NNNNNN, per-year sequence) is minted inside
create_order's atomic block; the sequential pk stays the internal/URL key.
The race proof uses the forced collision-retry path (conventions.md:17:
IntegrityError retry, never check-then-act) plus a unique-constraint pin --
both deterministic -- instead of wall-clock thread scheduling. Razorpay is
always mocked; no test touches the network.
"""
import re
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError
from django.test import tag
from django.utils import timezone

from common.testing import ApiTestCase
from orders import views as order_views
from orders.admin import OrderAdmin
from orders.models import Order
from orders.serializers import OrderSerializer


class OrderNumberTestBase(ApiTestCase):
    """Buyer with a seeded cart; helpers for raw rows and a second shopper."""

    def setUp(self):
        self.buyer = self.make_user("buyer")
        _, self.token = self.api_login("buyer")
        self.product = self.make_product(name="Rose Aurum", price="500.00", stock=10)
        self.seed_session_cart([(self.product, 2)])  # subtotal 1000.00

    def raw_order(self, number=None, user=None):
        """A direct ORM row, bypassing checkout (mirrors the legacy fixture
        shape that the nullable column must keep supporting)."""
        return Order.objects.create(
            user=user or self.buyer,
            full_name="R",
            phone="1",
            address="a",
            city="c",
            state="s",
            pincode="1",
            total_amount=Decimal("10.00"),
            order_number=number,
        )

    def second_buyer_checkout(self):
        """The dedup guard is keyed per user, so a second order enters the
        sequence through a different shopper's identical checkout."""
        self.make_user("other")
        other_client = self.fresh_client()
        self.api_login("other", client=other_client)
        self.seed_session_cart([(self.product, 2)], client=other_client)
        res = other_client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        return res


@tag("orders")
class OrderNumberFormatTests(OrderNumberTestBase):
    """[R-8.4] format pin and per-year sequence semantics."""

    def test_checkout_mints_spec_format_order_number(self):
        res = self.checkout()

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        match = re.fullmatch(r"ORD-(\d{4})-(\d{6})", order.order_number)
        self.assertIsNotNone(match, order.order_number)
        self.assertEqual(int(match.group(1)), timezone.now().year)
        # the customer-facing reference is exposed at creation time
        self.assertEqual(res.data["order_number"], order.order_number)

    def test_sequence_increments_within_a_year(self):
        first_res = self.checkout()
        second_res = self.second_buyer_checkout()

        year = timezone.now().year
        first = Order.objects.get(id=first_res.data["id"])
        second = Order.objects.get(id=second_res.data["id"])
        self.assertEqual(first.order_number, f"ORD-{year}-000001")
        self.assertEqual(second.order_number, f"ORD-{year}-000002")

    def test_sequence_restarts_each_year(self):
        """The sequence bucket is the creation year: next year's first order
        starts at 000001 again, and this year's numbers are untouched."""
        self.checkout()
        next_year = timezone.now().year + 1

        self.make_user("other")
        other_client = self.fresh_client()
        self.api_login("other", client=other_client)
        self.seed_session_cart([(self.product, 2)], client=other_client)
        with mock.patch.object(order_views, "_current_year", return_value=next_year):
            res = other_client.post(
                "/api/orders/checkout/", self.checkout_payload(), format="json"
            )

        self.assertEqual(res.status_code, 201, res.data)
        second = Order.objects.get(id=res.data["id"])
        self.assertEqual(second.order_number, f"ORD-{next_year}-000001")
        self.assertEqual(Order.objects.count(), 2)


@tag("orders")
class OrderNumberRaceTests(OrderNumberTestBase):
    """conventions.md:17 -- unique generation handles IntegrityError retry.
    The max+1 lookup is only a fast path; the unique constraint on
    Order.order_number is the concurrency authority."""

    def test_unique_constraint_is_the_race_authority(self):
        self.raw_order(number="ORD-2026-000001")

        with self.assertRaises(IntegrityError):
            self.raw_order(number="ORD-2026-000001")

    def test_collision_retries_with_a_fresh_candidate(self):
        """Two simultaneous creations that read the same stale max: the
        loser's first candidate collides with the committed order, the retry
        regenerates and commits -- never a duplicate number."""
        first_res = self.checkout()
        first = Order.objects.get(id=first_res.data["id"])
        fresh = order_views._generate_order_number()
        self.assertNotEqual(fresh, first.order_number)

        with mock.patch.object(
            order_views,
            "_generate_order_number",
            side_effect=[first.order_number, fresh],
        ) as generate:
            res = self.second_buyer_checkout()

        self.assertEqual(res.status_code, 201, res.data)
        second = Order.objects.get(id=res.data["id"])
        self.assertEqual(second.order_number, fresh)
        self.assertEqual(generate.call_count, 2)  # exactly one retry
        self.assertEqual(Order.objects.count(), 2)
        numbers = set(Order.objects.values_list("order_number", flat=True))
        self.assertEqual(len(numbers), 2)  # no duplicate was committed

    def test_exhausted_retries_rollback_and_raise(self):
        """A checkout that keeps losing the race burns its bounded attempts
        and fails loudly with nothing left behind -- it never commits a
        duplicate and never loops forever."""
        first_res = self.checkout()
        first = Order.objects.get(id=first_res.data["id"])

        self.make_user("other")
        other_client = self.fresh_client()
        self.api_login("other", client=other_client)
        self.seed_session_cart([(self.product, 2)], client=other_client)
        with mock.patch.object(
            order_views,
            "_generate_order_number",
            side_effect=[first.order_number] * order_views.ORDER_NUMBER_ATTEMPTS,
        ):
            with self.assertRaises(IntegrityError):
                other_client.post(
                    "/api/orders/checkout/", self.checkout_payload(), format="json"
                )

        # the losing transaction rolled back: the winner's row is intact
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(
            Order.objects.get().order_number, first.order_number
        )


@tag("orders")
class OrderNumberExposureTests(OrderNumberTestBase):
    """[R-8.5] exposure strategy: order_number is the customer-facing
    reference; the sequential pk remains the internal/URL/admin key."""

    def test_replayed_checkout_returns_same_order_number(self):
        """Dedup interplay: a replayed checkout collapses onto the original
        order, so both responses carry the same reference."""
        first = self.checkout()
        self.assertEqual(first.status_code, 201, first.data)

        replay = self.checkout()

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(replay.data["order_number"], first.data["order_number"])
        self.assertEqual(Order.objects.count(), 1)

    def test_order_number_exposed_read_only_in_order_serializer(self):
        self.checkout()
        order = Order.objects.get()

        self.assertIn("order_number", OrderSerializer.Meta.fields)
        self.assertIn("order_number", OrderSerializer.Meta.read_only_fields)

        listing = self.client.get("/api/orders/")
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual(listing.data["results"][0]["order_number"], order.order_number)
        # the pk stays the internal key: untouched, still the URL id
        self.assertEqual(listing.data["results"][0]["id"], order.id)

    def test_admin_list_display_includes_order_number(self):
        self.assertIn("order_number", OrderAdmin.list_display)

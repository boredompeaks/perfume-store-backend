import contextlib
import random
import re
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError
from django.test import tag
from django.utils import timezone

from common.testing import ApiTestCase
from orders.admin import OrderAdmin
from orders.models import Order
from orders.serializers import OrderSerializer

# Patch targets must reach the module that owns the helpers: a function resolves its
# own globals, so rebinding them on the views facade would not affect the code under test.
from orders.views import checkout as checkout_views


class OrderNumberTestBase(ApiTestCase):
    def setUp(self):
        self.buyer = self.make_user("buyer")
        _, self.token = self.api_login("buyer")
        self.product = self.make_product(name="Rose Aurum", price="500.00", stock=10)
        self.seed_session_cart([(self.product, 2)])

    def raw_order(self, number=None, user=None):
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
    def test_checkout_mints_spec_format_order_number(self):
        res = self.checkout()

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        match = re.fullmatch(r"ORD-(\d{4})-([0-9A-F]{8})", order.order_number)
        self.assertIsNotNone(match, order.order_number)
        self.assertEqual(int(match.group(1)), timezone.now().year)
        self.assertEqual(res.data["order_number"], order.order_number)

    def test_order_numbers_are_unique_within_a_year(self):
        first_res = self.checkout()
        second_res = self.second_buyer_checkout()

        year = timezone.now().year
        first = Order.objects.get(id=first_res.data["id"])
        second = Order.objects.get(id=second_res.data["id"])
        self.assertRegex(first.order_number, rf"ORD-{year}-[0-9A-F]{{8}}")
        self.assertRegex(second.order_number, rf"ORD-{year}-[0-9A-F]{{8}}")
        self.assertNotEqual(first.order_number, second.order_number)

    def test_year_bucket_tracks_the_creation_year(self):
        self.checkout()
        next_year = timezone.now().year + 1

        self.make_user("other")
        other_client = self.fresh_client()
        self.api_login("other", client=other_client)
        self.seed_session_cart([(self.product, 2)], client=other_client)
        with mock.patch.object(checkout_views, "_current_year", return_value=next_year):
            res = other_client.post(
                "/api/orders/checkout/", self.checkout_payload(), format="json"
            )

        self.assertEqual(res.status_code, 201, res.data)
        second = Order.objects.get(id=res.data["id"])
        self.assertRegex(second.order_number, rf"ORD-{next_year}-[0-9A-F]{{8}}")
        self.assertEqual(Order.objects.count(), 2)


@tag("orders")
class OrderNumberEntropyTests(OrderNumberTestBase):
    def test_suffix_is_secrets_token_hex_and_not_the_random_module(self):
        year = timezone.now().year
        with contextlib.ExitStack() as stack:
            token_hex = stack.enter_context(
                mock.patch.object(
                    checkout_views.secrets, "token_hex", return_value="deadbeef"
                )
            )
            for name in ("getrandbits", "choice", "randint", "randrange"):
                stack.enter_context(
                    mock.patch.object(
                        random,
                        name,
                        side_effect=AssertionError(f"random.{name} was called"),
                    )
                )
            number = checkout_views._generate_order_number()

        self.assertEqual(number, f"ORD-{year}-DEADBEEF")
        token_hex.assert_called_once_with(4)


@tag("orders")
class OrderNumberRaceTests(OrderNumberTestBase):
    """Unique generation handles IntegrityError retries: the order_number constraint is the concurrency authority, and the generator only mints candidates."""

    def test_unique_constraint_is_the_race_authority(self):
        self.raw_order(number="ORD-2026-000001")

        with self.assertRaises(IntegrityError):
            self.raw_order(number="ORD-2026-000001")

    def test_collision_retries_with_a_fresh_candidate(self):
        first_res = self.checkout()
        first = Order.objects.get(id=first_res.data["id"])
        fresh = checkout_views._generate_order_number()
        self.assertNotEqual(fresh, first.order_number)

        with mock.patch.object(
            checkout_views,
            "_generate_order_number",
            side_effect=[first.order_number, fresh],
        ) as generate:
            res = self.second_buyer_checkout()

        self.assertEqual(res.status_code, 201, res.data)
        second = Order.objects.get(id=res.data["id"])
        self.assertEqual(second.order_number, fresh)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(Order.objects.count(), 2)
        numbers = set(Order.objects.values_list("order_number", flat=True))
        self.assertEqual(len(numbers), 2)

    def test_exhausted_retries_rollback_and_raise(self):
        first_res = self.checkout()
        first = Order.objects.get(id=first_res.data["id"])

        self.make_user("other")
        other_client = self.fresh_client()
        self.api_login("other", client=other_client)
        self.seed_session_cart([(self.product, 2)], client=other_client)
        with mock.patch.object(
            checkout_views,
            "_generate_order_number",
            side_effect=[first.order_number] * checkout_views.ORDER_NUMBER_ATTEMPTS,
        ):
            with self.assertRaises(IntegrityError):
                other_client.post(
                    "/api/orders/checkout/", self.checkout_payload(), format="json"
                )

        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.get().order_number, first.order_number)


@tag("orders")
class OrderNumberExposureTests(OrderNumberTestBase):
    def test_replayed_checkout_returns_same_order_number(self):
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
        self.assertEqual(listing.data["results"][0]["id"], order.id)

    def test_admin_list_display_includes_order_number(self):
        self.assertIn("order_number", OrderAdmin.list_display)

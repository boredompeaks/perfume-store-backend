"""SPEC-9-01 [R-9.3.14]/[R-9.3.19]: checkout submit idempotency.

A client that sends an ``Idempotency-Key`` header on POST /api/orders/checkout/
declares every retry carrying the same value to be the SAME submission, so the
atomic creation block dedupes on (user, key): a replay returns the original
order and never mints a second order_number -- regardless of payload drift,
the dedup window, or a settled first attempt. Keyless clients keep the legacy
contract (the SPEC-21-1 fingerprint guard plus window), which this layer
composes behind. Races are pinned deterministically -- the (user, key) unique
constraint plus a NULL-distinctness pin -- instead of wall-clock thread
scheduling (house pattern from test_order_number.py). Razorpay is never
touched; no test hits the network.
"""
from decimal import Decimal

from django.db import IntegrityError
from django.test import override_settings, tag

from common.models import AuditEvent
from common.testing import ApiTestCase
from orders.models import Order, OrderItem


class IdempotencyTestBase(ApiTestCase):
    """Buyer with a seeded cart plus helpers for keyed checkouts."""

    def setUp(self):
        self.buyer = self.make_user("buyer")
        _, self.token = self.api_login("buyer")
        self.product = self.make_product(name="Rose Aurum", price="500.00", stock=10)
        self.seed_session_cart([(self.product, 2)])  # subtotal 1000.00

    def keyed_checkout(self, key=None, client=None, **overrides):
        """Checkout with (or without, when ``key`` is None) the header."""
        target = client or self.client
        extra = {}
        if key is not None:
            extra["HTTP_IDEMPOTENCY_KEY"] = key
        return target.post(
            "/api/orders/checkout/",
            self.checkout_payload(**overrides),
            format="json",
            **extra,
        )

    def second_session(self):
        """The same buyer on an independent session (second tab/device),
        with its own cart -- the cross-session retry surface."""
        client = self.fresh_client()
        self.api_login("buyer", client=client)
        self.seed_session_cart([(self.product, 2)], client=client)
        return client

    def raw_order(self, key=None, user=None):
        """A direct ORM row, bypassing checkout (legacy-fixture shape)."""
        return Order.objects.create(
            user=user or self.buyer,
            full_name="R",
            phone="1",
            address="a",
            city="c",
            state="s",
            pincode="1",
            total_amount=Decimal("10.00"),
            idempotency_key=key,
        )


@tag("orders")
class IdempotencyBindingTests(IdempotencyTestBase):
    """Header parsing and key binding at creation time."""

    def test_keyed_submit_creates_order_bound_to_the_key(self):
        res = self.keyed_checkout(key="checkout-retry-001")

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertEqual(order.idempotency_key, "checkout-retry-001")
        self.assertEqual(res.data["order_number"], order.order_number)

    def test_absent_header_creates_order_without_key(self):
        """Regression pin: keyless clients keep the legacy contract."""
        res = self.checkout()

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertIsNone(order.idempotency_key)

    def test_blank_header_is_treated_as_no_key(self):
        res = self.keyed_checkout(key="   ")

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertIsNone(order.idempotency_key)

    def test_max_length_key_is_accepted(self):
        res = self.keyed_checkout(key="k" * 128)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            Order.objects.get(id=res.data["id"]).idempotency_key, "k" * 128
        )

    def test_oversized_key_rejected_400_without_side_effects(self):
        res = self.keyed_checkout(key="k" * 129)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "Idempotency-Key is too long")
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_CREATED
            ).count(),
            0,
        )

    def test_failed_keyed_submit_binds_nothing_and_key_stays_reusable(self):
        """A submission that never reaches creation must not burn the key:
        the retry after fixing the payload is the first one to bind it."""
        bad = self.keyed_checkout(key="checkout-retry-001", phone="")
        self.assertEqual(bad.status_code, 400, bad.data)
        self.assertEqual(Order.objects.count(), 0)

        good = self.keyed_checkout(key="checkout-retry-001")

        self.assertEqual(good.status_code, 201, good.data)
        self.assertEqual(
            Order.objects.get(id=good.data["id"]).idempotency_key,
            "checkout-retry-001",
        )


@tag("orders")
class IdempotencyReplayTests(IdempotencyTestBase):
    """A retried submit with the same key returns the original outcome and
    never mints a second order."""

    def test_keyed_replay_with_drifted_payload_returns_original_order(self):
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)

        # a drifted payload defeats the SPEC-21-1 fingerprint, so this pins
        # the key layer specifically
        replay = self.keyed_checkout(key="checkout-retry-001", city="Pune")

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(
            replay.data["order_number"], first.data["order_number"]
        )
        self.assertEqual(replay.data["total_amount"], first.data["total_amount"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_CREATED
            ).count(),
            1,
        )

    def test_keyed_replay_across_sessions_returns_original_order(self):
        """The cross-session surface the SPEC-21-1 cart lock cannot cover:
        the same buyer retries from a second tab with the same key."""
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)
        other_session = self.second_session()

        replay = self.keyed_checkout(
            key="checkout-retry-001", client=other_session
        )

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 1)

    def test_keyed_replay_survives_dedup_window_expiry(self):
        """Window expiry legitimately allows a keyless reorder (pinned in
        CheckoutDedupTests); a keyed client declared the retry, so the key
        collapses it onto the original order instead."""
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)

        with override_settings(CHECKOUT_DEDUP_WINDOW_SECONDS=0):
            replay = self.keyed_checkout(key="checkout-retry-001")

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 1)

    def test_keyed_replay_after_settlement_returns_original_order(self):
        """Past the payable gate the SPEC-21-1 guard deliberately stands
        down; a keyed retry still returns the settled original instead of
        minting a second charge target. A genuinely new purchase sends a
        new (or no) key."""
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)
        Order.objects.filter(pk=first.data["id"]).update(
            status="confirmed", razorpay_payment_id="pay_SETTLED"
        )

        replay = self.keyed_checkout(key="checkout-retry-001")

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 1)

    def test_same_key_for_another_user_is_independent(self):
        """Per-user scoping: user B reusing user A's key gets their own
        order and never learns that A's exists."""
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)
        self.make_user("other")
        other_client = self.fresh_client()
        self.api_login("other", client=other_client)
        self.seed_session_cart([(self.product, 2)], client=other_client)

        theirs = self.keyed_checkout(
            key="checkout-retry-001", client=other_client
        )

        self.assertEqual(theirs.status_code, 201, theirs.data)
        self.assertNotEqual(theirs.data["id"], first.data["id"])
        other_order = Order.objects.get(id=theirs.data["id"])
        self.assertEqual(other_order.user.username, "other")
        self.assertEqual(other_order.idempotency_key, "checkout-retry-001")

        # and their own retry replays their own order, still two orders
        replay = self.keyed_checkout(
            key="checkout-retry-001", client=other_client
        )
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], theirs.data["id"])
        self.assertEqual(Order.objects.count(), 2)


@tag("orders")
class IdempotencyRaceAuthorityTests(IdempotencyTestBase):
    """Deterministic race pins: the (user, idempotency_key) unique
    constraint is the concurrency authority (conventions.md:17), and
    keyless rows can never collide because NULLs stay distinct."""

    def test_unique_constraint_rejects_duplicate_key_for_same_user(self):
        self.raw_order(key="checkout-retry-001")

        with self.assertRaises(IntegrityError):
            self.raw_order(key="checkout-retry-001")

    def test_null_keys_stay_distinct(self):
        """Keyless checkouts must never trip the constraint: two legacy
        rows without keys coexist."""
        self.raw_order()
        self.raw_order()

        self.assertEqual(Order.objects.count(), 2)

    def test_unauthenticated_keyed_checkout_is_rejected(self):
        res = self.fresh_client().post(
            "/api/orders/checkout/",
            self.checkout_payload(),
            format="json",
            HTTP_IDEMPOTENCY_KEY="checkout-retry-001",
        )

        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(Order.objects.count(), 0)

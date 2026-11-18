from decimal import Decimal

from django.db import IntegrityError
from django.test import override_settings, tag

from common.models import AuditEvent
from common.testing import ApiTestCase
from orders.models import Order, OrderItem


class IdempotencyTestBase(ApiTestCase):
    def setUp(self):
        self.buyer = self.make_user("buyer")
        _, self.token = self.api_login("buyer")
        self.product = self.make_product(name="Rose Aurum", price="500.00", stock=10)
        self.seed_session_cart([(self.product, 2)])

    def keyed_checkout(self, key=None, client=None, **overrides):
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
        client = self.fresh_client()
        self.api_login("buyer", client=client)
        self.seed_session_cart([(self.product, 2)], client=client)
        return client

    def raw_order(self, key=None, user=None):
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
    def test_keyed_submit_creates_order_bound_to_the_key(self):
        res = self.keyed_checkout(key="checkout-retry-001")

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertEqual(order.idempotency_key, "checkout-retry-001")
        self.assertEqual(res.data["order_number"], order.order_number)

    def test_absent_header_creates_order_without_key(self):
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
        """A submission that never reaches creation must not burn the key, so the retry can still bind it."""
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
    def test_keyed_replay_with_drifted_payload_returns_original_order(self):
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)

        replay = self.keyed_checkout(key="checkout-retry-001", city="Pune")

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(replay.data["order_number"], first.data["order_number"])
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
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)
        other_session = self.second_session()

        replay = self.keyed_checkout(key="checkout-retry-001", client=other_session)

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 1)

    def test_keyed_replay_survives_dedup_window_expiry(self):
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)

        with override_settings(CHECKOUT_DEDUP_WINDOW_SECONDS=0):
            replay = self.keyed_checkout(key="checkout-retry-001")

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 1)

    def test_keyed_replay_after_settlement_returns_original_order(self):
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
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)
        self.make_user("other")
        other_client = self.fresh_client()
        self.api_login("other", client=other_client)
        self.seed_session_cart([(self.product, 2)], client=other_client)

        theirs = self.keyed_checkout(key="checkout-retry-001", client=other_client)

        self.assertEqual(theirs.status_code, 201, theirs.data)
        self.assertNotEqual(theirs.data["id"], first.data["id"])
        other_order = Order.objects.get(id=theirs.data["id"])
        self.assertEqual(other_order.user.username, "other")
        self.assertEqual(other_order.idempotency_key, "checkout-retry-001")

        replay = self.keyed_checkout(key="checkout-retry-001", client=other_client)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], theirs.data["id"])
        self.assertEqual(Order.objects.count(), 2)


@tag("orders")
class IdempotencyDedupCollapseTests(IdempotencyTestBase):
    """A keyed submit that collapses through the SPEC-21-1 keyless guard must
    carry the key onto the collapsed order: the first 2xx for a key owns its
    order across payload drift, so that key can never mint a second one."""

    def test_keyed_collapse_through_keyless_dedup_adopts_the_key(self):
        keyless = self.checkout()
        self.assertEqual(keyless.status_code, 201, keyless.data)

        # the same purchase retried with a key is still the keyless order
        keyed = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(keyed.status_code, 200, keyed.data)
        self.assertEqual(keyed.data["id"], keyless.data["id"])

        # the drift defeats the SPEC-21-1 fingerprint; only the key binding
        # can keep the retry on the original order instead of minting another
        drifted = self.keyed_checkout(key="checkout-retry-001", city="Pune")
        self.assertEqual(drifted.status_code, 200, drifted.data)
        self.assertEqual(drifted.data["id"], keyless.data["id"])
        self.assertEqual(drifted.data["order_number"], keyless.data["order_number"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(
            Order.objects.get(id=keyless.data["id"]).idempotency_key,
            "checkout-retry-001",
        )

    def test_a_second_key_never_takes_over_the_first_keys_order(self):
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)

        # same payload, different key: a different submission, so it must
        # neither resolve to K1's order nor overwrite K1's binding
        second = self.keyed_checkout(key="checkout-retry-002")
        self.assertEqual(second.status_code, 201, second.data)
        self.assertNotEqual(second.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(
            Order.objects.get(id=first.data["id"]).idempotency_key,
            "checkout-retry-001",
        )
        self.assertEqual(
            Order.objects.get(id=second.data["id"]).idempotency_key,
            "checkout-retry-002",
        )

        # each key keeps resolving to its own order
        replay_first = self.keyed_checkout(key="checkout-retry-001", city="Pune")
        replay_second = self.keyed_checkout(key="checkout-retry-002", city="Pune")
        self.assertEqual(replay_first.status_code, 200, replay_first.data)
        self.assertEqual(replay_first.data["id"], first.data["id"])
        self.assertEqual(replay_second.status_code, 200, replay_second.data)
        self.assertEqual(replay_second.data["id"], second.data["id"])
        self.assertEqual(Order.objects.count(), 2)

    def test_an_existing_key_wins_over_a_matching_keyless_candidate(self):
        """K1 already owns an order. A later K1 submission whose payload
        matches a keyless pending order is still K1's order, not that one:
        the candidate must be left unkeyed for its own caller."""
        first = self.keyed_checkout(key="checkout-retry-001")
        self.assertEqual(first.status_code, 201, first.data)

        keyless = self.checkout(city="Pune")
        self.assertEqual(keyless.status_code, 201, keyless.data)

        replay = self.keyed_checkout(key="checkout-retry-001", city="Pune")

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertNotEqual(replay.data["id"], keyless.data["id"])
        self.assertIsNone(Order.objects.get(id=keyless.data["id"]).idempotency_key)
        self.assertEqual(Order.objects.count(), 2)


@tag("orders")
class IdempotencyRaceAuthorityTests(IdempotencyTestBase):
    """The (user, idempotency_key) unique constraint is the concurrency authority; keyless NULL rows can never collide."""

    def test_unique_constraint_rejects_duplicate_key_for_same_user(self):
        self.raw_order(key="checkout-retry-001")

        with self.assertRaises(IntegrityError):
            self.raw_order(key="checkout-retry-001")

    def test_null_keys_stay_distinct(self):
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

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(
            res.data["error"],
            "Guest checkout is not available. "
            "Sign in or create an account to place an order.",
        )
        self.assertEqual(Order.objects.count(), 0)

"""SPEC-1-B04 (Section 1, [R-1.13]): guest checkout.

Spec line 74 gives the guest exactly one job -- "browse products and
optionally check out without an account" -- and spec 9.3 requires the checkout
to be bound "to the correct customer or guest session" while spec 21.2's first
acceptance row is that a guest can "complete an eligible purchase". Before
this, ``POST /api/orders/checkout/`` answered 401 to anyone without a JWT, so
that row could not hold: a guest could build a cart and then had nowhere to go.

What is pinned here, in the order the requirement reads:

* a guest order is created by the SAME endpoint an account order is, with no
  session user, a validated guest email, and a credential minted from
  ``secrets``;
* the guest reads their own order back by possessing that credential -- and
  every miss (no token, wrong token, unknown number, another guest's number
  with a valid token) answers with one byte-identical 404, so the endpoint
  confirms nothing about which orders exist;
* the authenticated path is untouched, including that a body-supplied
  guest_email on an account submission is ignored rather than stored;
* stock behaviour is identical: the checkout gate and the reservation hold are
  minted for a guest order exactly as for an account one, and no money
  arithmetic moved (all of it Decimal, all of it server-side);
* guest orders are visible to the SAME staff surfaces that see customer
  orders -- the admin grid (searchable by the guest email), the admin CSV
  export, the admin JSON seam, and the packing queue;
* nothing that dereferences ``order.user`` raises on a guest row: the order
  confirmation mail, the refund seam, the payment webhook, and the customer
  payment endpoints (which stay account-scoped, so a guest order is simply
  not theirs to find).

Razorpay is always mocked (``self.razorpay_mock``); the webhook tests sign
their own deliveries with the fixture secret and never touch the network.
"""

import hashlib
import hmac
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import override_settings, tag
from django.utils import timezone
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle

from common.models import AuditEvent
from common.roles import ROLE_FINANCE, ROLE_INVENTORY, ROLE_SUPPORT
from common.testing import ApiTestCase
from orders.models import Order, OrderStatusEvent, PaymentEvent, Refund
from orders.serializers import OrderSerializer
from orders.state import TRIGGER_ORDER_CREATE
from orders.views import GUEST_TOKEN_HEADER, create_order
from products.models import StockReservation

GUEST_EMAIL = "guest@example.com"
TOKEN_HEADER = "HTTP_" + GUEST_TOKEN_HEADER.upper().replace("-", "_")

# The webhook fixture secret (recognizably not a real credential, V-01).
WEBHOOK_SECRET = "TESTINGONLY-WEBHOOK-SECRET-DO-NOT-USE"
WEBHOOK_URL = "/api/v1/webhooks/razorpay/"


class GuestCheckoutTestBase(ApiTestCase):
    """A guest browser: seeded session cart, no account, its own cookie jar."""

    def setUp(self):
        self.product = self.make_product(
            name="Rose Aurum", price="500.00", stock=10, category="Floral"
        )
        self.guest = self.fresh_client()
        self.seed_session_cart([(self.product, 2)], client=self.guest)  # 1000.00

    def guest_checkout(self, client=None, idempotency_key=None, **overrides):
        payload = self.checkout_payload(
            **overrides,
        )
        payload.setdefault("guest_email", GUEST_EMAIL)
        headers = {}
        if idempotency_key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = idempotency_key
        return (client or self.guest).post(
            "/api/orders/checkout/", payload, format="json", **headers
        )

    def place_guest_order(self, client=None, **overrides):
        """Checkout, asserting 201, and return (order, response)."""
        res = self.guest_checkout(client=client, **overrides)
        self.assertEqual(res.status_code, 201, res.data)
        return Order.objects.get(id=res.data["id"]), res

    def guest_token_header(self, token):
        return {TOKEN_HEADER: token}

    @staticmethod
    def user_with_role(username, role):
        """A staff account holding exactly one role, as the capability map
        reads it (group membership intersected with STAFF_ROLES)."""
        user = User.objects.create_user(
            username, f"{username}@example.com", "S3cure-Passphrase!", is_staff=True
        )
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user


@tag("orders")
class GuestCheckoutCreationTests(GuestCheckoutTestBase):
    """[R-1.13] The guest submission itself."""

    def test_a_guest_places_an_order_with_no_account_and_no_login(self):
        order, res = self.place_guest_order()

        self.assertIsNone(order.user)
        self.assertEqual(order.guest_email, GUEST_EMAIL)
        self.assertEqual(order.order_number, res.data["order_number"])
        self.assertEqual(order.status, "pending")
        # Money is untouched by this task: still Decimal, still server-side,
        # still the same total a customer checkout of this cart produces.
        self.assertIsInstance(order.total_amount, Decimal)
        self.assertEqual(order.total_amount, Decimal("1000.00"))
        self.assertEqual(res.data["total_amount"], "1000.00")
        self.assertIsNone(res.data["user"])
        self.assertEqual(res.data["guest_email"], GUEST_EMAIL)
        self.assertEqual(order.items.get().quantity, 2)

    def test_the_response_carries_the_token_that_can_read_the_order_back(self):
        order, res = self.place_guest_order()

        self.assertEqual(res.data["guest_token"], order.guest_token)
        self.assertTrue(order.guest_token)

    def test_the_token_is_minted_by_the_secrets_module(self):
        """conventions.md/the requirement: `secrets`, never `random`. Proved by
        pinning the call itself -- a counter or a PRNG would not be routed
        through ``secrets.token_urlsafe`` at all."""
        with mock.patch(
            "orders.views.secrets.token_urlsafe", return_value="SENTINEL-TOKEN"
        ) as mint:
            order, res = self.place_guest_order()

        mint.assert_called_once_with(32)
        self.assertEqual(order.guest_token, "SENTINEL-TOKEN")
        self.assertEqual(res.data["guest_token"], "SENTINEL-TOKEN")

    def test_the_token_is_long_and_never_reused(self):
        first, _ = self.place_guest_order()
        # A second browser, same guest email, its own cart: an independent
        # submission must mint an independent credential.
        other = self.fresh_client()
        self.seed_session_cart([(self.product, 1)], client=other)
        second, _ = self.place_guest_order(client=other)

        self.assertNotEqual(first.guest_token, second.guest_token)
        # 32 CSPRNG bytes render as 43 URL-safe characters - wide enough that
        # the token space cannot be walked, narrow enough for the column.
        self.assertEqual(len(first.guest_token), 43)
        self.assertEqual(len(second.guest_token), 43)

    def test_an_account_submission_ignores_a_body_supplied_guest_email(self):
        self.make_user("buyer")
        self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])

        res = self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(guest_email="impostor@example.com"),
            format="json",
        )

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertEqual(order.user.username, "buyer")
        self.assertEqual(order.guest_email, "")
        self.assertIsNone(order.guest_token)
        # And no credential is handed to an authenticated caller: possession
        # of a guest token is the only guest authorization there is, so an
        # account order must not carry one.
        self.assertNotIn("guest_token", res.data)

    def test_an_invalid_guest_email_is_refused_and_writes_nothing(self):
        for bad in ("not-an-email", "guest@", "@example.com", "a b@example.com"):
            with self.subTest(guest_email=bad):
                res = self.guest_checkout(guest_email=bad)

                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(
                    res.data["error"], "guest_email must be a valid email address"
                )
                self.assertEqual(Order.objects.count(), 0)

    def test_an_over_length_guest_email_is_refused_before_the_row_exists(self):
        """[R-1.13] The Postgres max_length trap. `validate_email` accepts any
        length and SQLite ignores `varchar(254)`, so the pre-fix endpoint
        answered 201 and stored the address verbatim -- a DataError 500 on the
        production database. The pin is the BEHAVIOUR (a 400, nothing
        written), not the field declaration."""
        limit = Order._meta.get_field("guest_email").max_length

        # A syntactically valid address one character too wide.
        too_long = "a" * (limit - len("@corp.example")) + "@corp.example"
        self.assertEqual(len(too_long), limit)
        too_long += "x"

        res = self.guest_checkout(guest_email=too_long)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(
            res.data["error"], "guest_email must be at most 254 characters"
        )
        self.assertEqual(Order.objects.count(), 0)

        # The width boundary itself is still accepted, so the gate is the
        # column's width and not a rounder number.
        at_limit = too_long[:-1]
        self.assertEqual(len(at_limit), limit)
        accepted = self.guest_checkout(guest_email=at_limit)

        self.assertEqual(accepted.status_code, 201, accepted.data)
        self.assertEqual(Order.objects.count(), 1)

    def test_an_over_length_shipping_field_is_refused_before_the_row_exists(self):
        """[R-1.13] The same trap on the rest of the payload: `full_name`'s 150
        and `phone`'s 15 are varchar widths SQLite does not enforce and
        Postgres rejects. Refused before the atomic block opens."""
        for field, limit in (("full_name", 150), ("phone", 15)):
            with self.subTest(field=field):
                res = self.guest_checkout(**{field: "x" * (limit + 1)})

                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(
                    res.data["error"],
                    f"{field} must be at most {limit} characters",
                )
                self.assertEqual(Order.objects.count(), 0)
                # The account path shares the gate: this is the shipping
                # payload, not a guest-only field.
                self.auth(self.api_login("buyer")[1])
                account = self.checkout(**{field: "x" * (limit + 1)})
                self.assertEqual(account.status_code, 400, account.data)
                self.assertEqual(Order.objects.count(), 0)
                self.auth(None)

    def test_the_guest_email_is_stored_in_one_canonical_form(self):
        """[R-1.13] BUG-3: RFC 5321 local-parts are case-insensitive in
        practice, so `Victim@Corp.Example` and `victim@corp.example` are one
        buyer. Canonicalised at the boundary, or the owner filter addresses
        them as two rows and one customer gets two orders."""
        order, res = self.place_guest_order(guest_email="  Victim@Corp.Example  ")

        self.assertEqual(order.guest_email, "victim@corp.example")
        self.assertEqual(res.data["guest_email"], "victim@corp.example")

    def test_a_case_variant_of_the_guest_email_is_one_owner_not_two(self):
        """[R-1.13] BUG-3, the consequence: the keyed collapse is scoped by the
        stored address, so a spelling variant must resolve to the SAME owner
        and collapse -- pre-fix it minted a second, separately addressable
        order for the same person."""
        key = "case-variant-001"
        first, _ = self.place_guest_order(
            guest_email="victim@corp.example", idempotency_key=key
        )

        upper = self.fresh_client()
        self.seed_session_cart([(self.product, 2)], client=upper)
        replay = self.guest_checkout(
            client=upper, guest_email="VICTIM@CORP.EXAMPLE", idempotency_key=key
        )

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.id)
        self.assertEqual(Order.objects.count(), 1)

    def test_a_guest_checkout_has_no_cart_too(self):
        """The cart gate is not a login gate: it answers 404 the same way for
        a guest, so a sessionless caller still cannot invent an order."""
        cartless = self.fresh_client()

        res = self.guest_checkout(client=cartless)

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Cart not found")
        self.assertEqual(Order.objects.count(), 0)

    def test_the_stock_gate_still_refuses_a_guest_oversell(self):
        self.product.__class__.objects.filter(pk=self.product.pk).update(stock=1)

        res = self.guest_checkout()

        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("Rose Aurum", res.data["error"])
        self.assertEqual(Order.objects.count(), 0)
        self.assertFalse(StockReservation.objects.exists())

    def test_a_guest_order_reserves_its_units_with_no_account_attached(self):
        """[R-12.6] The hold is the oversell guarantee, and a guest checkout
        takes the same hold an account one does -- with a NULL owner, which is
        why StockReservation.owner is nullable."""
        order, _ = self.place_guest_order()

        hold = StockReservation.objects.get(order=order)
        self.assertIsNone(hold.owner_id)
        self.assertEqual(hold.product_id, self.product.id)
        self.assertEqual(hold.quantity, 2)
        self.assertEqual(hold.status, StockReservation.Status.ACTIVE)
        self.assertGreater(hold.expires_at, timezone.now())

    def test_the_trail_records_the_guest_order_without_an_actor(self):
        order, _ = self.place_guest_order()

        event = OrderStatusEvent.objects.get(order=order)
        self.assertIsNone(event.actor_id)
        self.assertEqual(event.trigger, TRIGGER_ORDER_CREATE)
        self.assertEqual(event.to_status, "pending")
        audit = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.ORDER_CREATED, order=order
        )
        self.assertIsNone(audit.actor_id)
        self.assertEqual(audit.detail["order_id"], order.id)

    def test_checkout_is_throttled_as_a_public_mutating_endpoint(self):
        """conventions.md:24. Checkout draws on the cart budget on purpose
        (it is the terminal mutation of the session-cart flow), so the pin is
        on that scope rather than on a new rate."""
        self.assertEqual(create_order.view_class.throttle_scope, "cart")
        self.assertIn("cart", api_settings.DEFAULT_THROTTLE_RATES)

        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["cart"] = "2/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            # Two cart adds to reach the checkout: the shared budget is what
            # makes this meaningful, so the third call is the checkout.
            self.assertEqual(
                self.guest.post(
                    "/api/cart/",
                    {"product_id": self.product.id, "quantity": 1},
                    format="json",
                ).status_code,
                201,
            )
            self.assertEqual(
                self.guest.post(
                    "/api/cart/",
                    {"product_id": self.product.id, "quantity": 1},
                    format="json",
                ).status_code,
                201,
            )
            throttled = self.guest_checkout()
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(Order.objects.count(), 0)

    def test_the_guest_order_carries_no_coupon_or_money_arithmetic_of_its_own(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10")

        order, res = self.place_guest_order(coupon_code=coupon.code)

        self.assertEqual(order.discount_amount, Decimal("100.00"))
        self.assertEqual(order.total_amount, Decimal("900.00"))
        self.assertEqual(res.data["coupon"], "SAVE10")
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 0)  # spent at payment, not at checkout


@tag("orders")
class GuestCheckoutReplayTests(GuestCheckoutTestBase):
    """[R-1.13] The two dedup guards, re-pointed at a guest owner.

    Both collapse onto the caller's OWN prior submission and neither can be
    used to reach another guest's order -- which is why the owner filter is
    (user IS NULL AND guest_email = ...) and never a bare user=None.
    """

    def test_an_identical_resubmission_collapses_onto_the_first_order(self):
        first, _ = self.place_guest_order()

        replay = self.guest_checkout()

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.id)
        self.assertEqual(Order.objects.count(), 1)

    def test_a_collapse_never_hands_back_the_credential(self):
        """[R-1.13] INVARIANT: `guest_token` is disclosed by the 201 that
        mints it and by nothing else -- not the keyless dedup collapse, not
        the keyed replay. A permanent read credential must not be replayable
        by whoever reaches a collapse, so the disclosure does not depend on
        any collapse guard being right."""
        first, minted = self.place_guest_order()
        self.assertIn("guest_token", minted.data)

        unkeyed = self.guest_checkout()
        self.assertEqual(unkeyed.status_code, 200, unkeyed.data)
        self.assertNotIn("guest_token", unkeyed.data)

        other = self.fresh_client()
        self.seed_session_cart([(self.product, 2)], client=other)
        keyed = self.guest_checkout(client=other, idempotency_key="collapse-token-001")
        self.assertEqual(keyed.status_code, 200, keyed.data)
        self.assertEqual(keyed.data["id"], first.id)
        self.assertNotIn("guest_token", keyed.data)

    def test_a_different_guest_email_never_collapses_onto_another_order(self):
        """The write-side counterpart of the read-side isolation probe: an
        identical payload under a different guest identity is a DIFFERENT
        purchase, not a replay of somebody else's."""
        first, _ = self.place_guest_order()

        other = self.fresh_client()
        self.seed_session_cart([(self.product, 2)], client=other)
        res = self.guest_checkout(client=other, guest_email="other@example.com")

        self.assertEqual(res.status_code, 201, res.data)
        self.assertNotEqual(res.data["id"], first.id)
        self.assertEqual(Order.objects.count(), 2)

    def test_a_keyed_guest_resubmission_collapses_onto_the_first_order(self):
        """The keyed collapse still works for a genuine retry: same guest,
        same cart, same payload, same key."""
        key = "guest-retry-001"

        first = self.guest.post(
            "/api/orders/checkout/",
            self.checkout_payload(guest_email=GUEST_EMAIL),
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(first.status_code, 201, first.data)

        replay = self.guest.post(
            "/api/orders/checkout/",
            self.checkout_payload(guest_email=GUEST_EMAIL),
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertNotIn("guest_token", replay.data)
        self.assertEqual(Order.objects.count(), 1)

    def test_a_keyed_replay_with_a_drifted_payload_is_refused(self):
        """[R-1.13] P1 FIX: the keyed guest path requires the WHOLE purchase
        to agree, so payload drift is a conflict, not a collapse onto
        somebody's order. Spec 9.3.14's drift-tolerance stands for an
        authenticated caller only."""
        key = "guest-retry-drift"

        first = self.guest.post(
            "/api/orders/checkout/",
            self.checkout_payload(guest_email=GUEST_EMAIL),
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(first.status_code, 201, first.data)

        replay = self.guest.post(
            "/api/orders/checkout/",
            self.checkout_payload(guest_email=GUEST_EMAIL, city="Pune"),
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertNotIn("id", replay.data)
        self.assertNotIn("guest_token", replay.data)
        self.assertEqual(Order.objects.count(), 1)

    def test_a_stolen_email_and_key_buy_the_attacker_nothing(self):
        """[R-1.13] P1 FIX, the exact repro: the victim checks out; an
        attacker from a FRESH session, with a DIFFERENT cart and their own
        address, replays the victim's email and Idempotency-Key. Before the
        fix this answered 200 with the victim's id, the victim's address and
        the victim's `guest_token`."""
        key = "abc-123"
        victim, victim_res = self.place_guest_order(
            guest_email="victim@corp.example", idempotency_key=key
        )

        attacker = self.fresh_client()
        self.seed_session_cart([(self.product, 1)], client=attacker)  # 500.00
        stolen = attacker.post(
            "/api/orders/checkout/",
            self.checkout_payload(
                guest_email="victim@corp.example",
                full_name="Attacker",
                phone="9999999999",
                address="1 Attacker Way",
                city="Delhi",
                state="Delhi",
                pincode="110001",
            ),
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

        self.assertEqual(stolen.status_code, 409, stolen.data)
        # Nothing of the victim's leaks: the body is the uniform error
        # envelope (SPEC-9-03) and carries no order detail at all.
        self.assertEqual(stolen.data["code"], "conflict")
        self.assertEqual(stolen.data["details"], {})
        self.assertNotIn("id", stolen.data)
        self.assertNotIn("guest_token", stolen.data)
        self.assertNotIn(str(victim.id), str(stolen.data))
        self.assertEqual(Order.objects.count(), 1)
        victim.refresh_from_db()
        self.assertEqual(victim.address, "12 Rose Lane")

        # ...and the credential the victim was given still reads only their
        # own order, so the attack neither leaked nor invalidated it.
        read = attacker.get(
            "/api/orders/guest/",
            **self.guest_token_header(victim_res.data["guest_token"]),
        )
        self.assertEqual(read.status_code, 200, read.data)
        self.assertEqual(read.data["id"], victim.id)

    def test_the_trailing_space_variant_of_a_stolen_email_buys_nothing(self):
        """[R-1.13] P1 + P2 FIX: the same replay dressed as
        `victim@corp.example ` (one trailing space). The address is
        canonicalised at the boundary, so it resolves to the same owner AND
        still has to agree on the purchase."""
        key = "abc-123"
        victim, _ = self.place_guest_order(
            guest_email="victim@corp.example", idempotency_key=key
        )

        attacker = self.fresh_client()
        self.seed_session_cart([(self.product, 1)], client=attacker)
        stolen = attacker.post(
            "/api/orders/checkout/",
            self.checkout_payload(
                guest_email="victim@corp.example   ",
                address="1 Attacker Way",
            ),
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

        self.assertEqual(stolen.status_code, 409, stolen.data)
        self.assertEqual(Order.objects.count(), 1)
        victim.refresh_from_db()
        self.assertEqual(victim.guest_email, "victim@corp.example")

    def test_an_account_keyed_replay_still_tolerates_payload_drift(self):
        """[R-1.13] The fix is scoped to the guest branch on purpose: an
        authenticated caller proved who they are at login, so SPEC-9.3.14's
        "a keyed replay collapses regardless of payload drift" is unchanged
        for them (orders.test_idempotency pins the same contract)."""
        buyer = self.make_user("drift-buyer")
        _, token = self.api_login("drift-buyer")
        self.auth(token)
        self.seed_session_cart([(self.product, 2)])
        key = {"HTTP_IDEMPOTENCY_KEY": "account-drift-001"}

        first = self.client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json", **key
        )
        self.assertEqual(first.status_code, 201, first.data)

        replay = self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(city="Pune"),
            format="json",
            **key,
        )

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(buyer.orders.count(), 1)

    def test_the_guest_key_constraint_is_the_database_authority(self):
        """conventions.md:17. The (user, key) constraint cannot see a guest row
        -- its user is NULL and NULLs stay distinct -- so the conditional
        (guest_email, key) constraint is what makes a keyed guest replay
        impossible at the database level, not just at the probe."""
        order, _ = self.place_guest_order()
        order.idempotency_key = "guest-retry-001"
        order.save(update_fields=["idempotency_key"])

        # Savepoint: the refused insert rolls back to here, leaving the test
        # transaction usable for the count below (the products app's own
        # unique-constraint pin uses the same shape).
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(
                    user=None,
                    guest_email=GUEST_EMAIL,
                    guest_token="another-token",
                    full_name="G",
                    phone="1",
                    address="a",
                    city="c",
                    state="s",
                    pincode="1",
                    total_amount=Decimal("10.00"),
                    idempotency_key="guest-retry-001",
                )
        self.assertEqual(Order.objects.count(), 1)


@tag("orders")
class GuestOrderLookupTests(GuestCheckoutTestBase):
    """[R-1.13] Possession of the token is the whole authorization."""

    def test_the_token_alone_returns_the_guests_own_order(self):
        order, _ = self.place_guest_order()

        res = self.guest.get(
            "/api/orders/guest/", **self.guest_token_header(order.guest_token)
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], order.id)
        self.assertEqual(res.data["order_number"], order.order_number)
        self.assertEqual(res.data["status"], "pending")
        self.assertEqual(res.data["items"][0]["product_name"], "Rose Aurum")

    def test_the_order_number_is_an_optional_cross_check_not_the_credential(self):
        order, _ = self.place_guest_order()

        res = self.guest.get(
            f"/api/orders/guest/{order.order_number}/",
            **self.guest_token_header(order.guest_token),
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], order.id)

    def test_the_lookup_is_served_on_the_v1_mirror(self):
        order, _ = self.place_guest_order()

        res = self.guest.get(
            f"/api/v1/store/orders/guest/{order.order_number}/",
            **self.guest_token_header(order.guest_token),
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], order.id)

    def test_no_token_is_refused(self):
        order, _ = self.place_guest_order()

        res = self.guest.get("/api/orders/guest/")

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Order not found")

    def test_a_wrong_token_is_refused(self):
        order, _ = self.place_guest_order()

        res = self.guest.get(
            "/api/orders/guest/", **self.guest_token_header("not-the-token")
        )

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Order not found")

    def test_another_guests_token_cannot_read_this_order_number(self):
        """The isolation probe: guest B holds a VALID token -- for B's own
        order -- and points it at guest A's order number. A lookup scoped to
        the token alone or to (number, token) both fail, and both fail
        identically to a lookup with no token at all."""
        theirs, _ = self.place_guest_order()
        mine_client = self.fresh_client()
        self.seed_session_cart([(self.product, 1)], client=mine_client)
        mine, _ = self.place_guest_order(
            client=mine_client, guest_email="mine@example.com"
        )

        stolen = mine_client.get(
            f"/api/orders/guest/{theirs.order_number}/",
            **self.guest_token_header(mine.guest_token),
        )
        anonymous = self.guest.get(f"/api/orders/guest/{theirs.order_number}/")

        self.assertEqual(stolen.status_code, 404, stolen.data)
        self.assertEqual(anonymous.status_code, 404, anonymous.data)
        # Byte-identical answers: nothing here distinguishes "somebody else's
        # token" from "no token at all", so the probe learns nothing about
        # whether theirs.order_number exists.
        self.assertEqual(stolen.data, anonymous.data)
        # ...and the token that DOES work still works for its own order.
        own = mine_client.get(
            f"/api/orders/guest/{mine.order_number}/",
            **self.guest_token_header(mine.guest_token),
        )
        self.assertEqual(own.status_code, 200, own.data)
        self.assertEqual(own.data["id"], mine.id)

    def test_a_token_cannot_be_paired_with_another_orders_number(self):
        order, _ = self.place_guest_order()
        other_client = self.fresh_client()
        self.seed_session_cart([(self.product, 1)], client=other_client)
        other, _ = self.place_guest_order(
            client=other_client, guest_email="other@example.com"
        )

        res = self.guest.get(
            f"/api/orders/guest/{other.order_number}/",
            **self.guest_token_header(order.guest_token),
        )

        self.assertEqual(res.status_code, 404, res.data)

    def test_an_unknown_order_number_with_a_valid_token_is_refused(self):
        order, _ = self.place_guest_order()

        res = self.guest.get(
            "/api/orders/guest/ORD-1999-999999/",
            **self.guest_token_header(order.guest_token),
        )

        self.assertEqual(res.status_code, 404, res.data)

    def test_an_account_order_is_not_reachable_through_the_guest_lookup(self):
        self.make_user("buyer")
        self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])
        res = self.client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        account_order = Order.objects.get(id=res.data["id"])
        self.assertIsNone(account_order.guest_token)

        probe = self.guest.get(
            f"/api/orders/guest/{account_order.order_number}/",
            **self.guest_token_header("anything-at-all"),
        )

        self.assertEqual(probe.status_code, 404, probe.data)

    def test_an_oversized_token_is_refused_before_the_index(self):
        res = self.guest.get("/api/orders/guest/", **self.guest_token_header("x" * 65))

        self.assertEqual(res.status_code, 404, res.data)

    def test_a_blank_token_header_is_refused(self):
        res = self.guest.get("/api/orders/guest/", **self.guest_token_header("   "))

        self.assertEqual(res.status_code, 404, res.data)

    def test_the_lookup_never_returns_the_credential(self):
        """The token is disclosed by the checkout response that minted it and
        by nothing else -- not here, and not on any staff read."""
        order, _ = self.place_guest_order()

        res = self.guest.get(
            "/api/orders/guest/", **self.guest_token_header(order.guest_token)
        )

        self.assertNotIn("guest_token", res.data)
        self.assertNotIn("guest_token", OrderSerializer(order).data)


@tag("orders")
class GuestOrderStaffSurfaceTests(GuestCheckoutTestBase):
    """[R-1.13] Requirement 5: a guest order is a first-class order for staff.

    Every surface a customer order appears on must carry a guest order too,
    or the fulfilment queue silently drops the store's sales.
    """

    def test_the_admin_json_seam_serves_guest_orders(self):
        order, _ = self.place_guest_order()
        self.client.force_authenticate(self.user_with_role("finmgr", ROLE_FINANCE))

        listing = self.client.get("/api/admin/orders/")
        detail = self.client.get(f"/api/admin/orders/{order.id}/")

        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertIn(order.id, [row["id"] for row in listing.data["results"]])
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertIsNone(detail.data["user"])
        self.assertEqual(detail.data["guest_email"], GUEST_EMAIL)
        # The staff read names the customer without carrying their credential.
        self.assertNotIn("guest_token", detail.data)

    def test_the_admin_grid_shows_and_searches_the_guest_order(self):
        order, _ = self.place_guest_order()
        admin = self.make_staff("root")
        self.client.force_login(admin)

        changelist = self.client.get("/admin/orders/order/")
        found = self.client.get("/admin/orders/order/", {"q": GUEST_EMAIL})

        self.assertEqual(changelist.status_code, 200)
        self.assertContains(changelist, order.order_number)
        self.assertEqual(found.status_code, 200)
        self.assertContains(found, order.order_number)

    def test_the_admin_change_form_renders_the_guest_order(self):
        order, _ = self.place_guest_order()
        admin = self.make_staff("root")
        self.client.force_login(admin)

        res = self.client.get(f"/admin/orders/order/{order.id}/change/")

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, GUEST_EMAIL)

    def test_the_csv_export_answers_for_a_guest_order(self):
        """export_csv walks the selected queryset reading ``customer``. A
        guest row has no user, so the label resolves to the guest email
        instead of raising AttributeError mid-export."""
        order, _ = self.place_guest_order()
        admin = self.make_staff("root")
        self.client.force_login(admin)

        res = self.client.post(
            "/admin/orders/order/",
            {"action": "export_csv", "_selected_action": [str(order.pk)]},
        )

        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn(GUEST_EMAIL, body)
        self.assertIn(str(order.total_amount), body)

    def test_the_packing_queue_lists_the_guest_order(self):
        """[R-1-B03] The inventory/fulfilment operator's queue is unscoped by
        owner, so a guest order has to appear in it -- otherwise the store
        cannot ship the sales this task lets it take. The money and the
        shipping record stay withheld exactly as B03 narrowed them."""
        order, _ = self.place_guest_order()
        operator = self.user_with_role("packer", ROLE_INVENTORY)
        self.client.force_login(operator)

        res = self.client.get("/admin/orders/order/")

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, str(order.id))
        self.assertContains(res, order.city)
        self.assertNotContains(res, str(order.total_amount))
        self.assertNotContains(res, order.full_name)
        self.assertNotContains(res, order.phone)
        # The guest address is not a lookup key for this role either: the
        # queue is searched by reference only.
        found = self.client.get("/admin/orders/order/", {"q": GUEST_EMAIL})
        self.assertEqual(found.status_code, 200)
        self.assertNotContains(found, order.order_number)

    def test_the_dashboard_labels_a_guest_order(self):
        order, _ = self.place_guest_order()

        self.assertEqual(str(order), f"Order #{order.id} - {GUEST_EMAIL}")
        self.assertEqual(order.customer_name, GUEST_EMAIL)
        self.assertEqual(order.recipient, GUEST_EMAIL)

    def test_an_account_order_keeps_its_account_labels(self):
        buyer = self.make_user("buyer", email="buyer@example.com")
        self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])
        res = self.client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])

        self.assertEqual(str(order), f"Order #{order.id} - buyer")
        self.assertEqual(order.customer_name, "buyer")
        self.assertEqual(order.recipient, buyer.email)


@override_settings(RAZORPAY_WEBHOOK_SECRET=WEBHOOK_SECRET)
class GuestOrderNullUserPathTests(GuestCheckoutTestBase):
    """[R-1.13] Requirement 6: nothing that reads ``order.user`` may raise on
    a guest row. Each case below is a path the guest order really reaches."""

    def deliver(self, body, event_id="evt_GUEST1"):
        payload = json.dumps(body).encode("utf-8")
        signature = hmac.new(
            WEBHOOK_SECRET.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        return self.client.post(
            WEBHOOK_URL,
            data=payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_EVENT_ID=event_id,
            HTTP_X_RAZORPAY_SIGNATURE=signature,
        )

    def capture_event(self, order, amount_minor=100000):
        return {
            "entity": "event",
            "account_id": "acc_TEST",
            "event": "payment.captured",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": "payment",
                    "id": "pay_GUEST1",
                    "amount": amount_minor,
                    "currency": "INR",
                    "order_id": order.razorpay_order_id,
                    "captured": True,
                    "status": "captured",
                }
            },
        }

    def pay_the_guest_order(self, order):
        """Stand in for create_payment, which stays account-scoped: the store
        hands the gateway reference to a guest order out of band."""
        order.razorpay_order_id = "order_GUEST1"
        order.save(update_fields=["razorpay_order_id"])

    def test_the_payment_webhook_confirms_a_guest_order(self):
        order, _ = self.place_guest_order()
        self.pay_the_guest_order(order)

        res = self.deliver(self.capture_event(order))

        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.payment_status, "captured")
        self.assertEqual(order.razorpay_payment_id, "pay_GUEST1")
        self.assertEqual(
            PaymentEvent.objects.get(event_id="evt_GUEST1").outcome, "applied"
        )
        # Inventory is deliberately the callback writer's job (webhooks.py's
        # own contract), so the webhook's guest-leg work is the transition,
        # the trail and the hold conversion - all of which happened.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(
            order.stock_reservations.get().status,
            StockReservation.Status.CONVERTED,
        )
        self.assertIsNone(order.status_events.latest("id").actor_id)

    def test_the_order_confirmation_email_reaches_the_guest(self):
        """The notification reads the order's recipient, so the guest gets
        the mail instead of an AttributeError swallowed into the log."""
        order, _ = self.place_guest_order()
        self.pay_the_guest_order(order)

        self.deliver(self.capture_event(order))

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [GUEST_EMAIL])
        self.assertEqual(order.recipient, GUEST_EMAIL)

    def test_the_refund_seam_refunds_a_captured_guest_order(self):
        order, _ = self.place_guest_order()
        order.status = "confirmed"
        order.payment_status = "captured"
        order.paid_at = timezone.now()
        order.razorpay_order_id = "order_GUEST2"
        order.razorpay_payment_id = "pay_GUEST2"
        order.save()
        self.client.force_authenticate(self.user_with_role("finmgr", ROLE_FINANCE))
        client = self.razorpay_mock()
        client.refund.create.return_value = {"id": "rfnd_GUEST"}

        res = self.client.post(
            f"/api/admin/orders/{order.id}/refund/",
            {"reason": "guest changed their mind"},
            format="json",
        )

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["payment_status"], "refunded")
        self.assertEqual(res.data["refundable_remaining"], Decimal("0.00"))
        # The refund's actor is the finance operator who issued it; the order
        # it was issued against having no account is what this pins.
        self.assertEqual(
            res.data["refund"]["actor_id"],
            User.objects.get(username="finmgr").id,
        )
        self.assertEqual(Refund.objects.get(order=order).kind, Refund.Kind.FULL)

    def test_cancelling_a_guest_order_releases_its_hold(self):
        """The stock-release path a cancelled checkout takes, on a guest row."""
        order, _ = self.place_guest_order()
        self.client.force_authenticate(self.user_with_role("supp", ROLE_SUPPORT))

        res = self.client.post(f"/api/admin/orders/{order.id}/cancel/")

        self.assertEqual(res.status_code, 200, res.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertIsNotNone(order.cancelled_at)
        self.assertEqual(
            order.stock_reservations.get().status,
            StockReservation.Status.RELEASED,
        )

    def test_the_customer_payment_seams_never_reach_a_guest_order(self):
        """create_payment/verify_payment stay account-scoped, and a guest
        order is outside every account's scope -- so the answer is the same
        uniform 404 an unknown id gets, not a 500 on the NULL user."""
        order, _ = self.place_guest_order()
        self.make_user("buyer")
        self.api_login("buyer")
        self.razorpay_mock()

        intent = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        verify = self.client.post(
            "/api/orders/payment/verify/",
            {
                "order_id": order.id,
                "razorpay_order_id": "order_GUEST1",
                "razorpay_payment_id": "pay_GUEST1",
                "razorpay_signature": "sig",
            },
            format="json",
        )

        self.assertEqual(intent.status_code, 404, intent.data)
        self.assertEqual(intent.data["error"], "Order not found")
        self.assertEqual(verify.status_code, 404, verify.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.payment_status, "pending")

    def test_the_customer_order_reads_never_include_a_guest_order(self):
        """An account's history and detail are scoped to that account, so a
        guest sale is not disclosed by any authenticated read."""
        order, _ = self.place_guest_order()
        self.make_user("buyer")
        self.api_login("buyer")

        history = self.client.get("/api/orders/")
        detail = self.client.get(f"/api/orders/{order.id}/")

        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual(history.data["count"], 0)
        self.assertEqual(detail.status_code, 404, detail.data)


@tag("orders")
class GuestOrderModelInvariantTests(ApiTestCase):
    """[R-1.13] The two-owner rule, as a database fact rather than a habit."""

    def test_an_order_with_no_owner_at_all_is_refused(self):
        with self.assertRaises(IntegrityError):
            Order.objects.create(
                user=None,
                full_name="N",
                phone="1",
                address="a",
                city="c",
                state="s",
                pincode="1",
                total_amount=Decimal("10.00"),
            )

    def test_an_order_with_two_owners_is_refused(self):
        with self.assertRaises(IntegrityError):
            Order.objects.create(
                user=self.make_user("buyer"),
                guest_email=GUEST_EMAIL,
                guest_token="token-alongside-an-account",
                full_name="B",
                phone="1",
                address="a",
                city="c",
                state="s",
                pincode="1",
                total_amount=Decimal("10.00"),
            )

    def test_the_guest_columns_are_wide_enough_on_every_engine(self):
        """The widths are declared for BOTH engines: SQLite ignores
        max_length, Postgres enforces it, so a token or an address that only
        fits the development database is a production error."""
        self.assertEqual(Order._meta.get_field("guest_email").max_length, 254)
        self.assertEqual(Order._meta.get_field("guest_token").max_length, 64)
        self.assertTrue(Order._meta.get_field("guest_token").null)
        self.assertTrue(Order._meta.get_field("user").null)
        self.assertEqual(StockReservation._meta.get_field("owner").null, True)
        self.assertEqual(Order._meta.get_field("guest_token").unique, True)

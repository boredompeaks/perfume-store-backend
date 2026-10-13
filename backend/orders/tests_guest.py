"""TIER1-GUEST-DEADEND: anonymous checkout is REFUSED, uniformly.

Owner decision 2026-10-09 (recorded in ``backend/docs/run/TIER1-launch.md``):
full guest checkout is deferred (SPEC-3-02 off the Tier-1 list). What the
store did in the meantime was the dead end this file used to pin the happy
path of -- an anonymous ``POST /api/orders/checkout/`` minted an order that
could never be paid (the payment endpoints are account-scoped) while it held
a stock reservation and carried the coupon FK: saleable inventory parked on
a purchase nobody could complete. The server now refuses every anonymous
submission with ONE uniform answer, creating NOTHING.

What is pinned here, in the order the requirement reads:

* an anonymous POST to checkout (either mount) answers the one uniform
  refusal -- same status, same bytes -- for EVERY guest reason, so the
  answer is not an oracle for which inputs any guest path would accept;
* the refusal creates NOTHING: no order row, no stock reservation, no
  creation audit trail, no coupon attachment, no stock movement, and the
  product's stock and the coupon's ``used_count`` are unchanged;
* the authenticated path is untouched, END TO END: checkout 201, payment
  intent, verify, stock decrement, coupon spend, hold conversion;
* guest order ROWS that predate the refusal are still data, so every read
  path that serves them stays pinned against ORM-built rows of the exact
  pre-refusal shape: the token lookup (one byte-identical 404 for every
  miss), the staff surfaces, the payment webhook, the refund and cancel
  seams, and the account-scoped reads that must never see a guest row;
* the two-owner rule and the column widths stay database facts.

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

from cart.models import Cart, CartItem
from common import notifications
from common.models import AuditEvent
from common.roles import ROLE_FINANCE, ROLE_INVENTORY, ROLE_SUPPORT
from common.testing import ApiTestCase
from orders.models import Order, OrderItem, OrderStatusEvent, PaymentEvent, Refund
from orders.serializers import OrderSerializer
from orders.state import TRIGGER_ORDER_CREATE
from orders.views import GUEST_TOKEN_HEADER, create_order
from products.models import StockMovement, StockReservation

GUEST_EMAIL = "guest@example.com"
TOKEN_HEADER = "HTTP_" + GUEST_TOKEN_HEADER.upper().replace("-", "_")

# Hand-written literal, deliberately NOT imported from the view: a pin whose
# expected value is read off the constant under test agrees with a wrong
# constant from both sides (conventions.md, evidence claims).
REFUSAL_MESSAGE = (
    "Guest checkout is not available. Sign in or create an account to place an order."
)

# The webhook fixture secret (recognizably not a real credential, V-01).
WEBHOOK_SECRET = "TESTINGONLY-WEBHOOK-SECRET-DO-NOT-USE"
WEBHOOK_URL = "/api/v1/webhooks/razorpay/"


class GuestRefusalTestBase(ApiTestCase):
    """A guest browser: seeded session cart, no account, its own cookie jar."""

    def setUp(self):
        self.product = self.make_product(
            name="Rose Aurum", price="500.00", stock=10, category="Floral"
        )
        self.guest = self.fresh_client()
        self.seed_session_cart([(self.product, 2)], client=self.guest)  # 1000.00

    def guest_post(
        self,
        client=None,
        path="/api/orders/checkout/",
        idempotency_key=None,
        **overrides,
    ):
        payload = self.checkout_payload(**overrides)
        headers = {}
        if idempotency_key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = idempotency_key
        return (client or self.guest).post(path, payload, format="json", **headers)

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
class GuestCheckoutRefusalTests(GuestRefusalTestBase):
    """(a)-(e) The refusal itself, and the nothing it leaves behind."""

    def test_anonymous_checkout_is_refused_with_one_clear_error(self):
        res = self.guest_post()

        self.assertEqual(res.status_code, 403, res.data)
        # The SPEC-9-03 uniform envelope: the middleware wraps the view's
        # {"error": ...} into the store-wide shape, and the status family
        # label for a 403 is "permission_denied" on every reason alike.
        self.assertEqual(res.data["error"], REFUSAL_MESSAGE)
        self.assertEqual(res.data["code"], "permission_denied")
        self.assertEqual(res.data["details"], {})

    def test_the_refusal_creates_no_order_row_reservation_or_trail(self):
        """(b) Nothing is minted: no order, no hold, no creation trail."""
        coupon = self.make_coupon(code="SAVE10", discount_value="10")

        res = self.guest_post(coupon_code=coupon.code)

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(OrderStatusEvent.objects.count(), 0)
        self.assertFalse(StockReservation.objects.exists())
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.ORDER_CREATED
            ).count(),
            0,
        )

    def test_stock_is_unchanged_by_a_refused_submission(self):
        """(c) The dead-end's whole harm was holding stock; the refusal
        holds none. Captured before/after around the POST, not typed."""
        stock_before = type(self.product).objects.get(pk=self.product.pk).stock

        res = self.guest_post()

        self.assertEqual(res.status_code, 403, res.data)
        stock_after = type(self.product).objects.get(pk=self.product.pk).stock
        self.assertEqual(stock_after, stock_before)
        self.assertEqual(stock_after, 10)  # the setUp fixture, untouched

    def test_coupon_usage_is_unchanged_by_a_refused_submission(self):
        """(d) The dead-end order used to carry the coupon FK from
        checkout; the refusal attaches nothing and spends nothing."""
        coupon = self.make_coupon(code="SAVE10", discount_value="10", usage_limit=5)
        self.assertEqual(coupon.used_count, 0)

        res = self.guest_post(coupon_code=coupon.code)

        self.assertEqual(res.status_code, 403, res.data)
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 0)
        self.assertFalse(Order.objects.filter(coupon=coupon).exists())

    def test_every_guest_reason_gets_the_byte_identical_refusal(self):
        """(e) The no-oracle property, as bytes: missing, invalid,
        over-length and casefold-variant guest_email; over-length shipping
        fields; known, unknown and over-long Idempotency-Keys; a valid and
        an invalid coupon -- one answer for all of them. A caller cannot
        probe which inputs any guest path would have accepted."""
        coupon = self.make_coupon(code="SAVE10", discount_value="10")
        too_long_email = "a" * 255
        reasons = {
            "no guest_email": {},
            "valid guest_email": {"guest_email": GUEST_EMAIL},
            "invalid guest_email": {"guest_email": "not-an-email"},
            "over-length guest_email": {"guest_email": too_long_email},
            "whitespace casefold guest_email": {
                "guest_email": "  Victim@Corp.Example  "
            },
            "over-length full_name": {"full_name": "x" * 151},
            "over-length phone": {"phone": "1" * 16},
            "unknown coupon": {"coupon_code": "NOSUCHCODE"},
            "valid coupon": {"coupon_code": coupon.code},
            "named shipping_method": {"shipping_method": "express"},
            "keyed": {"__idempotency_key__": "uniform-key-001"},
            "over-long key": {"__idempotency_key__": "k" * 129},
        }

        bodies = {}
        for label, kwargs in reasons.items():
            kwargs = dict(kwargs)
            key = kwargs.pop("__idempotency_key__", None)
            with self.subTest(reason=label):
                res = self.guest_post(idempotency_key=key, **kwargs)
                self.assertEqual(res.status_code, 403, res.data)
                self.assertEqual(res.data["error"], REFUSAL_MESSAGE)
                bodies[label] = res.content

        # Byte-identical across every reason, and still zero rows after all
        # of them (the loop's own writes would show here if any leaked).
        self.assertEqual(len(set(bodies.values())), 1, bodies)
        self.assertEqual(Order.objects.count(), 0)
        self.assertFalse(StockReservation.objects.exists())

    def test_an_oversell_attempt_gets_the_same_refusal_not_a_stock_error(self):
        """The stock gate is an ACCOUNT gate now: an anonymous submission
        never reaches it, so it cannot be used to distinguish this refusal
        from the differentiated 400s further down the authenticated path."""
        type(self.product).objects.filter(pk=self.product.pk).update(stock=1)
        baseline = self.guest_post()
        oversell = self.guest_post()  # cart asks 2, stock holds 1

        self.assertEqual(oversell.status_code, 403, oversell.data)
        self.assertEqual(oversell.content, baseline.content)
        self.assertEqual(Order.objects.count(), 0)

    def test_a_sessionless_caller_gets_the_same_refusal(self):
        """No cart, no session cookie: the same answer as every other
        reason -- not the "Cart not found" 404 the authenticated path
        (still) answers, which would split the anonymous surface."""
        res = self.guest_post(client=self.fresh_client())

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["error"], REFUSAL_MESSAGE)

    def test_an_empty_cart_does_not_change_the_answer(self):
        CartItem.objects.filter(
            cart__session_id=self.guest.session.session_key
        ).delete()

        res = self.guest_post()

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["error"], REFUSAL_MESSAGE)

    def test_the_refusal_answers_the_v1_mirror_byte_identically(self):
        """Both mounts are the same view; a caller learns nothing from
        switching the path either, and neither mount writes anything."""
        main = self.guest_post()
        mirrored = self.guest_post(path="/api/v1/store/orders/checkout/")

        self.assertEqual(mirrored.status_code, 403, mirrored.data)
        self.assertEqual(mirrored.content, main.content)
        self.assertEqual(Order.objects.count(), 0)

    def test_an_invalid_guest_email_is_refused_and_writes_nothing(self):
        """The invalid-email reason, pinned on its own as well as inside the
        byte-identical set: the old differentiated 400 ("must be a valid
        email address") is gone, and with it any per-reason write risk."""
        for bad in ("not-an-email", "guest@", "@example.com", "a b@example.com"):
            with self.subTest(guest_email=bad):
                res = self.guest_post(guest_email=bad)

                self.assertEqual(res.status_code, 403, res.data)
                self.assertEqual(res.data["error"], REFUSAL_MESSAGE)
                self.assertEqual(Order.objects.count(), 0)

    def test_an_empty_payload_gets_the_same_refusal(self):
        """Field-validation errors are not reachable anonymously either:
        a payload missing every required field still gets the one answer,
        so no 400-listing-a-field shape can be probed from outside."""
        res = self.guest.post("/api/orders/checkout/", {}, format="json")

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["error"], REFUSAL_MESSAGE)

    def test_the_refusal_response_carries_no_order_fields(self):
        """The body is the refusal and nothing else -- no id, no order
        number, no totals, no items, no credential."""
        res = self.guest_post()

        self.assertEqual(res.status_code, 403, res.data)
        for field in ("id", "order_number", "total_amount", "guest_token", "items"):
            self.assertNotIn(field, res.data)

    def test_the_refusal_leaves_the_callers_cart_intact(self):
        """Checkout-only: the refusal neither clears the cart nor its
        lines -- removal stays the caller's call, exactly as on the delist
        refusal, and a logged-in retry can still buy the same cart."""
        res = self.guest_post()

        self.assertEqual(res.status_code, 403, res.data)
        lines = list(
            CartItem.objects.filter(
                cart__session_id=self.guest.session.session_key
            ).values_list("product_id", "quantity")
        )
        self.assertEqual(lines, [(self.product.id, 2)])

    def test_the_cart_coupon_state_is_untouched_by_a_refused_checkout(self):
        """The cart-side guest path feeds checkout (spec route rule 2 keeps
        the cart guest-open); the refusal must not consume, clear or alter
        the coupon the cart holds."""
        coupon = self.make_coupon(code="SAVE10", discount_value="10")
        applied = self.guest.post(
            "/api/cart/coupon/", {"code": coupon.code}, format="json"
        )
        self.assertEqual(applied.status_code, 200, applied.data)

        res = self.guest_post()

        self.assertEqual(res.status_code, 403, res.data)
        cart = Cart.objects.get(session_id=self.guest.session.session_key)
        self.assertEqual(cart.coupon_id, coupon.pk)
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 0)

    def test_an_anonymous_caller_can_still_cart_and_apply_a_coupon(self):
        """The spec boundary, positively: route rule 2 keeps the CART open
        to guests and rule 3's checkout is what refuses, so the refusal
        stopped at the checkout door and did not leak into the cart flow."""
        self.make_coupon(code="SAVE10", discount_value="10")

        add = self.guest.post(
            "/api/cart/", {"product_id": self.product.id, "quantity": 1}, format="json"
        )
        applied = self.guest.post(
            "/api/cart/coupon/", {"code": "SAVE10"}, format="json"
        )

        self.assertEqual(add.status_code, 201, add.data)
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertEqual(applied.data["coupon_code"], "SAVE10")

    def test_an_invalid_bearer_token_gets_the_auth_stack_answer_not_the_refusal(
        self,
    ):
        """The refusal is for ANONYMOUS callers. Broken credentials are the
        auth stack's axis -- answered with its 401 on every endpoint alike
        -- so this pin keeps the two axes tellable apart: a 401 here is a
        bad token, never a guest reason."""
        res = self.guest.post(
            "/api/orders/checkout/",
            self.checkout_payload(),
            format="json",
            HTTP_AUTHORIZATION="Bearer not-a-real-token",
        )

        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(res.data["code"], "not_authenticated")

    def test_an_anonymous_keyed_replay_never_reaches_an_account_order(self):
        """The leak the old keyed-collapse path had to be defended against
        is now refused at the door: an anonymous caller presenting a live
        account Idempotency-Key gets the uniform refusal, not the order,
        not the key's existence, nothing."""
        self.make_user("buyer")
        self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])
        key = "account-key-live-001"
        first = self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(),
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(Order.objects.count(), 1)
        self.auth(None)

        stolen = self.guest_post(idempotency_key=key)

        self.assertEqual(stolen.status_code, 403, stolen.data)
        self.assertEqual(stolen.data["error"], REFUSAL_MESSAGE)
        self.assertNotIn("id", stolen.data)
        self.assertEqual(Order.objects.count(), 1)

    def test_checkout_is_still_throttled_for_anonymous_callers(self):
        """conventions.md:24, unchanged: the throttle runs before the view
        body, so the anonymous funnel still draws on the cart budget and
        the refusal is never a bypass of it."""
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
            throttled = self.guest_post()
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(Order.objects.count(), 0)


@tag("orders")
class AuthenticatedCheckoutRegressionTests(GuestRefusalTestBase):
    """(f) The refusal is scoped to anonymous callers only: the account
    path still works END TO END, and the guest_email body field is still
    ignored outright on it."""

    def test_authenticated_checkout_still_works_end_to_end(self):
        """Checkout -> intent -> verify -> captured, with the stock
        decrement, the coupon spend and the hold conversion the payment
        commit is responsible for. Razorpay is mocked throughout."""
        coupon = self.make_coupon(code="SAVE10", discount_value="10", usage_limit=5)
        self.make_user("buyer")
        self.api_login("buyer")
        self.seed_session_cart([(self.product, 2)])  # 1000.00
        self.razorpay_mock(order_id="order_ACCTOK")

        res = self.client.post(
            "/api/orders/checkout/",
            self.checkout_payload(coupon_code=coupon.code),
            format="json",
        )

        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(id=res.data["id"])
        self.assertEqual(order.user.username, "buyer")
        self.assertEqual(order.guest_email, "")
        self.assertIsNone(order.guest_token)
        self.assertEqual(order.discount_amount, Decimal("100.00"))
        self.assertEqual(order.total_amount, Decimal("900.00"))

        intent = self.client.post(
            "/api/orders/payment/", {"order_id": order.id}, format="json"
        )
        self.assertEqual(intent.status_code, 200, intent.data)
        order.refresh_from_db()

        verify = self.client.post(
            "/api/orders/payment/verify/",
            {
                "order_id": order.id,
                "razorpay_order_id": order.razorpay_order_id,
                "razorpay_payment_id": "pay_ACCTOK",
                "razorpay_signature": "sig",
            },
            format="json",
        )

        self.assertEqual(verify.status_code, 200, verify.data)
        self.assertEqual(verify.data["status"], "confirmed")
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.payment_status, "captured")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 8)  # 10 - 2
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        self.assertEqual(
            order.stock_reservations.get().status,
            StockReservation.Status.CONVERTED,
        )

    def test_the_same_client_can_log_in_and_check_out_the_same_cart(self):
        """The refusal blocks the GUEST, not the purchase: the same browser,
        once authenticated, completes the checkout with the same cart it
        was refused with."""
        refused = self.guest_post()
        self.assertEqual(refused.status_code, 403, refused.data)

        self.make_user("buyer")
        res, token = self.api_login("buyer", client=self.guest)
        self.assertEqual(res.status_code, 200, res.data)
        self.guest.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        ok = self.guest.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )

        self.assertEqual(ok.status_code, 201, ok.data)
        order = Order.objects.get(id=ok.data["id"])
        self.assertEqual(order.user.username, "buyer")
        self.assertEqual(order.guest_email, "")

    def test_the_refusal_does_not_burn_an_order_number(self):
        """A rolled-back checkout never burns a number (R-8.4); a refused
        one never even asks. The first account checkout after any number of
        refusals still opens the year's sequence at 000001."""
        for _ in range(3):
            self.assertEqual(self.guest_post().status_code, 403)

        self.make_user("buyer")
        self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])
        res = self.client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["order_number"], f"ORD-{timezone.now().year}-000001")

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

    def test_an_over_length_shipping_field_is_still_refused_for_an_account(self):
        """The varchar-width gate is an ACCOUNT gate now (the anonymous
        side of it died with the refusal, pinned as uniform in the class
        above). BOTH the gate's status and its message are pinned: the
        status alone is not evidence, since a half of this test that never
        authenticated would stay green with the width loop deleted."""
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        self.auth(token)
        self.seed_session_cart([(self.product, 1)])
        for field, limit in (("full_name", 150), ("phone", 15)):
            with self.subTest(field=field):
                too_long = "x" * (limit + 1)
                res = self.checkout(**{field: too_long})

                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(
                    res.data["error"],
                    f"{field} must be at most {limit} characters",
                )
                self.assertEqual(Order.objects.count(), 0)
        self.auth(None)

    def test_an_account_keyed_replay_still_tolerates_payload_drift(self):
        """SPEC-9.3.14 stands for an authenticated caller: the keyed
        guest-agreement branch died with the refusal, and the account
        contract ("collapse regardless of payload drift") must not have
        moved with it (orders.test_idempotency pins the same contract)."""
        self.make_user("drift-buyer")
        self.api_login("drift-buyer")
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


class GuestOrderRowTestBase(GuestRefusalTestBase):
    """A guest order ROW the way the pre-refusal checkout minted one.

    The API no longer creates these (the refusal above); rows from before it
    are still data every staff and payment path must keep handling, so the
    classes below build the exact pre-refusal shape through the ORM: user
    NULL, guest_email + guest_token set, one OrderItem, an ACTIVE hold with
    a NULL owner, and the creation status event.
    """

    def setUp(self):
        super().setUp()
        self._row_seq = 0

    def make_guest_order(self, quantity=2, email=GUEST_EMAIL, **overrides):
        self._row_seq += 1
        fields = dict(
            user=None,
            guest_email=email,
            guest_token=f"legacy-guest-token-{self._row_seq:04d}",
            order_number=f"ORD-2026-8{self._row_seq:05d}",
            full_name="Guest Buyer",
            phone="9876543210",
            address="12 Rose Lane",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal("1000.00"),
            discount_amount=Decimal("0.00"),
            shipping_amount=Decimal("0.00"),
            status="pending",
        )
        fields.update(overrides)
        order = Order.objects.create(**fields)
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name=self.product.name,
            sku="",
            variant_name=self.product.name,
            price=self.product.price,
            quantity=quantity,
            subtotal=self.product.price * quantity,
        )
        StockReservation.objects.create(
            product=self.product,
            order=order,
            owner=None,
            quantity=quantity,
            expires_at=StockReservation.expiry_from_now(),
        )
        OrderStatusEvent.objects.create(
            order=order,
            from_status=None,
            to_status=order.status,
            actor=None,
            trigger=TRIGGER_ORDER_CREATE,
        )
        return order

    def guest_token_header(self, token):
        return {TOKEN_HEADER: token}


@tag("orders")
class GuestOrderLookupTests(GuestOrderRowTestBase):
    """Possession of the token is the whole authorization (legacy rows)."""

    def test_the_token_alone_returns_the_guests_own_order(self):
        order = self.make_guest_order()

        res = self.guest.get(
            "/api/orders/guest/", **self.guest_token_header(order.guest_token)
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], order.id)
        self.assertEqual(res.data["order_number"], order.order_number)
        self.assertEqual(res.data["status"], "pending")
        self.assertEqual(res.data["items"][0]["product_name"], "Rose Aurum")

    def test_the_order_number_is_an_optional_cross_check_not_the_credential(self):
        order = self.make_guest_order()

        res = self.guest.get(
            f"/api/orders/guest/{order.order_number}/",
            **self.guest_token_header(order.guest_token),
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], order.id)

    def test_the_lookup_is_served_on_the_v1_mirror(self):
        order = self.make_guest_order()

        res = self.guest.get(
            f"/api/v1/store/orders/guest/{order.order_number}/",
            **self.guest_token_header(order.guest_token),
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], order.id)

    def test_no_token_is_refused(self):
        self.make_guest_order()

        res = self.guest.get("/api/orders/guest/")

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Order not found")

    def test_a_wrong_token_is_refused(self):
        self.make_guest_order()

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
        theirs = self.make_guest_order(email="theirs@example.com")
        mine = self.make_guest_order(email="mine@example.com")

        stolen = self.guest.get(
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
        own = self.guest.get(
            f"/api/orders/guest/{mine.order_number}/",
            **self.guest_token_header(mine.guest_token),
        )
        self.assertEqual(own.status_code, 200, own.data)
        self.assertEqual(own.data["id"], mine.id)

    def test_a_token_cannot_be_paired_with_another_orders_number(self):
        order = self.make_guest_order(email="first@example.com")
        other = self.make_guest_order(email="other@example.com")

        res = self.guest.get(
            f"/api/orders/guest/{other.order_number}/",
            **self.guest_token_header(order.guest_token),
        )

        self.assertEqual(res.status_code, 404, res.data)

    def test_an_unknown_order_number_with_a_valid_token_is_refused(self):
        order = self.make_guest_order()

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
        """No response anywhere carries ``guest_token``: the serializer
        never includes it, so a leaked order read cannot be replayed as a
        guest lookup."""
        order = self.make_guest_order()

        res = self.guest.get(
            "/api/orders/guest/", **self.guest_token_header(order.guest_token)
        )

        self.assertNotIn("guest_token", res.data)
        self.assertNotIn("guest_token", OrderSerializer(order).data)


@tag("orders")
class GuestOrderStaffSurfaceTests(GuestOrderRowTestBase):
    """Requirement 5: a guest order is a first-class order for staff.

    Every surface a customer order appears on must carry a legacy guest row
    too, or the fulfilment queue silently drops the store's sales.
    """

    def test_the_admin_json_seam_serves_guest_orders(self):
        order = self.make_guest_order()
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
        order = self.make_guest_order()
        admin = self.make_staff("root")
        self.client.force_login(admin)

        changelist = self.client.get("/admin/orders/order/")
        found = self.client.get("/admin/orders/order/", {"q": GUEST_EMAIL})

        self.assertEqual(changelist.status_code, 200)
        self.assertContains(changelist, order.order_number)
        self.assertEqual(found.status_code, 200)
        self.assertContains(found, order.order_number)

    def test_the_admin_change_form_renders_the_guest_order(self):
        order = self.make_guest_order()
        admin = self.make_staff("root")
        self.client.force_login(admin)

        res = self.client.get(f"/admin/orders/order/{order.id}/change/")

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, GUEST_EMAIL)

    def test_the_csv_export_answers_for_a_guest_order(self):
        """export_csv walks the selected queryset reading ``customer``. A
        guest row has no user, so the label resolves to the guest email
        instead of raising AttributeError mid-export."""
        order = self.make_guest_order()
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
        cannot ship the sales this row shape carries. The money and the
        shipping record stay withheld exactly as B03 narrowed them."""
        order = self.make_guest_order()
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
        order = self.make_guest_order()

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
class GuestOrderNullUserPathTests(GuestOrderRowTestBase):
    """Requirement 6: nothing that reads ``order.user`` may raise on a
    guest row. Each case below is a path a pre-refusal row really reaches."""

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
        """Stand in for create_payment, which stays account-scoped: the
        store hands the gateway reference to a pre-refusal row out of band
        (a live deployment's only source of such a reference is the old
        checkout path; the webhook below is what confirms it)."""
        order.razorpay_order_id = "order_GUEST1"
        order.save(update_fields=["razorpay_order_id"])

    def test_the_payment_webhook_confirms_a_guest_order(self):
        order = self.make_guest_order()
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
        # Inventory rides the same shared commit the customer callback uses
        # (orders.inventory.commit_order_sale), so a guest order confirmed by
        # the DELIVERY has its stock decremented and its ledger row written
        # exactly as one confirmed by the callback would. The quantity is read
        # off the order rather than typed, so the pin tracks the fixture
        # instead of a number that can drift from it.
        line = order.items.get()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10 - line.quantity)
        movement = StockMovement.objects.get()
        self.assertEqual(movement.reason, StockMovement.Reason.SALE)
        self.assertEqual(movement.delta, -line.quantity)
        self.assertEqual(movement.note, f"Order #{order.id}")
        self.assertEqual(
            order.stock_reservations.get().status,
            StockReservation.Status.CONVERTED,
        )
        self.assertIsNone(order.status_events.latest("id").actor_id)

    def test_the_order_confirmation_email_reaches_the_guest(self):
        """The notification reads the order's recipient, so a legacy guest
        row gets the mail instead of an AttributeError swallowed into the
        log. Since TIER1-ASYNC-CONVERT the webhook queues the confirmation;
        the drain worker delivers it to the guest address."""
        order = self.make_guest_order()
        self.pay_the_guest_order(order)

        self.deliver(self.capture_event(order))

        # Nothing sent on the delivery thread -- the intent is queued.
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(order.recipient, GUEST_EMAIL)
        notifications.drain_notifications()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [GUEST_EMAIL])

    def test_the_refund_seam_refunds_a_captured_guest_order(self):
        order = self.make_guest_order()
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
        order = self.make_guest_order()
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
        row is outside every account's scope -- so the answer is the same
        uniform 404 an unknown id gets, not a 500 on the NULL user. This is
        exactly WHY the anonymous checkout path had to die: a guest order is
        unpayable by construction."""
        order = self.make_guest_order()
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
        guest row is not disclosed by any authenticated read."""
        order = self.make_guest_order()
        self.make_user("buyer")
        self.api_login("buyer")

        history = self.client.get("/api/orders/")
        detail = self.client.get(f"/api/orders/{order.id}/")

        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual(history.data["count"], 0)
        self.assertEqual(detail.status_code, 404, detail.data)


@tag("orders")
class GuestOrderModelInvariantTests(GuestOrderRowTestBase):
    """The two-owner rule and the guest-key constraint, as database facts
    rather than habits -- they outlive the refusal, because the rows it
    guards can still exist."""

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

    def test_the_guest_key_constraint_is_the_database_authority(self):
        """conventions.md:17. The (user, key) constraint cannot see a guest
        row -- its user is NULL and NULLs stay distinct -- so the conditional
        (guest_email, key) constraint is what makes a keyed guest row
        duplicate impossible at the database level. The constraint outlives
        the refusal: rows minted before it still carry keys."""
        order = self.make_guest_order()
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

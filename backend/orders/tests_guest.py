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

# Hand-written literal, deliberately not imported: a pin whose expected value is read
# off the constant under test agrees with a wrong constant from both sides.
REFUSAL_MESSAGE = (
    "Guest checkout is not available. Sign in or create an account to place an order."
)

WEBHOOK_SECRET = "TESTINGONLY-WEBHOOK-SECRET-DO-NOT-USE"
WEBHOOK_URL = "/api/v1/webhooks/razorpay/"


class GuestRefusalTestBase(ApiTestCase):
    def setUp(self):
        self.product = self.make_product(
            name="Rose Aurum", price="500.00", stock=10, category="Floral"
        )
        self.guest = self.fresh_client()
        self.seed_session_cart([(self.product, 2)], client=self.guest)

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
        user = User.objects.create_user(
            username, f"{username}@example.com", "S3cure-Passphrase!", is_staff=True
        )
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user


@tag("orders")
class GuestCheckoutRefusalTests(GuestRefusalTestBase):
    def test_anonymous_checkout_is_refused_with_one_clear_error(self):
        res = self.guest_post()

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["error"], REFUSAL_MESSAGE)
        self.assertEqual(res.data["code"], "permission_denied")
        self.assertEqual(res.data["details"], {})

    def test_the_refusal_creates_no_order_row_reservation_or_trail(self):
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
        stock_before = type(self.product).objects.get(pk=self.product.pk).stock

        res = self.guest_post()

        self.assertEqual(res.status_code, 403, res.data)
        stock_after = type(self.product).objects.get(pk=self.product.pk).stock
        self.assertEqual(stock_after, stock_before)
        self.assertEqual(stock_after, 10)

    def test_coupon_usage_is_unchanged_by_a_refused_submission(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10", usage_limit=5)
        self.assertEqual(coupon.used_count, 0)

        res = self.guest_post(coupon_code=coupon.code)

        self.assertEqual(res.status_code, 403, res.data)
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 0)
        self.assertFalse(Order.objects.filter(coupon=coupon).exists())

    def test_every_guest_reason_gets_the_byte_identical_refusal(self):
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

        self.assertEqual(len(set(bodies.values())), 1, bodies)
        self.assertEqual(Order.objects.count(), 0)
        self.assertFalse(StockReservation.objects.exists())

    def test_an_oversell_attempt_gets_the_same_refusal_not_a_stock_error(self):
        type(self.product).objects.filter(pk=self.product.pk).update(stock=1)
        baseline = self.guest_post()
        oversell = self.guest_post()

        self.assertEqual(oversell.status_code, 403, oversell.data)
        self.assertEqual(oversell.content, baseline.content)
        self.assertEqual(Order.objects.count(), 0)

    def test_a_sessionless_caller_gets_the_same_refusal(self):
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
        main = self.guest_post()
        mirrored = self.guest_post(path="/api/v1/store/orders/checkout/")

        self.assertEqual(mirrored.status_code, 403, mirrored.data)
        self.assertEqual(mirrored.content, main.content)
        self.assertEqual(Order.objects.count(), 0)

    def test_an_invalid_guest_email_is_refused_and_writes_nothing(self):
        for bad in ("not-an-email", "guest@", "@example.com", "a b@example.com"):
            with self.subTest(guest_email=bad):
                res = self.guest_post(guest_email=bad)

                self.assertEqual(res.status_code, 403, res.data)
                self.assertEqual(res.data["error"], REFUSAL_MESSAGE)
                self.assertEqual(Order.objects.count(), 0)

    def test_an_empty_payload_gets_the_same_refusal(self):
        res = self.guest.post("/api/orders/checkout/", {}, format="json")

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["error"], REFUSAL_MESSAGE)

    def test_the_refusal_response_carries_no_order_fields(self):
        res = self.guest_post()

        self.assertEqual(res.status_code, 403, res.data)
        for field in ("id", "order_number", "total_amount", "guest_token", "items"):
            self.assertNotIn(field, res.data)

    def test_the_refusal_leaves_the_callers_cart_intact(self):
        res = self.guest_post()

        self.assertEqual(res.status_code, 403, res.data)
        lines = list(
            CartItem.objects.filter(
                cart__session_id=self.guest.session.session_key
            ).values_list("product_id", "quantity")
        )
        self.assertEqual(lines, [(self.product.id, 2)])

    def test_the_cart_coupon_state_is_untouched_by_a_refused_checkout(self):
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
        res = self.guest.post(
            "/api/orders/checkout/",
            self.checkout_payload(),
            format="json",
            HTTP_AUTHORIZATION="Bearer not-a-real-token",
        )

        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(res.data["code"], "not_authenticated")

    def test_an_anonymous_keyed_replay_never_reaches_an_account_order(self):
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
        self.assertEqual(create_order.view_class.throttle_scope, "cart")
        self.assertIn("cart", api_settings.DEFAULT_THROTTLE_RATES)

        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["cart"] = "2/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
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
    def test_authenticated_checkout_still_works_end_to_end(self):
        coupon = self.make_coupon(code="SAVE10", discount_value="10", usage_limit=5)
        self.make_user("buyer")
        self.api_login("buyer")
        self.seed_session_cart([(self.product, 2)])
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
        self.assertEqual(self.product.stock, 8)
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        self.assertEqual(
            order.stock_reservations.get().status,
            StockReservation.Status.CONVERTED,
        )

    def test_the_same_client_can_log_in_and_check_out(self):
        refused = self.guest_post()
        self.assertEqual(refused.status_code, 403, refused.data)

        self.make_user("buyer")
        res, token = self.api_login("buyer", client=self.guest)
        self.assertEqual(res.status_code, 200, res.data)
        self.guest.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.seed_session_cart([(self.product, 2)], client=self.guest)

        ok = self.guest.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )

        self.assertEqual(ok.status_code, 201, ok.data)
        order = Order.objects.get(id=ok.data["id"])
        self.assertEqual(order.user.username, "buyer")
        self.assertEqual(order.guest_email, "")

    def test_the_refusal_does_not_burn_an_order_number(self):
        for _ in range(3):
            self.assertEqual(self.guest_post().status_code, 403)

        self.make_user("buyer")
        self.api_login("buyer")
        self.seed_session_cart([(self.product, 1)])
        res = self.client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )

        self.assertEqual(res.status_code, 201, res.data)
        self.assertRegex(
            res.data["order_number"],
            rf"ORD-{timezone.now().year}-[0-9A-F]{{8}}",
        )

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
        # Possession of a guest token is the only guest authorization, so an account order
        # must not carry one.
        self.assertNotIn("guest_token", res.data)

    def test_an_over_length_shipping_field_is_still_refused_for_an_account(self):
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
    """A legacy guest order row: user NULL, guest_email + guest_token set, one OrderItem and an ACTIVE hold with a NULL owner -- the pre-refusal shape staff and payment paths still handle."""

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
        theirs = self.make_guest_order(email="theirs@example.com")
        mine = self.make_guest_order(email="mine@example.com")

        stolen = self.guest.get(
            f"/api/orders/guest/{theirs.order_number}/",
            **self.guest_token_header(mine.guest_token),
        )
        anonymous = self.guest.get(f"/api/orders/guest/{theirs.order_number}/")

        self.assertEqual(stolen.status_code, 404, stolen.data)
        self.assertEqual(anonymous.status_code, 404, anonymous.data)
        # Byte-identical answers: "somebody else's token" and "no token" are indistinguishable,
        # so the probe cannot learn whether the order exists.
        self.assertEqual(stolen.data, anonymous.data)
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
        order = self.make_guest_order()

        res = self.guest.get(
            "/api/orders/guest/", **self.guest_token_header(order.guest_token)
        )

        self.assertNotIn("guest_token", res.data)
        self.assertNotIn("guest_token", OrderSerializer(order).data)


@tag("orders")
class GuestOrderStaffSurfaceTests(GuestOrderRowTestBase):
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
        # Inventory rides the shared commit_order_sale the callback uses, so a delivery-confirmed
        # guest order decrements stock and writes its ledger row exactly as a callback would.
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
        order = self.make_guest_order()
        self.pay_the_guest_order(order)

        self.deliver(self.capture_event(order))

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
        self.assertEqual(
            res.data["refund"]["actor_id"],
            User.objects.get(username="finmgr").id,
        )
        self.assertEqual(Refund.objects.get(order=order).kind, Refund.Kind.FULL)

    def test_cancelling_a_guest_order_releases_its_hold(self):
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
        """create_payment/verify_payment are account-scoped, so a guest row is unpayable by construction and answers the same uniform 404 an unknown id gets."""
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
        """(guest_email, key) is the DB authority because the (user, key) constraint cannot see NULL-user rows; the constraint outlives the refusal."""
        order = self.make_guest_order()
        order.idempotency_key = "guest-retry-001"
        order.save(update_fields=["idempotency_key"])

        # Savepoint: the refused insert rolls back to here, leaving the test transaction usable
        # for the count below.
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
        """Widths are declared for BOTH engines: SQLite ignores max_length while Postgres enforces it, so a value that only fits SQLite is a production error."""
        self.assertEqual(Order._meta.get_field("guest_email").max_length, 254)
        self.assertEqual(Order._meta.get_field("guest_token").max_length, 64)
        self.assertTrue(Order._meta.get_field("guest_token").null)
        self.assertTrue(Order._meta.get_field("user").null)
        self.assertEqual(StockReservation._meta.get_field("owner").null, True)
        self.assertEqual(Order._meta.get_field("guest_token").unique, True)

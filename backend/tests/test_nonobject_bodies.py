"""Non-object JSON bodies on the ORDERS + PRODUCTS public write endpoints.

The defect class is F7-B1 (T1b red-team): a JSON array / scalar / null body
parses fine in DRF but has no ``.get`` to call, so reading keys off it raised
AttributeError — a 500 on a public write endpoint. T1F1 fixed the cart half
via ``cart.views._object_body``; this module pins the sibling sites the
T1F1 auditor confirmed in the orders + products half: the staff refund seam,
payment create/verify, order checkout (the required-fields and coupon reads),
the coupon-apply endpoint and the staff inventory adjust.

Method copied from cart/tests.py::NonObjectJsonBodyTests — every non-object
JSON top level is posted as a RAW STRING, because the DRF test client's
``format="json"`` path treats falsy payloads (``[]``, ``{}``, ``0``) as "no
data" and would post an empty body instead of the payload under test. The
acceptance bar is byte-identity: each endpoint must refuse a non-object body
with exactly the response it already gives an empty object (its own
missing-key/validation 400), and no state may move.
"""

from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import tag
from django.utils import timezone

from cart.models import Cart
from common.roles import ROLE_FINANCE, ROLE_INVENTORY
from common.testing import ApiTestCase
from orders.models import Order, Refund
from products.models import StockMovement

# Every non-object JSON top level a parser can produce.
RAW_BODIES = ("[]", "[1,2]", "42", '"hello"', "null", "true")


class NonObjectBodyRefusalMixin:
    """Post raw JSON strings and require the ``{}`` answer, byte for byte."""

    def _post_raw(self, path, body):
        return self.client.post(path, body, content_type="application/json")

    def _refuse_same_as_empty_object(self, path):
        """The ``{}`` response is the oracle: each non-object body must get
        that endpoint's own invalid-object 400 with identical wire bytes,
        never a 500 and never a differentiated body."""
        expected = self._post_raw(path, "{}")
        self.assertEqual(expected.status_code, 400, expected.data)
        for body in RAW_BODIES:
            with self.subTest(body=body):
                res = self._post_raw(path, body)
                self.assertEqual(res.status_code, 400, res.data)
                # Byte-identical wire body, not just an equal status class.
                self.assertEqual(res.content, expected.content)
                self.assertEqual(res.data, expected.data)
        return expected

    @staticmethod
    def _role_user(username, role, staff=False):
        user = User.objects.create_user(
            username, f"{username}@example.com", "S3cure-Passphrase!"
        )
        if staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user


@tag("orders")
class RefundNonObjectBodyTests(NonObjectBodyRefusalMixin, ApiTestCase):
    """POST /api/admin/orders/<id>/refund/ — the reason/amount reads."""

    PATH = "/api/admin/orders/{order_id}/refund/"

    def setUp(self):
        self.buyer = self.make_user("buyer")
        self.finance = self._role_user("finmgr", ROLE_FINANCE, staff=True)
        self.order = Order.objects.create(
            user=self.buyer,
            full_name="Buyer Person",
            phone="9876543210",
            address="12 Rose Lane",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            status="confirmed",
            payment_status="captured",
            total_amount=Decimal("900.00"),
            paid_at=timezone.now(),
            razorpay_order_id="order_PAYCHECK",
            razorpay_payment_id="pay_CAPTURED1",
        )
        self.client.force_authenticate(self.finance)

    def test_refund_refuses_a_non_object_body_like_an_empty_object(self):
        path = self.PATH.format(order_id=self.order.id)
        self._refuse_same_as_empty_object(path)
        # Money semantics untouched: no refund row, no payment-dimension move.
        self.assertEqual(Refund.objects.count(), 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "captured")
        self.assertIsNone(self.order.refunded_at)


@tag("orders")
class PaymentNonObjectBodyTests(NonObjectBodyRefusalMixin, ApiTestCase):
    """POST /api/orders/payment/ and /api/orders/payment/verify/ — the
    order_id / gateway-reference reads."""

    def setUp(self):
        self.buyer = self.make_user("buyer")
        self.order = Order.objects.create(
            user=self.buyer,
            full_name="Buyer Person",
            phone="9876543210",
            address="12 Rose Lane",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            status="pending",
            payment_status="pending",
            total_amount=Decimal("499.99"),
        )
        self.client.force_authenticate(self.buyer)

    def test_create_payment_refuses_a_non_object_body_like_an_empty_object(self):
        expected = self._refuse_same_as_empty_object("/api/orders/payment/")
        self.assertEqual(expected.data["error"], "order_id is required")
        self.order.refresh_from_db()
        self.assertIsNone(self.order.razorpay_order_id)  # no intent minted

    def test_verify_payment_refuses_a_non_object_body_like_an_empty_object(self):
        expected = self._refuse_same_as_empty_object("/api/orders/payment/verify/")
        self.assertEqual(expected.data["error"], "Payment details are required")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "pending")  # nothing captured
        self.assertIsNone(self.order.razorpay_payment_id)


@tag("orders")
class CheckoutNonObjectBodyTests(NonObjectBodyRefusalMixin, ApiTestCase):
    """POST /api/orders/checkout/ (required-fields + coupon reads) and
    POST /api/orders/apply-coupon/ (the code read)."""

    def setUp(self):
        self.buyer = self.make_user("buyer")
        self.client.force_authenticate(self.buyer)
        self.product = self.make_product(stock=5)
        self.seed_session_cart([(self.product, 1)])
        self.cart = Cart.objects.get()

    def test_checkout_refuses_a_non_object_body_like_an_empty_object(self):
        expected = self._refuse_same_as_empty_object("/api/orders/checkout/")
        # The endpoint's own missing-first-required-field refusal, reached
        # because the caller HAS a session cart (the guard under test is
        # past the cart gates, not the cart gates themselves).
        self.assertEqual(expected.data["error"], "full_name is required")
        self.assertEqual(Order.objects.count(), 0)  # no order minted
        self.assertEqual(self.cart.items.count(), 1)  # cart untouched

    def test_apply_coupon_refuses_a_non_object_body_like_an_empty_object(self):
        expected = self._refuse_same_as_empty_object("/api/orders/apply-coupon/")
        self.assertEqual(expected.data["error"], "Coupon code is required")
        self.cart.refresh_from_db()
        self.assertIsNone(self.cart.coupon_id)  # nothing persisted


@tag("products")
class InventoryAdjustNonObjectBodyTests(NonObjectBodyRefusalMixin, ApiTestCase):
    """POST inventory adjustment — the staff adjust-stock payload reads
    (product_id/delta/reason/note), on both the canonical admin mount and
    the deprecated store alias (SPEC-9-T3)."""

    URL = "/api/products/inventory/adjustments/"
    ADMIN_URL = "/api/v1/admin/inventory/adjustments/"

    def setUp(self):
        self.invmgr = self._role_user("invmgr", ROLE_INVENTORY)
        self.product = self.make_product(stock=10)
        self.client.force_authenticate(self.invmgr)

    def _assert_refused_without_moving_state(self, url):
        expected = self._refuse_same_as_empty_object(url)
        self.assertEqual(expected.data["error"], "product_id is required")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)  # stock untouched
        self.assertEqual(StockMovement.objects.count(), 0)  # no ledger row

    def test_inventory_adjust_refuses_a_non_object_body_like_an_empty_object(self):
        self._assert_refused_without_moving_state(self.URL)

    def test_admin_mount_refuses_a_non_object_body_like_an_empty_object(self):
        self._assert_refused_without_moving_state(self.ADMIN_URL)

"""SPEC-E-01: the customer's refund-request seam.

One sentence of requirement: an authenticated customer can file a refund
request against one of their own captured orders, and filing one moves no
money.

The probes below are organized around the rules the task names, plus the traps
this repo has already been bitten by:

* the order lookup is ownership-scoped, so a stranger's order and a
  nonexistent order answer ONE byte-identical 404 (``OwnershipTests``);
* every payment status outside the captured-money pair is a 409, and
  ``refunded`` is among them because there is nothing left to return
  (``StatusGateTests``);
* one pending request per order is enforced by a partial unique index, proved
  by reaching the database directly rather than trusting the view's probe
  (``DuplicateTests``);
* every supplied item id must belong to the addressed order - a mixed list is
  refused whole, never silently narrowed (``ItemsValidationTests``);
* the 201 body is the explicit-field projection, and the response exposes no
  staff decision id (``HappyPathTests``);
* filing a request writes no ``Refund`` row and changes no ``payment_status``
  (``MoneyIsUntouchedTests``);
* ``requester`` is SET_NULL, so deleting an account never erases the trail
  (``TrailTests``).
"""

from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import tag
from rest_framework.test import APIClient

from common.testing import ApiTestCase
from orders.models import Order, OrderItem, Refund, RefundRequest
from orders.serializers import RefundRequestSerializer
from orders.state import CAPTURED_MONEY_PAYMENT_STATUSES, PAYMENT_ALLOWED_TRANSITIONS

CREATE_TEMPLATE = "/api/v1/store/orders/{order_id}/refund-request/"
LEGACY_TEMPLATE = "/api/orders/{order_id}/refund-request/"
TEST_PASSWORD = "S3cure-Passphrase!"


class RefundRequestTestCase(ApiTestCase):
    """Fixtures: two customers, one captured order each, and a helper to file."""

    def setUp(self):
        super().setUp()
        self.buyer = self.make_user("refund-buyer")
        self.stranger = self.make_user("refund-stranger")
        self.product = self.make_product()
        self.order = self._order(self.buyer, "REF-2026-000001")
        self.line = self._line(self.order)
        self.other_order = self._order(self.stranger, "REF-2026-000002")
        self.other_line = self._line(self.other_order)
        self._number = 100

    def _order(self, user, order_number, payment_status="captured"):
        return Order.objects.create(
            user=user,
            order_number=order_number,
            full_name="Refund Buyer",
            phone="9876500077",
            address="3 Refund Lane",
            city="Indore",
            state="MP",
            pincode="452001",
            status="confirmed",
            total_amount=Decimal("500.00"),
            payment_status=payment_status,
        )

    def _line(self, order):
        return OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name="Rose Aurum",
            price=Decimal("250.00"),
            quantity=2,
            subtotal=Decimal("500.00"),
        )

    def new_order(self, payment_status="captured", user=None):
        self._number += 1
        order = self._order(
            user or self.buyer,
            f"REF-2026-{self._number:06d}",
            payment_status=payment_status,
        )
        self._line(order)
        return order

    def login_as(self, user):
        """A REAL JWT minted by the login endpoint (never force_authenticate)."""
        res, token = self.api_login(user.username)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(token, res.data)

    def anonymous_client(self):
        return APIClient()

    def file(self, order=None, client=None, url=None, **overrides):
        order = order or self.order
        payload = {"reason": "The bottle arrived damaged."}
        payload.update(overrides)
        target = client or self.client
        return target.post(
            url or CREATE_TEMPLATE.format(order_id=order.id),
            payload,
            format="json",
        )


@tag("e2e")
class OwnershipTests(RefundRequestTestCase):
    def test_anonymous_caller_is_refused(self):
        response = self.file(client=self.anonymous_client())
        self.assertEqual(response.status_code, 401)
        self.assertFalse(RefundRequest.objects.exists())

    def test_foreign_order_is_a_uniform_404(self):
        self.login_as(self.buyer)

        foreign = self.file(order=self.other_order)
        missing = self.client.post(
            CREATE_TEMPLATE.format(order_id=999999),
            {"reason": "The bottle arrived damaged."},
            format="json",
        )

        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(foreign.data, missing.data)
        self.assertFalse(RefundRequest.objects.exists())

    def test_guest_orders_are_unaddressable(self):
        guest = Order.objects.create(
            user=None,
            guest_email="guest@example.com",
            guest_token="refund-guest-token",
            order_number="REF-2026-000900",
            full_name="Guest Buyer",
            phone="9876500066",
            address="4 Guest Lane",
            city="Indore",
            state="MP",
            pincode="452001",
            status="confirmed",
            total_amount=Decimal("500.00"),
            payment_status="captured",
        )
        self.login_as(self.buyer)

        response = self.file(order=guest)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(RefundRequest.objects.exists())


@tag("e2e")
class StatusGateTests(RefundRequestTestCase):
    def test_only_captured_money_can_be_requested(self):
        self.login_as(self.buyer)

        for payment_status in ("pending", "authorized", "failed", "refunded"):
            with self.subTest(payment_status=payment_status):
                order = self.new_order(payment_status=payment_status)
                response = self.file(order=order)
                self.assertEqual(response.status_code, 409, response.data)

        self.assertFalse(RefundRequest.objects.exists())

    def test_captured_and_partially_refunded_are_accepted(self):
        self.login_as(self.buyer)

        for payment_status in ("captured", "partially_refunded"):
            with self.subTest(payment_status=payment_status):
                order = self.new_order(payment_status=payment_status)
                response = self.file(order=order)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["status"], "pending")

    def test_every_payment_value_the_machine_admits_is_gated(self):
        # A hand-written oracle on BOTH sides of the gate: the expected set is
        # a literal, not a recomputation from the constant the view reads, so
        # a wrong machine fires this by name. Because the loop is driven from
        # the transition table itself, a newly admitted payment value can
        # never slip through untested (the B07a lesson: coverage measures
        # lines executed, not states reasoned about).
        self.assertEqual(
            set(CAPTURED_MONEY_PAYMENT_STATUSES),
            {"captured", "partially_refunded"},
        )
        self.login_as(self.buyer)

        for payment_status in sorted(PAYMENT_ALLOWED_TRANSITIONS):
            with self.subTest(payment_status=payment_status):
                order = self.new_order(payment_status=payment_status)
                response = self.file(order=order)
                expected = (
                    201 if payment_status in {"captured", "partially_refunded"} else 409
                )
                self.assertEqual(response.status_code, expected, response.data)


@tag("e2e")
class DuplicateTests(RefundRequestTestCase):
    def test_a_second_pending_request_is_a_conflict(self):
        self.login_as(self.buyer)
        RefundRequest.objects.create(
            order=self.order,
            requester=self.buyer,
            reason="First ask",
        )

        response = self.file()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(RefundRequest.objects.filter(order=self.order).count(), 1)

    def test_a_decided_request_releases_the_order(self):
        self.login_as(self.buyer)
        RefundRequest.objects.create(
            order=self.order,
            requester=self.buyer,
            reason="First ask",
            status=RefundRequest.Status.REJECTED,
        )

        response = self.file()

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(RefundRequest.objects.filter(order=self.order).count(), 2)

    def test_the_database_is_the_last_resort_authority(self):
        RefundRequest.objects.create(
            order=self.order,
            requester=self.buyer,
            reason="First ask",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RefundRequest.objects.create(
                    order=self.order,
                    requester=self.buyer,
                    reason="Racing ask",
                )

    def test_a_racing_insert_that_slips_past_the_probe_is_still_a_409(self):
        # The probe and the insert are separated by no lock the test can
        # reach, so the race is simulated at the savepoint boundary: the
        # unique index raising during save must be answered with the same 409
        # as the probe, never a 500 or a poisoned transaction.
        self.login_as(self.buyer)

        with patch.object(
            RefundRequestSerializer,
            "save",
            side_effect=IntegrityError("orders_refundrequest_pending_uidx"),
        ):
            response = self.file()

        self.assertEqual(response.status_code, 409)
        self.assertFalse(RefundRequest.objects.exists())


@tag("e2e")
class ValidationTests(RefundRequestTestCase):
    def test_reason_is_required(self):
        self.login_as(self.buyer)

        missing = self.client.post(
            CREATE_TEMPLATE.format(order_id=self.order.id),
            {},
            format="json",
        )
        blank = self.file(reason="   ")

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(blank.status_code, 400)
        self.assertFalse(RefundRequest.objects.exists())

    def test_a_non_object_body_is_a_400_not_a_500(self):
        self.login_as(self.buyer)

        response = self.client.post(
            CREATE_TEMPLATE.format(order_id=self.order.id),
            ["not", "an", "object"],
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(RefundRequest.objects.exists())

    def test_an_item_from_another_order_is_refused(self):
        self.login_as(self.buyer)

        response = self.file(items=[self.other_line.id])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(RefundRequest.objects.exists())

    def test_a_mixed_list_is_refused_whole(self):
        self.login_as(self.buyer)

        response = self.file(items=[self.line.id, self.other_line.id])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(RefundRequest.objects.exists())

    def test_an_unknown_item_id_is_refused(self):
        self.login_as(self.buyer)

        response = self.file(items=[999999])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(RefundRequest.objects.exists())

    def test_items_of_the_wrong_shape_are_refused(self):
        self.login_as(self.buyer)

        response = self.file(items="1,2")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(RefundRequest.objects.exists())


@tag("e2e")
class HappyPathTests(RefundRequestTestCase):
    def test_a_customer_files_a_whole_order_request(self):
        self.login_as(self.buyer)

        response = self.file()

        self.assertEqual(response.status_code, 201, response.data)
        stored = RefundRequest.objects.get()
        self.assertEqual(stored.order, self.order)
        self.assertEqual(stored.requester, self.buyer)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.items, [])
        self.assertIsNone(stored.decided_by)
        self.assertIsNone(stored.decided_at)
        self.assertEqual(stored.reason, "The bottle arrived damaged.")

    def test_item_ids_are_stored_and_duplicates_collapse(self):
        self.login_as(self.buyer)

        response = self.file(items=[self.line.id, self.line.id])

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(RefundRequest.objects.get().items, [self.line.id])
        self.assertEqual(response.data["items"], [self.line.id])

    def test_the_response_is_the_explicit_field_projection(self):
        self.login_as(self.buyer)

        response = self.file()

        self.assertEqual(
            set(response.data),
            {
                "id",
                "order",
                "order_number",
                "requester",
                "reason",
                "items",
                "status",
                "decided_at",
                "created_at",
            },
        )
        self.assertEqual(response.data["order"], self.order.id)
        self.assertEqual(response.data["order_number"], self.order.order_number)
        self.assertEqual(response.data["requester"], self.buyer.id)
        self.assertEqual(response.data["status"], "pending")
        self.assertIsNone(response.data["decided_at"])
        # Staff identity is not part of the customer projection.
        self.assertNotIn("decided_by", response.data)

    def test_the_serializer_declares_its_fields(self):
        self.assertNotIn("__all__", RefundRequestSerializer.Meta.fields)
        self.assertEqual(
            list(RefundRequestSerializer.Meta.fields),
            [
                "id",
                "order",
                "order_number",
                "requester",
                "reason",
                "items",
                "status",
                "decided_at",
                "created_at",
            ],
        )

    def test_the_serializer_is_usable_without_an_order_context(self):
        # Output rendering (the 201 body) constructs the serializer with an
        # instance and no context, so the item-ownership gate must be a
        # write-path-only concern rather than an attribute that always exists.
        serializer = RefundRequestSerializer(data={"reason": "x", "items": [1]})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["items"], [1])

    def test_the_legacy_order_family_alias_answers_the_same(self):
        self.login_as(self.buyer)

        response = self.file(
            url=LEGACY_TEMPLATE.format(order_id=self.order.id),
        )

        self.assertEqual(response.status_code, 201, response.data)


@tag("e2e")
class MoneyIsUntouchedTests(RefundRequestTestCase):
    def test_filing_a_request_moves_no_money(self):
        self.login_as(self.buyer)
        before = Order.objects.get(pk=self.order.pk)

        response = self.file()

        self.assertEqual(response.status_code, 201, response.data)
        after = Order.objects.get(pk=self.order.pk)
        self.assertEqual(after.payment_status, before.payment_status)
        self.assertEqual(after.total_amount, before.total_amount)
        self.assertFalse(Refund.objects.exists())


class TrailTests(RefundRequestTestCase):
    def test_deleting_the_requester_keeps_the_request(self):
        # The order belongs to the buyer, so the stranger can be deleted
        # without the order (and its request) cascading away first.
        request = RefundRequest.objects.create(
            order=self.order,
            requester=self.stranger,
            reason="Filed by a since-deleted account",
        )

        self.stranger.delete()

        request.refresh_from_db()
        self.assertIsNone(request.requester)
        self.assertEqual(RefundRequest.objects.count(), 1)

    def test_status_vocabulary_is_exactly_the_three_decisions(self):
        self.assertEqual(
            list(RefundRequest.Status.values),
            ["pending", "approved", "rejected"],
        )

    def test_requester_fk_is_set_null(self):
        field = RefundRequest._meta.get_field("requester")
        self.assertEqual(field.remote_field.on_delete.__name__, "SET_NULL")
        self.assertTrue(field.null)

    def test_decision_fields_start_empty(self):
        request = RefundRequest.objects.create(
            order=self.order,
            requester=self.buyer,
            reason="No decision yet",
        )
        self.assertIsNone(request.decided_by)
        self.assertIsNone(request.decided_at)

    def test_the_label_is_null_safe_and_names_the_status(self):
        request = RefundRequest.objects.create(
            order=self.order,
            requester=self.buyer,
            reason="Label probe",
        )

        self.assertEqual(str(request), f"Refund request #{request.pk} (pending)")

from decimal import Decimal
from unittest.mock import patch

import razorpay
from django.contrib import admin
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.test import RequestFactory, tag
from rest_framework.test import APIClient

from common.admin import RoleAwareModelAdmin
from common.roles import ROLE_FINANCE, ROLE_SUPPORT
from common.testing import ApiTestCase
from orders.admin import RefundRequestAdmin
from orders.models import Order, OrderItem, Refund, RefundRequest
from orders.serializers import RefundRequestSerializer
from orders.state import CAPTURED_MONEY_PAYMENT_STATUSES, PAYMENT_ALLOWED_TRANSITIONS
from orders.views.refund import refund_request_amount

CREATE_TEMPLATE = "/api/v1/store/orders/{order_id}/refund-request/"
LEGACY_TEMPLATE = "/api/orders/{order_id}/refund-request/"
TEST_PASSWORD = "S3cure-Passphrase!"


class RefundRequestTestCase(ApiTestCase):
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
        # The expected set is a hand-written literal, not a recomputation from the constant the
        # view reads, so a wrong machine fires this by name.
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
        # The race is simulated at the savepoint boundary: the unique index raising during save
        # must answer the same 409 as the probe, never a 500 or a poisoned transaction.
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
        # Output rendering constructs the serializer without context, so the item-ownership gate
        # must be write-path-only rather than an attribute that always exists.
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


ADMIN_REQUESTS = "/admin/orders/refundrequest/"


@tag("e2e")
class AdminDecisionTests(RefundRequestTestCase):
    """Approval is the only place a request becomes money: it calls orders.refunds.issue_refund, never writes a Refund row and never calls the gateway."""

    def setUp(self):
        super().setUp()
        self.finance = self._role_user("refund-finance", ROLE_FINANCE)
        self.support = self._role_user("refund-support", ROLE_SUPPORT)
        self.request_record = RefundRequest.objects.create(
            order=self.order,
            requester=self.buyer,
            reason="The bottle arrived damaged.",
        )

    def _role_user(self, username, role):
        user = self.make_user(username)
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    def gateway(self):
        client = self.razorpay_mock()
        client.refund.create.return_value = {"id": "rfnd_E02"}
        return client

    def captured(self, order):
        order.razorpay_payment_id = "pay_E02"
        order.save(update_fields=["razorpay_payment_id"])
        return order

    def order_with_two_lines(self):
        self._number += 1
        order = self._order(self.buyer, f"REF-2026-{self._number:06d}")
        order.total_amount = Decimal("600.50")
        order.save(update_fields=["total_amount"])
        first = self._line(order)
        second = OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name="Oud Noir",
            price=Decimal("100.50"),
            quantity=1,
            subtotal=Decimal("100.50"),
        )
        return self.captured(order), first, second

    def partial_refund(self, order, amount="100.00"):
        Refund.objects.create(
            order=order,
            amount=Decimal(amount),
            reason="Earlier partial refund",
            kind=Refund.Kind.PARTIAL,
            status=Refund.Status.PROCESSED,
            actor=self.finance,
            gateway_refund_id="rfnd_PRIOR",
        )
        order.payment_status = "partially_refunded"
        order.save(update_fields=["payment_status"])

    def post_action(self, action, records, *, note=None, confirm=True, user=None):
        self.client.force_login(user or self.finance)
        data = {
            "action": action,
            "_selected_action": [str(record.pk) for record in records],
            "select_across": "0",
        }
        if confirm:
            data["confirm"] = "yes"
        if note is not None:
            data["confirmation_note"] = note
        return self.client.post(ADMIN_REQUESTS, data, follow=True)

    def test_the_surface_is_refunds_create_gated_and_read_only(self):
        model_admin = admin.site._registry[RefundRequest]
        self.assertIsInstance(model_admin, RefundRequestAdmin)
        self.assertIsInstance(model_admin, RoleAwareModelAdmin)
        self.assertEqual(
            model_admin.capability_map,
            {
                "view": "refunds.create",
                "add": None,
                "change": "refunds.create",
                "delete": None,
            },
        )
        self.assertEqual(
            model_admin.action_capabilities,
            {
                "approve_requests": "refunds.create",
                "reject_requests": "refunds.create",
            },
        )
        self.assertEqual(
            set(model_admin.actions),
            {"approve_requests", "reject_requests"},
        )
        self.assertIn("status", model_admin.list_filter)
        for field in ("order", "requester", "status", "item_count", "created_at"):
            self.assertIn(field, model_admin.list_display)

        for user in (self.finance, self.support):
            request = RequestFactory().get(ADMIN_REQUESTS)
            request.user = user
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request))

    def test_finance_sees_the_queue_and_both_decisions(self):
        self.client.force_login(self.finance)

        response = self.client.get(ADMIN_REQUESTS)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Approve selected refund requests")
        self.assertContains(response, "Reject selected refund requests")

    def test_support_cannot_reach_the_queue_or_run_a_decision(self):
        client = self.gateway()

        self.client.force_login(self.support)
        page = self.client.get(ADMIN_REQUESTS)
        posted = self.post_action(
            "approve_requests",
            [self.request_record],
            note="Support must not move money",
            user=self.support,
        )

        self.assertEqual(page.status_code, 403)
        self.assertEqual(posted.status_code, 403)
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "pending")
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_the_confirmation_page_asks_for_a_required_note(self):
        response = self.post_action(
            "approve_requests", [self.request_record], confirm=False
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="confirmation_note"')
        self.assertContains(response, "required")
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "pending")

    def test_the_required_note_is_offered_only_for_the_two_decisions(self):
        model_admin = admin.site._registry[RefundRequest]

        self.assertIsNone(model_admin.confirmation_reason_form("not_a_decision"))
        form = model_admin.confirmation_reason_form("approve_requests", {})
        self.assertFalse(form.is_valid())

    def test_a_decided_request_cannot_be_rejected_twice(self):
        self.gateway()
        self.post_action("reject_requests", [self.request_record], note="Not eligible")
        entries = LogEntry.objects.count()

        response = self.post_action(
            "reject_requests", [self.request_record], note="Still not eligible"
        )

        self.assertContains(response, "already decided")
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "rejected")
        self.assertEqual(LogEntry.objects.count(), entries)
        self.assertEqual(Refund.objects.count(), 0)

    def test_a_decision_without_a_note_is_refused(self):
        client = self.gateway()

        for action in ("approve_requests", "reject_requests"):
            with self.subTest(action=action):
                response = self.post_action(action, [self.request_record], note="")
                self.assertContains(response, "A decision reason/note is required")
                self.request_record.refresh_from_db()
                self.assertEqual(self.request_record.status, "pending")

        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_a_hand_posted_status_change_is_ignored(self):
        self.client.force_login(self.finance)

        self.client.post(
            f"{ADMIN_REQUESTS}{self.request_record.pk}/change/",
            {
                "order": self.order.id,
                "requester": self.buyer.id,
                "reason": "rewritten by hand",
                "status": "approved",
                "decided_by": self.finance.id,
                "_save": "Save",
            },
            follow=True,
        )

        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "pending")
        self.assertIsNone(self.request_record.decided_by)
        self.assertEqual(self.request_record.reason, "The bottle arrived damaged.")
        self.assertEqual(Refund.objects.count(), 0)

    def test_the_ask_is_line_totals_or_the_remaining_balance(self):
        order, first, second = self.order_with_two_lines()

        self.assertEqual(refund_request_amount(order, []), Decimal("600.50"))
        self.assertEqual(refund_request_amount(order, [second.id]), Decimal("100.50"))
        self.assertEqual(
            refund_request_amount(order, [first.id, second.id]),
            Decimal("600.50"),
        )

    def test_approval_refunds_the_requested_lines_and_marks_the_request(self):
        order, _first, second = self.order_with_two_lines()
        self.request_record.items = [second.id]
        self.request_record.order = order
        self.request_record.save(update_fields=["items", "order"])
        client = self.gateway()

        response = self.post_action(
            "approve_requests",
            [self.request_record],
            note="Approved after inspection",
        )

        self.assertEqual(response.status_code, 200)
        refund = Refund.objects.get()
        self.assertEqual(refund.amount, Decimal("100.50"))
        self.assertEqual(refund.kind, Refund.Kind.PARTIAL)
        self.assertEqual(refund.status, Refund.Status.PROCESSED)
        self.assertEqual(refund.reason, "Approved after inspection")
        self.assertEqual(
            refund.idempotency_key, f"refund-request-{self.request_record.pk}"
        )
        self.assertEqual(refund.actor_id, self.finance.pk)
        self.assertEqual(refund.gateway_refund_id, "rfnd_E02")
        client.refund.create.assert_called_once_with(
            {"payment_id": "pay_E02", "amount": 10050}, timeout=10
        )

        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "approved")
        self.assertEqual(self.request_record.decided_by, self.finance)
        self.assertIsNotNone(self.request_record.decided_at)

        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")
        self.assertEqual(order.refundable_remaining, Decimal("500.00"))

        entry = LogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(RefundRequest),
            object_id=str(self.request_record.pk),
        )
        self.assertEqual(entry.user_id, self.finance.pk)
        self.assertEqual(entry.action_flag, CHANGE)
        self.assertIn(
            f"Approved refund request #{self.request_record.pk}", entry.change_message
        )
        self.assertIn("Approved after inspection", entry.change_message)

    def test_a_whole_order_request_refunds_the_remaining_balance(self):
        order = self.captured(self.new_order())
        self.partial_refund(order)
        self.request_record.order = order
        self.request_record.items = []
        self.request_record.save(update_fields=["order", "items"])
        self.gateway()

        response = self.post_action(
            "approve_requests",
            [self.request_record],
            note="The rest of the order came back",
        )

        self.assertEqual(response.status_code, 200)
        refund = Refund.objects.get(idempotency_key__isnull=False)
        self.assertEqual(refund.amount, Decimal("400.00"))
        self.assertEqual(refund.kind, Refund.Kind.FULL)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(order.refundable_remaining, Decimal("0.00"))

    def test_a_gateway_refusal_leaves_the_request_pending(self):
        self.request_record.items = [self.line.id]
        self.request_record.save(update_fields=["items"])
        order = self.captured(self.order)
        client = self.gateway()
        client.refund.create.side_effect = razorpay.errors.BadRequestError(
            "provider internals"
        )

        response = self.post_action(
            "approve_requests",
            [self.request_record],
            note="Try to refund",
        )

        self.assertContains(
            response, "the payment gateway could not complete this refund"
        )
        self.assertNotContains(response, "provider internals")
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "pending")
        self.assertIsNone(self.request_record.decided_by)
        self.assertIsNone(self.request_record.decided_at)
        self.assertEqual(Refund.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "captured")
        self.assertIsNone(order.refunded_at)

    def test_an_item_request_above_the_remaining_balance_is_refused(self):
        order = self.captured(self.order)
        self.partial_refund(order)
        self.request_record.items = [self.line.id]
        self.request_record.save(update_fields=["items"])
        client = self.gateway()

        response = self.post_action(
            "approve_requests",
            [self.request_record],
            note="Over the remaining balance",
        )

        self.assertContains(response, "exceeds the refundable balance")
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "pending")
        self.assertEqual(Refund.objects.count(), 1)
        client.refund.create.assert_not_called()

    def test_a_request_naming_a_deleted_line_is_refused(self):
        self.captured(self.order)
        self.request_record.items = [self.line.id]
        self.request_record.save(update_fields=["items"])
        OrderItem.objects.filter(pk=self.line.pk).delete()
        client = self.gateway()

        response = self.post_action(
            "approve_requests",
            [self.request_record],
            note="Line is gone",
        )

        self.assertContains(response, "no longer on the order")
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "pending")
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_a_second_approval_is_a_no_op(self):
        self.request_record.items = [self.line.id]
        self.request_record.save(update_fields=["items"])
        self.captured(self.order)
        client = self.gateway()

        self.post_action(
            "approve_requests", [self.request_record], note="First decision"
        )
        second = self.post_action(
            "approve_requests", [self.request_record], note="Second decision"
        )

        self.assertContains(second, "already decided")
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "approved")
        self.assertEqual(Refund.objects.count(), 1)
        client.refund.create.assert_called_once()

    def test_the_idempotency_key_is_the_second_approval_authority(self):
        # Re-opening the request (a replay after an operator reset) must collapse onto the refund
        # the key already produced instead of moving the same money twice.
        self.request_record.items = [self.line.id]
        self.request_record.save(update_fields=["items"])
        self.captured(self.order)
        client = self.gateway()
        self.post_action(
            "approve_requests", [self.request_record], note="First decision"
        )
        RefundRequest.objects.filter(pk=self.request_record.pk).update(
            status=RefundRequest.Status.PENDING
        )

        response = self.post_action(
            "approve_requests", [self.request_record], note="Replayed decision"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Refund.objects.count(), 1)
        client.refund.create.assert_called_once()
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "approved")

    def test_rejection_moves_no_money_and_is_audited(self):
        client = self.gateway()

        response = self.post_action(
            "reject_requests",
            [self.request_record],
            note="Outside the refund policy",
        )

        self.assertEqual(response.status_code, 200)
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "rejected")
        self.assertEqual(self.request_record.decided_by, self.finance)
        self.assertIsNotNone(self.request_record.decided_at)
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()
        entry = LogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(RefundRequest),
            object_id=str(self.request_record.pk),
        )
        self.assertEqual(entry.user_id, self.finance.pk)
        self.assertEqual(entry.action_flag, CHANGE)
        self.assertIn(
            f"Rejected refund request #{self.request_record.pk}", entry.change_message
        )
        self.assertIn("Outside the refund policy", entry.change_message)

    def test_a_rejected_request_cannot_then_be_approved(self):
        self.captured(self.order)
        client = self.gateway()
        self.post_action("reject_requests", [self.request_record], note="Not eligible")

        response = self.post_action(
            "approve_requests", [self.request_record], note="Changed my mind"
        )

        self.assertContains(response, "already decided")
        self.request_record.refresh_from_db()
        self.assertEqual(self.request_record.status, "rejected")
        self.assertEqual(Refund.objects.count(), 0)
        client.refund.create.assert_not_called()

    def test_the_item_count_column_distinguishes_all_from_a_list(self):
        model_admin = admin.site._registry[RefundRequest]

        self.assertEqual(model_admin.item_count(self.request_record), "All")

        self.request_record.items = [self.line.id]
        self.assertEqual(model_admin.item_count(self.request_record), 1)

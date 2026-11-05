"""Account erasure by deidentification, not deletion (NEW-P0-2b, R-17.33).

An account that owns an order cannot be deleted without taking - or being
refused by - the financial record it owns. Erasure therefore tombstones the
account row in place: the person is gone from the login surface, the order,
its payment references and its refunds stay reconcilable, and the only audit
row added says who erased the account and why, never what the old identity
was.
"""

import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.test.utils import CaptureQueriesContext

from accounts.models import Address
from accounts.services import (
    AccountAlreadyErased,
    UnsafeErasureReason,
    erase_account,
)
from common.models import AuditEvent
from common.testing import ApiTestCase
from orders.models import Order, PaymentEvent, Refund

User = get_user_model()


class AccountDeidentificationTests(ApiTestCase):
    def _order_owned_by(self, user, order_number="ORD-2026-000601"):
        return Order.objects.create(
            user=user,
            order_number=order_number,
            full_name="Erasure Probe",
            phone="9876500201",
            address="1 Tombstone Row",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal("499.00"),
            payment_status="captured",
            razorpay_order_id="order_ERASE0001",
            razorpay_payment_id="pay_ERASE0001",
        )

    def test_erasing_an_account_with_orders_keeps_the_order_payment_and_refund_rows(
        self,
    ):
        buyer = self.make_user("erase-buyer", email="erase-buyer@example.com")
        operator = self.make_user("erasure-operator")
        order = self._order_owned_by(buyer)
        refund = Refund.objects.create(
            order=order,
            amount=Decimal("100.00"),
            reason="Erasure probe",
            kind=Refund.Kind.PARTIAL,
            status=Refund.Status.PROCESSED,
            gateway_refund_id="rfnd_ERASE0001",
        )
        payment_event = PaymentEvent.objects.create(
            event_id="evt_ERASE0001",
            event_type="payment.captured",
            order=order,
            gateway_payment_id="pay_ERASE0001",
            amount=Decimal("499.00"),
            payload={"id": "evt_ERASE0001"},
            outcome=PaymentEvent.Outcome.APPLIED,
        )

        # Reaching the assertions below is the "ProtectedError not raised"
        # half: a delete-based erasure would raise here on the retained
        # order's PROTECT foreign key (and would cascade the rows away
        # wherever that policy is absent) instead of returning a tombstone.
        erased = erase_account(buyer, actor=operator, reason="ticket #4401")

        self.assertEqual(erased.pk, buyer.pk)
        row = User.objects.get(pk=buyer.pk)
        self.assertEqual(row.username, f"erased-{buyer.pk}")
        self.assertEqual(row.email, f"erased-{buyer.pk}@invalid")
        self.assertEqual(row.first_name, "")
        self.assertEqual(row.last_name, "")
        self.assertFalse(row.is_active)
        self.assertFalse(row.has_usable_password())
        self.assertFalse(User.objects.filter(email="erase-buyer@example.com").exists())
        self.assertFalse(User.objects.filter(username="erase-buyer").exists())

        live_order = Order.objects.get(pk=order.pk)
        self.assertEqual(live_order.user_id, buyer.pk)
        self.assertEqual(live_order.razorpay_order_id, "order_ERASE0001")
        self.assertEqual(live_order.razorpay_payment_id, "pay_ERASE0001")
        self.assertEqual(live_order.payment_status, "captured")
        retained_refund = Refund.objects.get(pk=refund.pk)
        self.assertEqual(retained_refund.order_id, order.pk)
        self.assertEqual(retained_refund.gateway_refund_id, "rfnd_ERASE0001")
        self.assertTrue(PaymentEvent.objects.filter(pk=payment_event.pk).exists())

    def test_a_second_erasure_is_refused_without_changing_the_tombstone_or_the_trail(
        self,
    ):
        buyer = self.make_user("twice-erased", email="twice-erased@example.com")
        order = self._order_owned_by(buyer, order_number="ORD-2026-000602")
        erase_account(buyer, reason="first request")
        before = User.objects.values(
            "username", "email", "first_name", "last_name", "is_active", "password"
        ).get(pk=buyer.pk)
        audit_rows = AuditEvent.objects.filter(event_type="auth.account_erased").count()

        with self.assertRaises(AccountAlreadyErased) as caught:
            erase_account(buyer, reason="retry")

        self.assertIsInstance(caught.exception, ValueError)
        after = User.objects.values(
            "username", "email", "first_name", "last_name", "is_active", "password"
        ).get(pk=buyer.pk)
        self.assertEqual(after, before)
        self.assertEqual(
            AuditEvent.objects.filter(event_type="auth.account_erased").count(),
            audit_rows,
        )
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

    def test_an_account_without_orders_is_tombstoned_not_deleted(self):
        buyer = self.make_user("no-orders-yet", email="no-orders-yet@example.com")

        erase_account(buyer)

        self.assertTrue(User.objects.filter(pk=buyer.pk).exists())
        row = User.objects.get(pk=buyer.pk)
        self.assertEqual(row.username, f"erased-{buyer.pk}")
        self.assertEqual(row.email, f"erased-{buyer.pk}@invalid")
        self.assertEqual(row.first_name, "")
        self.assertEqual(row.last_name, "")
        self.assertFalse(row.is_active)
        self.assertFalse(row.has_usable_password())

    def test_an_unsaved_account_is_refused_before_touching_the_database(self):
        unsaved = User(username="never-saved")

        with self.assertRaises(ValueError):
            erase_account(unsaved)

        self.assertFalse(User.objects.filter(username="never-saved").exists())
        self.assertFalse(
            AuditEvent.objects.filter(event_type="auth.account_erased").exists()
        )

    def test_the_audit_row_names_the_actor_and_reason_without_the_old_identity(self):
        buyer = self.make_user("audited-buyer", email="audited-buyer@example.com")
        buyer.first_name = "Audited"
        buyer.last_name = "Buyer"
        buyer.save(update_fields=["first_name", "last_name"])
        operator = self.make_staff("erasure-operator")

        with self.assertLogs("common.audit", level="INFO") as captured:
            erase_account(buyer, actor=operator, reason="ticket #4402")

        event = AuditEvent.objects.get(event_type="auth.account_erased")
        self.assertEqual(event.actor_id, operator.pk)
        self.assertEqual(event.detail["action"], "account.erased")
        self.assertEqual(event.detail["user_id"], buyer.pk)
        self.assertEqual(event.detail["reason"], "ticket #4402")
        stored = json.dumps(event.detail)
        for old_value in (
            "audited-buyer@example.com",
            "audited-buyer",
            "Audited",
            "Buyer",
        ):
            self.assertNotIn(old_value, stored)
            self.assertNotIn(old_value, "\n".join(captured.output))

    def _address_owned_by(self, user, label="Home"):
        return Address.objects.create(
            user=user,
            label=label,
            line1="12 Rose Lane",
            city="Mumbai",
            state="Maharashtra",
            postal_code="400001",
            country="India",
            phone="9876500201",
        )

    def _account_snapshot(self, pk):
        return User.objects.values(
            "username", "email", "first_name", "last_name", "is_active", "password"
        ).get(pk=pk)

    def test_erasing_an_account_deletes_its_saved_addresses_and_no_one_elses(self):
        buyer = self.make_user("address-buyer", email="address-buyer@example.com")
        home = self._address_owned_by(buyer, label="Home")
        work = self._address_owned_by(buyer, label="Work")
        other = self.make_user("address-other", email="address-other@example.com")
        theirs = self._address_owned_by(other, label="Theirs")

        erase_account(buyer, reason="ticket #4403")

        self.assertEqual(Address.objects.filter(user_id=buyer.pk).count(), 0)
        self.assertFalse(Address.objects.filter(pk=home.pk).exists())
        self.assertFalse(Address.objects.filter(pk=work.pk).exists())
        self.assertEqual(Address.objects.filter(pk=theirs.pk).count(), 1)

    def test_a_reason_carrying_the_identity_being_erased_is_refused_and_writes_nothing(
        self,
    ):
        buyer = self.make_user("jane.doe", email="jane.doe@example.com")
        buyer.first_name = "Jane"
        buyer.last_name = "Doe"
        buyer.save(update_fields=["first_name", "last_name"])
        self._address_owned_by(buyer)
        before = self._account_snapshot(buyer.pk)
        reason = "customer jane.doe@example.com rang on 9876500299"

        with CaptureQueriesContext(connection) as queries:
            with self.assertNoLogs("common.audit", level="INFO"):
                with self.assertRaises(UnsafeErasureReason) as caught:
                    erase_account(buyer, reason=reason)

        # The refusal does not merely roll back later: it is raised before the
        # first write statement, so nothing needs undoing.
        statements = [
            query["sql"].strip().upper() for query in queries.captured_queries
        ]
        self.assertEqual(
            [
                sql
                for sql in statements
                if sql.startswith(("INSERT", "UPDATE", "DELETE"))
            ],
            [],
        )

        # The refusal is a value that can cross a boundary safely: it must not
        # echo the reason it rejected into a handler, a 400 body or a log.
        self.assertNotIn(reason, str(caught.exception))
        self.assertNotIn("9876500299", str(caught.exception))
        self.assertEqual(self._account_snapshot(buyer.pk), before)
        self.assertEqual(Address.objects.filter(user_id=buyer.pk).count(), 1)
        self.assertFalse(
            AuditEvent.objects.filter(event_type="auth.account_erased").exists()
        )

    def test_every_old_identifier_and_contact_pattern_is_refused_then_a_safe_reason_succeeds(
        self,
    ):
        buyer = self.make_user("jane.doe", email="jane.doe@example.com")
        buyer.first_name = "Jane"
        buyer.last_name = "Doe"
        buyer.save(update_fields=["first_name", "last_name"])
        unsafe_reasons = (
            "customer jane.doe@example.com asked",
            "account jane.doe asked",
            "call Jane back",
            "ask Doe about it",
            "contact Jane Doe",
            "forward to somebody.else@example.org",
            "mobile 9876500299",
            "mobile 98765-00299",
        )

        for reason in unsafe_reasons:
            with self.assertRaises(UnsafeErasureReason, msg=reason):
                erase_account(buyer, reason=reason)

        # Every refusal above is non-destructive, so the account is still the
        # one the operator was asked to erase.
        self.assertTrue(User.objects.filter(pk=buyer.pk, username="jane.doe").exists())

        erase_account(buyer, reason="ticket #4401 closed by post")

        row = User.objects.get(pk=buyer.pk)
        self.assertEqual(row.username, f"erased-{buyer.pk}")
        event = AuditEvent.objects.get(event_type="auth.account_erased")
        self.assertEqual(event.detail["reason"], "ticket #4401 closed by post")

    def test_a_failed_audit_write_rolls_back_the_tombstone_and_the_address_deletion(
        self,
    ):
        buyer = self.make_user("rollback-buyer", email="rollback-buyer@example.com")
        address = self._address_owned_by(buyer)

        with mock.patch.object(
            AuditEvent, "record", side_effect=DatabaseError("audit store down")
        ):
            with self.assertRaises(DatabaseError):
                erase_account(buyer, reason="ticket #4404")

        self.assertEqual(self._account_snapshot(buyer.pk)["username"], "rollback-buyer")
        self.assertEqual(
            self._account_snapshot(buyer.pk)["email"], "rollback-buyer@example.com"
        )
        self.assertTrue(Address.objects.filter(pk=address.pk).exists())

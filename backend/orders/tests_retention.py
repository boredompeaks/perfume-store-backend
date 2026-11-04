"""The retention boundary on `Order.user` (docs/retention.md, R-17.31).

An order is a financial record. Erasing a customer therefore deidentifies the
account row and keeps the order it owns; it never cascades the order (or the
refunds hanging from it) away with the login. These tests pin that boundary at
the ORM, where the policy lives, and re-prove the guest/account invariant the
FK change must not disturb.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, models, transaction
from django.db.models import ProtectedError

from common.testing import ApiTestCase
from orders.models import Order, Refund


class OrderRetentionTests(ApiTestCase):
    def _account_order(self, user, order_number="ORD-2026-000501"):
        return Order.objects.create(
            user=user,
            order_number=order_number,
            full_name="Retained Buyer",
            phone="9876500101",
            address="1 Retention Row",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal("499.00"),
        )

    def _guest_order(self, order_number="ORD-2026-000502"):
        return Order.objects.create(
            order_number=order_number,
            guest_email="retention-guest@example.com",
            guest_token="retention-guest-token-value",
            full_name="Retained Guest",
            phone="9876500102",
            address="2 Retention Row",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal("499.00"),
        )

    def test_deleting_an_account_with_an_order_is_refused_and_the_order_survives(
        self,
    ):
        user = self.make_user("retained-buyer")
        order = self._account_order(user)
        refund = Refund.objects.create(
            order=order,
            amount=Decimal("100.00"),
            reason="Retention probe",
            kind=Refund.Kind.PARTIAL,
        )

        with self.assertRaises(ProtectedError):
            user.delete()

        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        live = Order.objects.get(pk=order.pk)
        self.assertEqual(live.user_id, user.pk)
        self.assertTrue(Refund.objects.filter(pk=refund.pk).exists())

    def test_a_guest_order_has_no_account_to_protect(self):
        guest = self._guest_order()
        unrelated = self.make_user("no-orders-yet")

        unrelated.delete()

        self.assertFalse(User.objects.filter(pk=unrelated.pk).exists())
        live = Order.objects.get(pk=guest.pk)
        self.assertIsNone(live.user_id)
        self.assertEqual(live.guest_email, "retention-guest@example.com")

    def test_the_account_xor_guest_constraint_still_holds_for_both_shapes(self):
        user = self.make_user("xor-buyer")
        account = self._account_order(user)
        guest = self._guest_order()

        # Both documented shapes persist, and the two-owner columns agree.
        account.refresh_from_db()
        self.assertIsNone(account.guest_token)
        self.assertEqual(account.guest_email, "")
        self.assertIsNone(guest.user_id)
        constraint_names = {c.name for c in Order._meta.constraints}
        self.assertIn("orders_account_xor_guest_ck", constraint_names)

        # A hybrid row and an ownerless row are still refused by the database.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.create(
                user=user,
                order_number="ORD-2026-000503",
                guest_email="both@example.com",
                guest_token="both-owners-token",
                full_name="Hybrid Buyer",
                phone="9876500103",
                address="3 Retention Row",
                city="Mumbai",
                state="Maharashtra",
                pincode="400001",
                total_amount=Decimal("499.00"),
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.create(
                order_number="ORD-2026-000504",
                full_name="Ownerless Buyer",
                phone="9876500104",
                address="4 Retention Row",
                city="Mumbai",
                state="Maharashtra",
                pincode="400001",
                total_amount=Decimal("499.00"),
            )

    def test_an_account_with_no_orders_is_still_deletable(self):
        user = self.make_user("empty-account")

        user.delete()

        self.assertFalse(User.objects.filter(pk=user.pk).exists())

    def test_the_account_fk_is_protect_and_still_nullable(self):
        field = Order._meta.get_field("user")

        self.assertIs(field.remote_field.on_delete, models.PROTECT)
        self.assertTrue(field.null)

    def test_deleting_the_order_first_unblocks_the_account_erasure(self):
        # Outside the legal window the documented path is to delete the order
        # rows; PROTECT is a retention guard, not a permanent lock on the user.
        user = self.make_user("erased-after-orders")
        order = self._account_order(user, order_number="ORD-2026-000505")

        order.delete()
        user.delete()

        self.assertFalse(Order.objects.filter(pk=order.pk).exists())
        self.assertFalse(User.objects.filter(pk=user.pk).exists())

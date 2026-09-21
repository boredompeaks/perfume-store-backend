"""SPEC-12-03 [R-12.9]: the expire_reservations reconciler (§12.1 step 7).

Reading implemented: a hold still ACTIVE whose expires_at has lapsed is
marked EXPIRED (the model's own transition map assigns step 7 to EXPIRED;
RELEASED is step 6's in-flow vocabulary, owned by the 12-02 checkout/admin
writers), and each affected product lands one StockMovement ledger row —
delta 0 BY DESIGN because a hold never left on-hand stock (the SALE
decrement is verify_payment's conversion; a hold only gated
available-to-sell), so the row records reserved units returning to the
pool with stock_after gated on the locked on-hand.
"""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone

import products.management.commands.expire_reservations as sweep_module
from common.testing import ApiTestCase
from orders.models import Order
from products.models import StockMovement, StockReservation, products


class SweepHarness(ApiTestCase):
    """Shared fixtures: mint real checkouts, then backdate their holds."""

    def make_hold(self, username, product, quantity=1, expired=True):
        """Checkout through the public API so the hold is the real 12-02
        artifact, then (optionally) push its TTL into the past."""
        self.make_user(username)
        client = self.fresh_client()
        self.api_login(username, client=client)
        self.seed_session_cart([(product, quantity)], client=client)
        res = client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(pk=res.data["id"])
        if expired:
            StockReservation.objects.filter(order=order).update(
                expires_at=timezone.now() - timedelta(minutes=1)
            )
        return order


class ExpireReservationsCommandTests(SweepHarness):
    def test_no_stale_reservations_is_a_clean_noop(self):
        product = self.make_product(stock=5)
        self.make_hold("buyer", product, expired=False)
        out = StringIO()
        call_command("expire_reservations", verbosity=0, stdout=out)
        # The live hold survives, the ledger records nothing, and a
        # verbosity-0 sweep (the cron invocation) prints nothing.
        self.assertTrue(
            StockReservation.objects.filter(
                status=StockReservation.Status.ACTIVE
            ).exists()
        )
        self.assertEqual(StockMovement.objects.count(), 0)
        self.assertEqual(out.getvalue(), "")

    def test_expired_hold_is_released_with_a_ledger_row(self):
        product = self.make_product(stock=5)
        order = self.make_hold("buyer", product, quantity=2)

        out = StringIO()
        call_command("expire_reservations", stdout=out)

        self.assertIn("released 1", out.getvalue())
        hold = order.stock_reservations.get()
        self.assertEqual(hold.status, StockReservation.Status.EXPIRED)
        # On-hand is untouched by a release: the SALE decrement belongs to
        # verify_payment's conversion, not to the reconciler.
        product.refresh_from_db()
        self.assertEqual(product.stock, 5)
        movement = StockMovement.objects.get()
        self.assertEqual(movement.product, product)
        self.assertEqual(movement.delta, 0)
        self.assertEqual(movement.reason, StockMovement.Reason.CORRECTION)
        self.assertEqual(movement.stock_after, 5)
        self.assertIsNone(movement.created_by)
        self.assertIn(f"#{order.id}", movement.note)
        self.assertIn("available-to-sell", movement.note)

    def test_live_and_terminal_holds_are_untouched(self):
        live_product = self.make_product(name="Live", stock=3)
        converted_product = self.make_product(name="Sold", stock=3)
        released_product = self.make_product(name="Cancelled", stock=3)
        live_order = self.make_hold("alice", live_product, expired=False)
        converted_order = self.make_hold("bob", converted_product)
        released_order = self.make_hold("carol", released_product)
        # Terminal holds with lapsed TTLs: a status filter, not just the
        # expiry filter, keeps them terminal (no resurrection).
        StockReservation.objects.filter(order=converted_order).update(
            status=StockReservation.Status.CONVERTED
        )
        StockReservation.objects.filter(order=released_order).update(
            status=StockReservation.Status.RELEASED
        )

        call_command("expire_reservations", verbosity=0)

        statuses = dict(
            StockReservation.objects.values_list("order_id", "status")
        )
        self.assertEqual(statuses[live_order.pk], StockReservation.Status.ACTIVE)
        self.assertEqual(
            statuses[converted_order.pk], StockReservation.Status.CONVERTED
        )
        self.assertEqual(
            statuses[released_order.pk], StockReservation.Status.RELEASED
        )
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_rerun_is_idempotent(self):
        product = self.make_product(stock=4)
        self.make_hold("buyer", product, quantity=1)

        call_command("expire_reservations", verbosity=0)
        self.assertEqual(StockMovement.objects.count(), 1)
        call_command("expire_reservations", verbosity=0)

        # Exactly one ledger row across both runs: the second sweep finds
        # nothing matching (status, expires_at) and must not double-book.
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(
            StockReservation.objects.filter(
                status=StockReservation.Status.EXPIRED
            ).count(),
            1,
        )

    def test_one_ledger_row_per_product_references_every_order(self):
        shared = self.make_product(name="Shared", stock=9)
        other = self.make_product(name="Other", stock=6)
        first = self.make_hold("alice", shared, quantity=2)
        second = self.make_hold("bob", shared, quantity=3)
        third = self.make_hold("carol", other, quantity=1)

        call_command("expire_reservations", verbosity=0)

        self.assertEqual(
            StockReservation.objects.filter(
                status=StockReservation.Status.EXPIRED
            ).count(),
            3,
        )
        movements = StockMovement.objects.order_by("product_id")
        self.assertEqual(movements.count(), 2)
        shared_row = movements.get(product=shared)
        self.assertEqual(shared_row.stock_after, 9)
        self.assertIn(f"#{first.id}", shared_row.note)
        self.assertIn(f"#{second.id}", shared_row.note)
        self.assertNotIn(f"#{third.id}", shared_row.note)
        other_row = movements.get(product=other)
        self.assertEqual(other_row.stock_after, 6)
        self.assertIn(f"#{third.id}", other_row.note)
        shared.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(shared.stock, 9)
        self.assertEqual(other.stock, 6)

    def test_hold_rearmed_mid_sweep_is_skipped_not_released(self):
        """Race: a checkout retry re-targets a lapsed hold (fresh TTL)
        between the sweep's snapshot and its per-product flip — the same
        window _mint_order_reservations' IntegrityError re-arm runs in.
        The conditional UPDATE re-filters on status AND expiry at flip
        time, so the re-armed hold survives with no phantom ledger row,
        while the untouched product's release proceeds."""
        raced = self.make_product(name="Raced", stock=3)
        clean = self.make_product(name="Clean", stock=5)
        raced_order = self.make_hold("alice", raced, quantity=2)
        clean_order = self.make_hold("bob", clean, quantity=1)

        real_manager = products.objects
        rearmed = False

        class RacingManager:
            # Minimal shim over the real manager: the first product the
            # sweep locks wins the race for the "raced" hold, re-arming it
            # exactly like the checkout retry's re-target would.
            def select_for_update(self):
                return self

            def get(self, **kwargs):
                nonlocal rearmed
                if not rearmed:
                    rearmed = True
                    StockReservation.objects.filter(order=raced_order).update(
                        expires_at=timezone.now() + timedelta(minutes=10)
                    )
                return real_manager.select_for_update().get(**kwargs)

        with patch.object(sweep_module, "products") as shimmed:
            shimmed.objects = RacingManager()
            call_command("expire_reservations", verbosity=0)

        self.assertEqual(
            raced_order.stock_reservations.get().status,
            StockReservation.Status.ACTIVE,
        )
        self.assertEqual(
            clean_order.stock_reservations.get().status,
            StockReservation.Status.EXPIRED,
        )
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().product, clean)

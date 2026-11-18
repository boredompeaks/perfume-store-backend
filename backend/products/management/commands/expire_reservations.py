"""Scheduled sweep marking lapsed ACTIVE stock reservations EXPIRED.

Sweeps the reservation book only: payment truth is untouched and a late
verify still proceeds on verify_payment's re-checked stock, so the cadence
must run well inside RESERVATION_TTL, which must exceed the
checkout-session window.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from products.models import StockMovement, StockReservation, products


class Command(BaseCommand):
    help = (
        "Release expired checkout stock reservations (SPEC-12-03, §12.1 "
        "step 7): active holds with a lapsed expires_at are marked "
        "expired and each affected product lands a StockMovement ledger "
        "row. Idempotent: flips are conditional on status AND expiry, so "
        "holds released elsewhere or re-armed by a checkout retry are "
        "never double-released or resurrected."
    )

    def handle(self, *args, **options):
        # Returns None: call_command would print a non-None handle() result
        # as output text; the stdout summary is the operator-visible count.
        released = self._expire_stale(timezone.now())
        if options.get("verbosity", 1) >= 1:
            self.stdout.write(
                f"expire_reservations: released {released} stale reservation(s)."
            )

    def _expire_stale(self, now):
        """Flip active+lapsed holds to EXPIRED and land the ledger rows.

        Locks products in ascending-pk order (matching verify_payment, so no
        lock-order inversion) and re-checks status AND expiry in the
        conditional UPDATE: a hold re-armed or released concurrently no
        longer matches, so it is skipped rather than resurrected or
        double-released.
        """
        stale = list(
            StockReservation.objects.filter(
                status=StockReservation.Status.ACTIVE,
                expires_at__lte=now,
            ).values("id", "product_id", "order_id", "quantity")
        )
        if not stale:
            return 0

        by_product = {}
        for row in stale:
            by_product.setdefault(row["product_id"], []).append(row)

        released = 0
        with transaction.atomic():
            for product_id in sorted(by_product):
                rows = by_product[product_id]
                product = products.objects.select_for_update().get(pk=product_id)
                flipped = StockReservation.objects.filter(
                    pk__in=[row["id"] for row in rows],
                    status=StockReservation.Status.ACTIVE,
                    expires_at__lte=now,
                ).update(status=StockReservation.Status.EXPIRED)
                released += flipped
                if flipped:
                    self._record_release(product, rows)
        return released

    @staticmethod
    def _record_release(product, rows):
        """One ledger row per affected product, with delta 0 by design.

        A hold never left on-hand stock — it gated available-to-sell only —
        so the row records units returning to the pool, gated on the locked
        on-hand, with no actor because the sweep is a system action.
        """
        refs = ", ".join(f"#{row['order_id']}" for row in rows)
        units = sum(row["quantity"] for row in rows)
        StockMovement.objects.create(
            product=product,
            delta=0,
            reason=StockMovement.Reason.CORRECTION,
            stock_after=product.stock,
            note=(
                f"Expired checkout holds released (orders {refs}): "
                f"{units} unit(s) back to available-to-sell; on-hand "
                f"unchanged"
            )[:200],
            created_by=None,
        )

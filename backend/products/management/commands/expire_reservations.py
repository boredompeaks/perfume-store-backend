"""Scheduled stale-reservation reconciliation (SPEC-12-03, [R-12.9]).

§12.1 step 7: "A scheduled reconciliation process identifies stale
reservations." This command is that process: every hold still ACTIVE whose
``expires_at`` has lapsed is marked EXPIRED (the model's transition map
assigns step 7 to EXPIRED; RELEASED is step 6's in-flow vocabulary, owned
by the SPEC-12-02 checkout/admin writers), and each affected product lands
one StockMovement ledger row — [R-12.9] makes the ledger row the
requirement, not a bare status flip.

Coordination (documented in .env.example): the cadence must run well
inside RESERVATION_TTL, and the TTL must exceed the payment-provider
checkout-session window. The sweep reclaims the reservation book only —
payment truth (webhook/timeout reconciliation, SPEC-1-06/SPEC-1-18) is
never touched, and a late verify of a swept checkout still proceeds on
verify_payment's re-checked stock (SPEC-12-02 deliberately made a lapsed
TTL a non-gate for conversion).
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
        # No return value: call_command treats a non-None handle() result
        # as output text; the operator-visible count is the stdout summary
        # (silent under --verbosity 0, as cron wants).
        released = self._expire_stale(timezone.now())
        if options.get("verbosity", 1) >= 1:
            self.stdout.write(
                f"expire_reservations: released {released} stale "
                f"reservation(s)."
            )

    def _expire_stale(self, now):
        """Flip active+lapsed holds to EXPIRED and land the ledger rows.

        Locking: each product row is locked before its flip so the
        movement's ``stock_after`` gates on the real on-hand (conventions:
        stock-adjacent flows lock what they read) and product locks are
        taken in ascending-pk order — the same order verify_payment uses —
        ruling out lock-order inversion with a concurrent verify. The flip
        itself is an atomic conditional UPDATE re-checking status AND
        expiry at UPDATE time: a hold re-armed by a checkout retry (fresh
        TTL, the _mint_order_reservations IntegrityError path) or released
        by a concurrent writer no longer matches, so it is skipped —
        never resurrected, never double-released.
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
                product = products.objects.select_for_update().get(
                    pk=product_id
                )
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
        """One ledger row per affected product (the [6.5.17] idiom's
        fields, with delta 0 BY DESIGN): a hold never left on-hand stock —
        it gated available-to-sell only, and the SALE decrement belongs to
        verify_payment's conversion — so the row records reserved units
        returning to the pool, with ``stock_after`` gated on the locked
        on-hand and no actor (the sweep is a system action, like the SALE
        row)."""
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

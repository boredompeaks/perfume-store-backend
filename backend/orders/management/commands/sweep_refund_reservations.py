"""``manage.py sweep_refund_reservations [--dry-run]``.

Settles PENDING refund reservations whose ``reserved_until`` has lapsed as
FAILED ``reservation_expired`` rows. The refund writer leaves such a row
behind when the process dies after committing the reservation but before the
gateway outcome is applied; the row already stops reducing the order's
balance once it lapses, but without this command nothing ever records WHY it
stopped reserving.

The sweep is deliberately visible, never a silent release:

* only rows with a NON-NULL, lapsed ``reserved_until`` are selected - a NULL
  expiry means "held indefinitely by design" and is never touched;
* each row is re-checked under ``status=PENDING`` in its own atomic update,
  so a row that settled concurrently is left alone (and not counted);
* every swept row is logged with the reason code and printed with its order
  and amount, and the summary line reports the count.

``--dry-run`` reports the same candidates and changes nothing. Any unexpected
error exits 1 through ``CommandError`` rather than looking like a clean sweep.
"""

import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from orders.models import Refund

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Settle PENDING refund reservations whose reserved_until has lapsed "
        "as FAILED (failure_code=reservation_expired). --dry-run reports "
        "without changing anything; NULL reserved_until rows are never swept."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the expired reservations without changing any row.",
        )

    def handle(self, *args, **options):
        try:
            self._sweep(dry_run=options["dry_run"])
        except Exception as exc:
            logger.exception("Refund reservation sweep failed: %s", exc)
            raise CommandError(f"refund reservation sweep failed: {exc}") from exc

    def _sweep(self, *, dry_run):
        now = timezone.now()
        candidates = list(
            Refund.objects.filter(
                status=Refund.Status.PENDING,
                reserved_until__isnull=False,
                reserved_until__lte=now,
            )
            .order_by("pk")
            .values_list("pk", "order_id", "amount")
        )

        swept = 0
        for refund_pk, order_id, amount in candidates:
            if dry_run:
                self.stdout.write(
                    f"would sweep refund {refund_pk} "
                    f"(order {order_id}, amount {amount})"
                )
                continue

            with transaction.atomic():
                updated = Refund.objects.filter(
                    pk=refund_pk,
                    status=Refund.Status.PENDING,
                ).update(
                    status=Refund.Status.FAILED,
                    failure_code=Refund.FailureCode.RESERVATION_EXPIRED,
                    reserved_until=None,
                    updated_at=now,
                )
            if updated != 1:
                # A concurrent settlement/retry won the row; it is not an
                # expired reservation any more, so nothing is released and
                # nothing is counted.
                logger.info(
                    "Refund reservation %s was no longer PENDING; left alone.",
                    refund_pk,
                )
                continue

            swept += 1
            logger.warning(
                "Swept expired refund reservation %s (order %s, amount %s): "
                "failure_code=%s.",
                refund_pk,
                order_id,
                amount,
                Refund.FailureCode.RESERVATION_EXPIRED,
            )
            self.stdout.write(
                f"swept refund {refund_pk} (order {order_id}, amount {amount})"
            )

        if dry_run:
            self.stdout.write(
                f"swept 0 expired refund reservation(s) "
                f"(dry run; {len(candidates)} would be swept)"
            )
        else:
            self.stdout.write(f"swept {swept} expired refund reservation(s)")

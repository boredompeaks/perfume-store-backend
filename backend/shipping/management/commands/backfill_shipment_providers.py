"""``manage.py backfill_shipment_providers [--dry-run|--apply]``.

Links legacy parcels (carrier text, no provider FK) to ``ShippingProvider``
rows. The matching rule lives in ``shipping.backfill`` so the dry-run and the
apply run cannot disagree; this command only chooses whether to write and
reports the outcome. It is safe to run repeatedly: matched rows lose their NULL
provider, so the next pass has nothing to do.
"""

from django.core.management.base import BaseCommand

from shipping.backfill import backfill_shipments


class Command(BaseCommand):
    help = (
        "Link legacy Shipment.carrier text to ShippingProvider by code "
        "(case-insensitive) or exact name, only when exactly one provider "
        "matches. Default is a dry run; --apply writes."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be linked without writing (the default).",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Write the unambiguous links.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        counts = backfill_shipments(apply=apply)
        verb = "linked" if apply else "would link"
        self.stdout.write(
            f"[backfill_shipment_providers] {verb} {counts['matched']} parcel(s); "
            f"ambiguous {counts['ambiguous']}, unmatched {counts['unmatched']}, "
            f"already linked {counts['already_linked']}"
        )
        if not apply:
            self.stdout.write("  dry run: no rows were written (pass --apply)")

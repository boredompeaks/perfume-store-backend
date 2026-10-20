"""Delete abandoned anonymous carts (D-04b).

A guest cart is never converted into anything: a visitor who never signs in
leaves a session-keyed row that no account owns and no order points at. This
command is the scheduled cleanup for those rows, and it deliberately cannot
touch anything else:

* only ``user IS NULL`` rows — an account cart is the customer's basket and
  is never pruned, however old;
* only empty carts — a row with a line is a basket someone may still sign in
  to claim, so the merge path, not the janitor, decides its fate;
* only carts no order links — a checked-out cart is history the capture
  flow still clears through the link.

Age is measured on ``updated_at`` (the last write), not ``created_at``: a
guest who added an item yesterday evening has an old row but a live basket.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cart.models import Cart


class Command(BaseCommand):
    help = "Delete empty, orderless, anonymous carts older than --days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Minimum age in days, measured from updated_at (default: 30).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 0:
            raise CommandError("--days must be zero or greater.")

        cutoff = timezone.now() - timedelta(days=days)
        stale = Cart.objects.filter(
            user__isnull=True,
            order__isnull=True,
            items__isnull=True,
            updated_at__lt=cutoff,
        )
        matched = stale.count()

        if options["dry_run"]:
            self.stdout.write(
                f"Dry run: {matched} empty anonymous cart(s) older than "
                f"{days} day(s) would be deleted."
            )
            return

        stale.delete()
        # Report the matched count (the rows this command selected), not the
        # ORM's cascade-inclusive delete count.
        self.stdout.write(
            f"Deleted {matched} empty anonymous cart(s) older than {days} day(s)."
        )

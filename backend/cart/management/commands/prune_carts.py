"""Delete abandoned anonymous carts.

Only ``user IS NULL``, empty, orderless rows are eligible — an account cart is
the customer's basket, a cart with lines may still be claimed at sign-in, and a
checked-out cart is history. Age is measured on ``updated_at`` so a live basket
is never pruned for being old.
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
        # Report the matched count, not the ORM's cascade-inclusive delete count.
        self.stdout.write(
            f"Deleted {matched} empty anonymous cart(s) older than {days} day(s)."
        )

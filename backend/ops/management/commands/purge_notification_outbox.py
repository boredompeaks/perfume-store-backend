"""Scheduled deletion of spent notification-outbox rows, the named deletion owner for ``NotificationOutbox``.

The outbox payload deliberately keeps unscrubbed one-time token material, so every row past ``expires_at`` is deleted; rows inside their window are never touched.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from common.models import NotificationOutbox


class Command(BaseCommand):
    help = (
        "Delete notification-outbox rows whose expires_at has passed, which is "
        "the compensating control for the outbox payload not being scrubbed "
        "of one-time token material. Rows inside their window are never "
        "touched: a pending row there is a notification still owed. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="report how many rows would be deleted, and delete none.",
        )
        parser.add_argument(
            "--grace-seconds",
            type=int,
            default=0,
            help=(
                "how much LONGER than the configured bound to keep an expired "
                "row. The cutoff is moved back by this many seconds, so 3600 "
                "deletes a row one hour after it expired rather than at the "
                "instant it did. Clamped at zero: a negative value cannot "
                "move the cutoff forward and delete a row that has not "
                "expired."
            ),
        )

    def handle(self, *args, **options):
        # No return value: call_command treats a non-None handle() result as
        # output text, and the count is the stdout summary.
        grace = max(0, options.get("grace_seconds") or 0)
        cutoff = timezone.now() - timedelta(seconds=grace)
        spent = NotificationOutbox.objects.filter(expires_at__lte=cutoff)
        if options.get("dry_run"):
            doomed = spent.count()
        else:
            # The delete is one statement and needs no atomic block: the
            # predicate is re-evaluated in the database.
            doomed, _ = spent.delete()
        if options.get("verbosity", 1) >= 1:
            verb = "would delete" if options.get("dry_run") else "deleted"
            self.stdout.write(
                f"purge_notification_outbox: {verb} {doomed} expired "
                f"notification outbox row(s) at or before {cutoff.isoformat()}."
            )

"""Drain the durable notification outbox: the operator surface for one pass.

One pass, not a resident worker, and ``--retry-dead`` refuses a row past its
retention window rather than re-opening something that can no longer send.
"""

import argparse

from django.core.management import call_command
from django.core.management.base import BaseCommand

from common import notifications
from common.models import NotificationOutbox


def positive_int(value):
    """argparse type for ``--batch-size``: a positive integer.

    Zero would fall through to the configured default and a negative value would
    examine nothing; both look like an answer, so refuse them.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an integer; --batch-size is how many rows one "
            "pass claims at most"
        )
    if number < 1:
        raise argparse.ArgumentTypeError(
            f"--batch-size must be a positive integer, got {number}; a pass "
            "bounded at zero or below would examine nothing and report it as "
            "an idle queue"
        )
    return number


class Command(BaseCommand):
    help = (
        "Send queued notification-outbox rows: claim each under a row lock, "
        "send it off the response path, retry a failed send with exponential "
        "backoff up to NOTIFICATION_OUTBOX_MAX_ATTEMPTS, and dead-letter a "
        "row that has exhausted them or whose stored reference cannot be "
        "resolved. One pass; schedule it. Idempotent and safe to run "
        "concurrently with itself."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=positive_int,
            default=None,
            help=(
                "how many rows this pass will claim at most; must be a "
                "positive integer. Defaults to "
                "NOTIFICATION_OUTBOX_DRAIN_BATCH_SIZE. The pass stops early "
                "whenever there is nothing claimable left, so this is a "
                "ceiling and not a wait."
            ),
        )
        parser.add_argument(
            "--status-only",
            action="store_true",
            help=(
                "report how many rows sit in each status and send nothing. "
                "The per-status breakdown is printed by every run; this "
                "suppresses the pass itself, which is what an operator wants "
                "when they are deciding whether to use --retry-dead."
            ),
        )
        parser.add_argument(
            "--retry-dead",
            action="store_true",
            help=(
                "re-open dead-lettered rows for another attempt before "
                "draining, preserving their attempt count so a row that fails "
                "again returns to the dead-letter instead of retrying forever. "
                "A row past its expires_at is REFUSED and left dead: the "
                "payload is deliberately not scrubbed of one-time token "
                "material, so the retention window is the control that bounds "
                "it and this command will not extend it. Both counts are "
                "reported."
            ),
        )
        parser.add_argument(
            "--event-type",
            default=None,
            help=(
                "restrict --retry-dead to one event type (e.g. order.paid). "
                "Ignored without --retry-dead."
            ),
        )
        parser.add_argument(
            "--purge-expired",
            action="store_true",
            help=(
                "also run purge_notification_outbox. Off by default: that "
                "purge deletes every row past its expiry whatever its status, "
                "including dead-lettered ones an operator has not looked at."
            ),
        )

    def handle(self, *args, **options):
        # No return value: call_command treats a non-None handle() result as
        # output text.
        if options.get("purge_expired"):
            call_command("purge_notification_outbox", verbosity=options["verbosity"])

        requeued = notifications.DeadRetryResult(0, 0, ())
        if options.get("retry_dead"):
            requeued = notifications.retry_dead_notifications(
                event_type=options.get("event_type")
            )

        result = None
        if not options.get("status_only"):
            result = notifications.drain_notifications(batch_size=options["batch_size"])

        if options.get("verbosity", 1) >= 1:
            self.stdout.write(
                self._summary(
                    result,
                    requeued,
                    retried=bool(options.get("retry_dead")),
                )
            )

    def _summary(self, result, requeued, retried):
        """One line whose numbers all came from this run.

        The per-status breakdown always prints, so "0 dead" and "nothing looked
        at" stay distinguishable. The retry clause is keyed on the FLAG, not the
        counts, so a run that moved nothing still says it ran.
        """
        counts = notifications.outbox_status_counts()
        backlog = ", ".join(
            f"{status} {counts[status]}" for status in NotificationOutbox.Status.values
        )
        retried_clause = (
            f"re-opened {requeued.requeued} dead row(s), {requeued.expired} left "
            "dead past their expiry; "
            if retried
            else ""
        )
        if result is None:
            return (
                f"drain_notification_outbox: --status-only; {retried_clause}"
                f"backlog is {backlog}."
            )
        return (
            f"drain_notification_outbox: examined {result.examined}; sent "
            f"{result.sent}, failed {result.failed}, dead-lettered {result.dead}, "
            f"vanished {result.vanished}; {retried_clause}backlog is {backlog}."
        )

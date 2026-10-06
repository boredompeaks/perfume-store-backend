"""Drain the durable notification outbox: claim, send, retry, dead-letter.

ASYNC-2c2, and the half of §19.3 that the ASYNC-2c1 substrate was waiting for.
One pass of the loop lives in ``common.notifications.drain_notifications``;
this is its operator surface.

**One pass, not a resident worker.** Like ``expire_reservations`` and
``purge_notification_outbox`` this is a sweep a scheduler runs, and that is a
deliberate choice rather than an omission: the spec's §19.3 list is a list of
CAPABILITIES and mandates no broker or daemon, so the question this task has to
answer is "can a queued notification be claimed, retried with backoff,
dead-lettered, observed and manually retried", not "which worker runs it". A
process that sleeps in a loop would answer the second question at the cost of
the first - it would need its own supervision, its own shutdown handling, and a
second thing to restart when it wedged. Scheduling is documented in
``.env.example``; a run interval well inside the retry backoff is what keeps a
row's actual wait close to the backoff it was given.

Nothing here is required to keep the table small: ``purge_notification_outbox``
owns deletion, and this command offers ``--purge-expired`` rather than doing it
unasked. Default off, because that purge removes dead-lettered rows too and a
failure an operator has not looked at yet is the row most worth keeping.

``--retry-dead`` is the manual-retry capability §19.3 asks for. Its
authorization is this project's existing one for operator actions - control of
the process running ``manage.py``. There is no request and no user in a
management command, so there is no capability to evaluate, and the alternative
(a new capability plus an endpoint) is owned by ``common/roles.py``, which this
task does not touch. What is deliberately NOT here is an inline ``is_staff``
check pretending to be a gate.

Idempotent and safe to run concurrently with itself, which is the same pair of
properties every other sweep in this project claims and the reason two workers
are safe: a row is claimed under a row lock with its send state re-checked, so
a second invocation skips rows this one holds, and rows this pass fails are
left claimable rather than half-written.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand

from common import notifications
from common.models import NotificationOutbox


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
            type=int,
            default=None,
            help=(
                "how many rows this pass will claim at most. Defaults to "
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
                "again returns to the dead-letter instead of retrying forever."
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
        # output text, and the operator-visible summary is the stdout line
        # below (silent under --verbosity 0, as a cron wants).
        if options.get("purge_expired"):
            call_command("purge_notification_outbox", verbosity=options["verbosity"])

        requeued = 0
        if options.get("retry_dead"):
            requeued = notifications.retry_dead_notifications(
                event_type=options.get("event_type")
            )

        result = None
        if not options.get("status_only"):
            result = notifications.drain_notifications(batch_size=options["batch_size"])

        if options.get("verbosity", 1) >= 1:
            self.stdout.write(self._summary(result, requeued))

    def _summary(self, result, requeued):
        """One line, and every number in it came from this run.

        The per-status breakdown is printed even after a pass, because "0
        dead" and "nothing was looked at" have to be distinguishable to whoever
        is deciding whether the queue is healthy.
        """
        counts = notifications.outbox_status_counts()
        backlog = ", ".join(
            f"{status} {counts[status]}" for status in NotificationOutbox.Status.values
        )
        if result is None:
            return f"drain_notification_outbox: --status-only; backlog is {backlog}."
        return (
            f"drain_notification_outbox: examined {result.examined}; sent "
            f"{result.sent}, failed {result.failed}, dead-lettered {result.dead}, "
            f"vanished {result.vanished}; re-opened {requeued} dead row(s); "
            f"backlog is {backlog}."
        )

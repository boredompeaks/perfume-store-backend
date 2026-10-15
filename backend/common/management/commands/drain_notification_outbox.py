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

``--retry-dead`` REFUSES a dead-lettered row whose retention window has
already closed, and the summary line says so with its own count. Re-opening one
would not make it sendable — the claim predicate refuses an expired row too —
so it would become a pending row that is never claimed and never sent, and the
status report would stop showing a dead-lettered row that plainly exists. See
``common.notifications.retry_dead_notifications`` for the full reasoning.

Idempotent and safe to run concurrently with itself, which is the same pair of
properties every other sweep in this project claims and the reason two workers
are safe: a row is claimed under a row lock with its send state re-checked, so
a second invocation skips rows this one holds, and rows this pass fails are
left claimable rather than half-written.
"""

import argparse

from django.core.management import call_command
from django.core.management.base import BaseCommand

from common import notifications
from common.models import NotificationOutbox


def positive_int(value):
    """argparse ``type`` for ``--batch-size``: a whole number, at least one.

    An unvalidated limit is silent in both directions, which is why this is
    here rather than a ``if limit <= 0`` inside the pass. Zero fell through the
    ``batch_size or _setting(...)`` idiom to the configured default, so an
    operator who typed ``--batch-size 0`` got a FULL pass they had not asked
    for; a negative value bounded the claim slice at nothing, so the same
    operator got an empty report that reads exactly like an idle queue. Both
    are worse than a refusal, because both look like an answer.

    Raised as ``ArgumentTypeError`` so argparse names the offending option and
    its value, which ``CommandError`` from ``handle()`` could not: the mistake
    is in the invocation, not in the state of the queue.
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
        # output text, and the operator-visible summary is the stdout line
        # below (silent under --verbosity 0, as a cron wants).
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
        """One line, and every number in it came from this run.

        The per-status breakdown is printed even after a pass, because "0
        dead" and "nothing was looked at" have to be distinguishable to whoever
        is deciding whether the queue is healthy.

        The retry clause is printed whenever ``--retry-dead`` ran and omitted
        when it did not, so its presence says the flag was used rather than
        that rows moved. It is keyed on the FLAG, not on the counts, for the
        same reason the other numbers are: an absent number reads as a zero,
        and this command's predecessor had exactly that failure — a re-opened
        row that was never claimed produced "re-opened 1" and a backlog with no
        dead rows, describing neither thing that happened. Keyed on the counts
        it would have gone silent on ``--retry-dead`` that matched nothing,
        which is the one answer an operator running it most wants.

        ``retried`` is true for the ``--status-only --retry-dead`` combination
        too. That is the operator deciding whether to use ``--retry-dead``, so
        a run of it that re-opened rows without saying so would be the one
        place this command fails to explain itself.
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

"""Scheduled deletion of spent notification-outbox rows (ASYNC-2c1).

This command is the **named deletion owner** for ``NotificationOutbox``, and it
exists now rather than being deferred, because the outbox payload is
deliberately NOT run through ``common.audit.clean_audit_payload``: a
password-reset or email-verification notification is built out of exactly the
one-time token material that scrubber drops, and dropping it here would break
the mail the queue exists to deliver. Not scrubbing is a deliberate decision;
leaving the table to fill with unscubbed payloads forever would be an
unmitigated one. Deletion on a clock is the compensating control, and this is
where it runs.

Two independent bounds protect the token, and only one of them is this repo's:
Django's ``PASSWORD_RESET_TIMEOUT`` caps how long the token is *valid*, and
``expires_at`` caps how long the *copy sitting in the queue* survives. This
command enforces the second.

What is deleted, and why each half is safe:

- **Every row past ``expires_at``**, whatever its status. A row that has been
  drained is finished; a row that has NOT been drained by then is stranded —
  either its notification failed in a way no retry will fix, or nothing has
  drained the table at all (true until ASYNC-2c2). Neither is a reason to keep
  a payload that may hold credential material.
- **Nothing that has not expired.** A pending row inside its window is a
  notification still owed, and this command must never be the reason a
  customer stops hearing about an order.

Honest limit, stated rather than papered over: nothing in this repository
schedules it. There is no cron, no celery beat and no worker loop yet, so an
operator must run it (documented cadence in ``.env.example``, which must be
well inside ``NOTIFICATION_OUTBOX_TTL_SECONDS``) or ASYNC-2c2's drain loop
must call it on each tick. Until one of those exists, this table accumulates.
That is a property of the substrate, not of this command.

Idempotent, like every sweep in this project: it deletes by predicate and
reports a count, so running it twice deletes nothing the second time and
never resurrects a row.
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
                "add this many seconds to the expiry cutoff, for an operator "
                "who needs a longer grace window than the configured bound. "
                "Never subtracts."
            ),
        )

    def handle(self, *args, **options):
        # No return value: call_command treats a non-None handle() result as
        # output text; the operator-visible count is the stdout summary
        # (silent under --verbosity 0, as cron wants).
        grace = max(0, options.get("grace_seconds") or 0)
        cutoff = timezone.now() - timedelta(seconds=grace)
        spent = NotificationOutbox.objects.filter(expires_at__lte=cutoff)
        if options.get("dry_run"):
            doomed = spent.count()
        else:
            # The delete is a single statement and needs no atomic block of
            # its own: the predicate is re-evaluated by the database, and a
            # row inserted after the count is simply not this run's business.
            doomed, _ = spent.delete()
        if options.get("verbosity", 1) >= 1:
            verb = "would delete" if options.get("dry_run") else "deleted"
            self.stdout.write(
                f"purge_notification_outbox: {verb} {doomed} expired "
                f"notification outbox row(s) at or before {cutoff.isoformat()}."
            )

"""Celery tasks for the three scheduled sweeps.

Each calls its management command through ``call_command``, so the scheduled tick
and an operator's manual run execute one implementation.
"""

from celery import shared_task
from django.core.management import call_command

# Silence is the cron behaviour these commands were written for; the counts still
# live in the database and the command's return path.
_VERBOSITY = 0


@shared_task(name="common.tasks.expire_reservations")
def expire_reservations():
    """Run the stale-reservation sweep (one pass), exactly as the command does."""
    call_command("expire_reservations", verbosity=_VERBOSITY)


@shared_task(name="common.tasks.drain_notification_outbox")
def drain_notification_outbox():
    """Run the outbox drain (one pass): claim, send, retry, dead-letter.

    No ``--retry-dead`` or ``--purge-expired``: both are operator decisions that
    should not run unattended.
    """
    call_command("drain_notification_outbox", verbosity=_VERBOSITY)


@shared_task(name="common.tasks.purge_notification_outbox")
def purge_notification_outbox():
    """Delete outbox rows past ``expires_at``; unexpired rows are still owed and
    untouched."""
    call_command("purge_notification_outbox", verbosity=_VERBOSITY)

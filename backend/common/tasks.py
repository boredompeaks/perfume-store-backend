"""The three scheduled sweeps, as Celery tasks (SPEC-2-03 / SPEC-19-1).

ASYNC-INFRA. Every sweep in this project was written as a management command
and then, separately, never scheduled - which is the finding this module
closes. ``CELERY_BEAT_SCHEDULE`` in ``config.settings`` points three beat
entries here, and each task below runs exactly the management command it is
named after.

**The whole design is `call_command`, and that is the point.**

The obvious implementation is for each task to import the underlying function
- ``notifications.drain_notifications``, or the ``Command.handle`` bodies -
and call it. That creates two paths into the same behaviour: the operator's
``manage.py drain_notification_outbox`` and the beat tick. Two paths are two
things to keep in step, and they drift: a flag added to the command reaches the
operator but not the schedule, a retry bound changed in the task leaves the
command reporting something the schedule no longer does, and the suite can be
green over both because each tests only its own door.

Calling the command instead means there is ONE implementation. The operator's
manual run and the scheduled tick execute the same code, so a divergence is not
merely unlikely, it is unrepresentable. It also means these tasks carry no
policy at all - no thresholds, no batch sizes, no flags - because every policy
knob already lives in settings and is read by the command that owns it.

Two consequences worth stating rather than hiding:

- **A task failure is a task failure.** If a sweep raises, celery records the
  exception and the beat entry still fires next tick; the sweep is idempotent,
  so a repeated tick over the same rows is safe. There is no retry wrapper here
  on purpose - the drain command already has bounded retry with backoff and
  dead-lettering for the rows it cannot send, and a second retry layer above it
  would double-count attempts against a policy that counts them.
- **The task is a thin shim, so `call_command`'s own failure modes apply.** It
  is in-process, not a subprocess, and it raises on a non-zero command exit, so
  celery sees a genuine task error rather than a silent success.

**Nothing here converts a notification call site; the conversion lives in
the call sites themselves** (TIER1-ASYNC-CONVERT turned the three business
sites to ``enqueue``). The ordering that made that conversion safe was
deliberate and the dark failure it avoided is concrete: a call site
converted before a drain exists writes a row nobody reads, and the
customer's mail simply never arrives - a regression no suite catches
unless it is looking for exactly that, which is why the drain suite pins
the converted sites end to end (queue, then drain, then mail).
"""

from celery import shared_task
from django.core.management import call_command

# Silence is deliberate and is the cron behaviour these commands were written
# for: each one prints its summary only at verbosity >= 1, because the beat
# schedule is unattended and its stdout is a worker's log, not an operator's
# terminal. The counts still exist - in the command's own return path and in
# what it writes to the database - they are just not echoed on every tick. An
# operator who wants the breakdown runs the command by hand, verbosity and all.
_VERBOSITY = 0


@shared_task(name="common.tasks.expire_reservations")
def expire_reservations():
    """Run SPEC-12-03's stale-reservation sweep (one pass).

    Named for the command it runs. Every active hold whose ``expires_at`` has
    lapsed is marked expired with its StockMovement ledger row, exactly as
    ``manage.py expire_reservations`` does - see this module's docstring for why
    the command is called rather than its internals.
    """
    call_command("expire_reservations", verbosity=_VERBOSITY)


@shared_task(name="common.tasks.drain_notification_outbox")
def drain_notification_outbox():
    """Run SPEC-19.3's outbox drain: claim, send, retry, dead-letter.

    One pass, on the cadence ``CELERY_BEAT_SCHEDULE`` sets. This is the sweep
    that makes ``common.notifications.enqueue`` meaningful: since
    TIER1-ASYNC-CONVERT the business call sites queue through it, and this
    tick is what turns their rows into delivered mail.

    No ``--retry-dead`` here and no ``--purge-expired``: both are operator
    decisions with a flag for a reason. Re-opening dead letters is an
    intervention on a specific failure somebody has looked at, and purging
    early deletes dead-lettered rows an operator has not looked at yet. A
    schedule that did either unattended would be making that decision 1440 times
    a day for nobody.
    """
    call_command("drain_notification_outbox", verbosity=_VERBOSITY)


@shared_task(name="common.tasks.purge_notification_outbox")
def purge_notification_outbox():
    """Run the outbox retention sweep: delete rows past ``expires_at``.

    The compensating control for the outbox payload deliberately not being run
    through the audit scrubber - see the command's own docstring, which is
    authoritative on what is deleted and why. Nothing unexpired is ever
    touched: a pending row inside its window is a notification still owed.
    """
    call_command("purge_notification_outbox", verbosity=_VERBOSITY)

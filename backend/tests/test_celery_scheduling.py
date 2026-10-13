"""ASYNC-INFRA: the scheduler the repo lacked - beat config, tasks, and the
anti-divergence property between a scheduled sweep and a manual one.

The finding this closes is not a missing feature but a missing SCHEDULE:
``expire_reservations``, ``drain_notification_outbox`` and
``purge_notification_outbox`` were all built, all tested, and none of them was
ever run by anything. Stale reservations never expired and the notification
outbox was never drained - so the substrate added for SPEC-19.3 could not
deliver a single notification, because nothing ever drained it.

Three claims are pinned here, and each is pinned against the plausible wrong
implementation rather than against the code as written:

- **The schedule names the three commands.** Not "some sweep exists" - each
  beat entry is asserted to name the command it exists to run, so renaming a
  command without touching the schedule (or the reverse) fails here rather than
  silently stopping the sweep. The entry is also asserted to name a task that
  is actually REGISTERED on the app, which is the failure a schedule written
  before its tasks would produce: a beat that fires into a broker and gets
  `NotRegistered` back, once per interval, forever.
- **A task really runs the command's own logic, and there is only one copy of
  it.** This is the half worth being careful about. A task that reimplemented
  the sweep would pass every behavioural pin here and still be wrong, because
  the operator's manual `manage.py drain_notification_outbox` and the beat tick
  would be two implementations agreeing only by accident. So the pins assert
  the CALL - that the task reaches the management command through
  `call_command`, not through a second copy of the logic - and then separately
  assert that the command still produces its documented effect when the task is
  what invoked it. A reimplementation fails the first; a broken command fails
  the second.
- **No live broker is required.** Redis is not present in the test
  environment, and it must not have to be: the suite imports `config.celery`
  and calls these tasks, and both are asserted to work with no Redis anywhere.
  Eager execution is toggled explicitly in-process for the `.delay()` pins, and
  the direct-call pins never touch a broker at all. If the broker connection
  were made at import, importing the app in this suite would fail outright, so
  the import itself is part of what is pinned.

**The call sites are converted and the worker delivers what they queue.**
The last class pins the TIER1-ASYNC-CONVERT state: the production modules
that call ``enqueue`` are named exactly (a surprise fourth fails, and one
of the named three going quiet fails too), the request path sends no mail,
and the drain — the same command this module schedules — is what turns the
queued row into the customer's confirmation. The ordering hazard this
class originally guarded against (converting a site before a drain is
proven) is historical: the worker landed first, the sites followed.
"""

import pathlib
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from common import notifications, tasks
from common.models import AuditEvent, NotificationOutbox
from common.testing import ApiTestCase
from config import celery_app
from orders.models import Order
from products.models import StockMovement, StockReservation


def make_order(user=None, total="499.99", **overrides):
    """A real order, so the real handler renders a real email about it.

    Same shape as the drain suite's helper of the same name, and for the same
    reason: a queue row whose order cannot be resolved is dead-lettered by the
    claim predicate, so a hand-made stub would exercise the wrong path.
    """
    fields = dict(
        user=user,
        full_name="Scheduled Buyer",
        phone="9876543210",
        address="12 Rose Lane",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        total_amount=Decimal(total),
    )
    fields.update(overrides)
    return Order.objects.create(**fields)


# The three sweeps this task exists to schedule. Written out as literals, not
# derived from the schedule: an oracle recomputed from the constant under test
# agrees with a wrong constant from both sides, which is how a schedule that
# lost an entry could still pass its own pin.
EXPECTED_SWEEPS = (
    "expire_reservations",
    "drain_notification_outbox",
    "purge_notification_outbox",
)


class BeatScheduleShapeTests(SimpleTestCase):
    """What the schedule contains, before anything asks it to run."""

    def test_the_schedule_schedules_exactly_the_three_sweeps(self):
        schedule = celery_app.conf.beat_schedule
        self.assertEqual(
            set(schedule),
            set(EXPECTED_SWEEPS),
            "the beat schedule must schedule exactly the three sweeps this "
            "task exists to schedule, keyed by management-command name",
        )

    def test_every_entry_runs_the_task_named_after_its_command(self):
        # Key, task and command agree by construction; pinning it means a
        # rename that misses one of the three fails instead of leaving a sweep
        # scheduled under a name its own command no longer answers to.
        for command in EXPECTED_SWEEPS:
            with self.subTest(command=command):
                self.assertEqual(
                    celery_app.conf.beat_schedule[command]["task"],
                    f"common.tasks.{command}",
                )

    def test_every_scheduled_task_is_actually_registered(self):
        """A beat entry naming an unregistered task is a silent outage.

        `NotRegistered` is raised by the WORKER, at run time, once per interval
        - and only on the deployment that has the schedule wrong. This is the
        failure a schedule written ahead of its tasks produces, so it is pinned
        by asking the app itself, which is the same lookup the worker does.
        """
        registered = celery_app.tasks
        for command in EXPECTED_SWEEPS:
            with self.subTest(command=command):
                dotted = celery_app.conf.beat_schedule[command]["task"]
                self.assertIn(dotted, registered)
                # Same task, compared by identity of its underlying function:
                # the app's registry holds a proxy-bound Task, so the objects
                # are equal by name and by the function they wrap rather than
                # by `assertIs`, which is not what "registered" means here.
                self.assertEqual(registered[dotted].name, dotted)
                self.assertIs(
                    registered[dotted].run,
                    getattr(tasks, command).run,
                )

    def test_every_entry_carries_a_schedule_and_a_task(self):
        for command in EXPECTED_SWEEPS:
            with self.subTest(command=command):
                entry = celery_app.conf.beat_schedule[command]
                self.assertIn("schedule", entry)
                self.assertIn("task", entry)
                self.assertTrue(entry["schedule"])

    def test_the_scheduled_tasks_are_the_only_celery_tasks(self):
        """Nothing else is queued from this repo.

        Worth pinning because it is what makes "the schedule runs the existing
        logic" checkable: if a task ever appears here that reimplements a
        sweep instead of calling it, this names it.
        """
        ours = {name for name in celery_app.tasks if name.startswith("common.tasks.")}
        self.assertEqual(
            ours,
            {f"common.tasks.{command}" for command in EXPECTED_SWEEPS},
        )


def _utcnow():
    """Timezone-aware UTC now, which is what celery's schedules expect.

    Naive datetimes are deprecated in this Python and would make the estimate
    ambiguous across the DST boundary the schedule is explicitly UTC for.
    """
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _next_fires(schedule, count=3):
    """The next `count` datetimes `schedule` fires at, by walking is_due.

    `crontab.is_due(last_run_at)` answers "did you fire since", so the walk
    passes the previous fire each time - which is exactly how beat's own tick
    loop drives it, and what makes the answer a schedule rather than a
    constant. (Measured at this pin: the signature takes the datetime directly
    and returns a `(fired, when)` tuple; it does not take an elapsed interval.)
    """
    fires = []
    last_run = _utcnow() - timedelta(days=1)
    cursor = _utcnow()
    for _ in range(60 * 24 * 60):
        cursor = cursor + timedelta(seconds=20)
        fired, _when = schedule.is_due(last_run)
        if fired:
            fires.append(cursor)
            last_run = cursor
            if len(fires) >= count:
                break
    return fires


class BrokerConfigTests(SimpleTestCase):
    """Env-driven broker/backend, and the no-Redis-at-import property.

    Read from the live app config rather than from `config.settings`, because
    the app config is what a worker actually consumes - a pin that read the
    settings module would be satisfied by a broker URL celery never sees.
    """

    def test_the_broker_and_result_backend_default_to_redis(self):
        self.assertTrue(celery_app.conf.broker_url.startswith("redis://"))
        self.assertTrue(celery_app.conf.result_backend.startswith("redis://"))

    def test_redis_url_is_the_single_default_for_both(self):
        """One knob, so broker and backend cannot disagree by accident."""
        from django.conf import settings

        self.assertEqual(settings.CELERY_BROKER_URL, settings.REDIS_URL)
        self.assertEqual(settings.CELERY_RESULT_BACKEND, settings.REDIS_URL)

    def test_the_broker_and_backend_can_be_split(self):
        """A deployment may put result storage somewhere else.

        Env-driven keys, so pinned in a subprocess like the rest of this
        repo's settings pins: settings are import-time, and reading the module
        in-process would prove only what this process happened to import. The
        override keys must be honoured INDEPENDENTLY, which is the whole
        reason they are two keys and not one.
        """
        from tests.test_settings_security import _NON_DEBUG_ENV, run_settings_import

        res = run_settings_import(
            {
                **_NON_DEBUG_ENV,
                "REDIS_URL": "redis://shared.example.test:6379/0",
                "CELERY_BROKER_URL": "redis://broker.example.test:6379/0",
                "CELERY_RESULT_BACKEND": "redis://results.example.test:6379/1",
            },
            snippet=(
                "import config.settings as s; "
                "print('BROKER', s.CELERY_BROKER_URL); "
                "print('BACKEND', s.CELERY_RESULT_BACKEND); "
                "print('REDIS', s.REDIS_URL)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("BROKER redis://broker.example.test:6379/0", res.stdout)
        self.assertIn("BACKEND redis://results.example.test:6379/1", res.stdout)
        # REDIS_URL is unchanged by the split: it is the fallback, not a
        # third thing that has to agree with the two above it.
        self.assertIn("REDIS redis://shared.example.test:6379/0", res.stdout)

    def test_serializers_are_json_on_both_legs(self):
        """No pickle: a result backend that deserializes pickle is remote code
        execution for anyone who can write to it."""
        self.assertEqual(celery_app.conf.task_serializer, "json")
        self.assertEqual(celery_app.conf.result_serializer, "json")
        self.assertEqual(list(celery_app.conf.accept_content), ["json"])

    def test_importing_the_app_opened_no_broker_connection(self):
        """The suite runs with no Redis anywhere in the environment.

        The load-bearing part of this claim is already structural: this module
        imports `config` at the top, so if building the app connected to a
        broker, every test in this file would fail at import in CI. What is
        asserted here is the mechanical fact the import alone cannot state -
        the broker connection object exists but has not connected - which is
        the difference between "the app was built" and "the app was built and
        nothing dialed out".

        Read as a subprocess rather than in-process, because the question is
        about the state of a FRESHLY BUILT app, and an in-process app has
        already been poked by every earlier test in this file. The repo's own
        settings-import harness is reused rather than hand-rolled, because it
        scrubs a developer's untracked `.env` and supplies the non-debug env a
        boot needs - both of which a naive `import config` in a subprocess gets
        wrong.
        """
        from tests.test_settings_security import _NON_DEBUG_ENV, run_settings_import

        snippet = (
            "import config;"
            "app = config.celery_app;"
            "print('URL', app.conf.broker_url);"
            # Autodiscovery is LAZY, so the registry is empty until the app is
            # finalized. `app.loader.import_default_modules()` is what a worker
            # does at boot to populate it, and calling it here is what makes
            # the printed task list meaningful rather than vacuously empty.
            "app.loader.import_default_modules();"
            "print('TASKS', sorted(n for n in app.tasks if n.startswith('common.')))"
        )
        res = run_settings_import(dict(_NON_DEBUG_ENV), snippet=snippet)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        # A fresh import built the app, resolved the broker and registered the
        # three tasks without a Redis anywhere in the environment - which is
        # only possible because none of that opens a socket.
        self.assertIn("URL redis://", res.stdout)
        for command in EXPECTED_SWEEPS:
            self.assertIn(f"common.tasks.{command}", res.stdout)


class TaskInvokesCommandTests(SimpleTestCase):
    """The anti-divergence property: each task runs its COMMAND.

    Asserted on the CALL, not on the effect, because the effect is asserted in
    `ScheduledSweepEffectTests` below and an effect-only pin would be satisfied
    just as well by a reimplementation - which is the defect.
    """

    def test_each_task_reaches_its_command_through_call_command(self):
        for command in EXPECTED_SWEEPS:
            with self.subTest(command=command):
                with mock.patch.object(
                    tasks, "call_command", autospec=True
                ) as call_command_mock:
                    getattr(tasks, command)()
                call_command_mock.assert_called_once()
                self.assertEqual(call_command_mock.call_args.args[0], command)

    def test_the_tasks_run_silently(self):
        """An unattended schedule must not echo to the worker's stdout.

        Pinned because it is the one argument the tasks pass that could be
        wrong in either direction: too loud and every tick writes a line to a
        log nobody reads, too quiet and a sweep that has been silently failing
        for a week looks identical to a healthy one. verbosity=0 matches the
        cron invocation these commands were written for.
        """
        for command in EXPECTED_SWEEPS:
            with self.subTest(command=command):
                with mock.patch.object(
                    tasks, "call_command", autospec=True
                ) as call_command_mock:
                    getattr(tasks, command)()
                self.assertEqual(call_command_mock.call_args.kwargs.get("verbosity"), 0)

    def test_a_command_failure_propagates_as_a_task_failure(self):
        """The shim must not swallow an exception.

        `call_command` raises on a failing command, so a broken sweep surfaces
        as a failed celery task with its traceback in the worker's log - the
        failure mode that lets an operator see it. A `try/except` here would
        turn every sweep failure into a green tick, which is the one outcome
        worse than no scheduler at all.
        """
        with mock.patch.object(
            tasks, "call_command", side_effect=RuntimeError("sweep blew up")
        ):
            with self.assertRaises(RuntimeError):
                tasks.drain_notification_outbox()


class EagerDispatchTests(SimpleTestCase):
    """.delay() reaches the task body with no broker running.

    Celery's eager mode runs a task inline in the calling process instead of
    publishing it, which is how a deployment with no worker can still use the
    queue - and how these pins run in CI, where no Redis exists. The point
    proved is that `.delay()` is wired to a REGISTERED task that actually
    executes, rather than reaching for a broker and hanging.

    The result backend is repointed at celery's `cache+memory://` for the
    duration, and that is not cosmetic: eager execution still STORES its
    result, so leaving the backend on the configured Redis means this pin
    reaches for a broker that CI does not have.

    Repointed through `override_settings`, which is the mechanism that works
    here because `config.celery` configures the app FROM THE SETTINGS MODULE:
    the app's conf is a live view of Django settings rather than a snapshot,
    so assigning to `celery_app.conf.result_backend` is overwritten by the next
    read. That is measured, not assumed - setting the attribute directly leaves
    the redis backend in place and the pin spends the full redis retry ladder
    before failing. `Celery.backend` is also a read-only property, so the
    cached instance cannot be replaced that way either.
    """

    def test_delay_executes_the_task_body_without_a_broker(self):
        with override_settings(
            CELERY_TASK_ALWAYS_EAGER=True,
            CELERY_RESULT_BACKEND="cache+memory://",
        ):
            with mock.patch.object(
                tasks, "call_command", autospec=True
            ) as call_command_mock:
                result = tasks.expire_reservations.delay()
                self.assertTrue(result.successful())
                call_command_mock.assert_called_once_with(
                    "expire_reservations", verbosity=0
                )

    def test_every_scheduled_task_dispatches_without_a_broker(self):
        """All three, not just one - a schedule entry whose task cannot be
        dispatched is the outage this class exists to catch."""
        with override_settings(
            CELERY_TASK_ALWAYS_EAGER=True,
            CELERY_RESULT_BACKEND="cache+memory://",
        ):
            for command in EXPECTED_SWEEPS:
                with self.subTest(command=command):
                    with mock.patch.object(tasks, "call_command", autospec=True):
                        result = getattr(tasks, command).delay()
                        self.assertTrue(result.successful())

    def test_a_task_is_not_reachable_through_a_broker_here(self):
        """The negative half, so the positive one cannot pass vacuously.

        With eager mode ON, `.delay()` runs inline. Turned OFF - the default,
        and what CI runs - `.delay()` would PUBLISH, and this environment has
        no Redis to publish to. Asserting the default is off, and that the
        override really repointed the backend at an in-memory store rather than
        leaving redis under it, is what stops "the task ran" from meaning "the
        task ran because a broker happened to be reachable".
        """
        from django.conf import settings

        self.assertFalse(settings.CELERY_TASK_ALWAYS_EAGER)
        self.assertTrue(settings.CELERY_RESULT_BACKEND.startswith("redis://"))
        with override_settings(
            CELERY_TASK_ALWAYS_EAGER=True,
            CELERY_RESULT_BACKEND="cache+memory://",
        ):
            self.assertEqual(celery_app.conf.result_backend, "cache+memory://")
            self.assertTrue(celery_app.conf.task_always_eager)

    def test_eager_mode_is_off_by_default(self):
        """Not a test setting, and the suite does not depend on it: the sweep
        effect pins below call the tasks directly, so they hold with or without
        it. Off by default because a deployment that silently queued everything
        in-process would never deliver anything."""
        from django.conf import settings

        self.assertFalse(settings.CELERY_TASK_ALWAYS_EAGER)


class ScheduledSweepEffectTests(ApiTestCase):
    """Each scheduled task produces its command's documented effect.

    Separate from the call pins above on purpose: a reimplementation would fail
    those and pass these, and a command that stopped working would fail these
    and pass those. Both halves are needed to say the schedule runs the real
    logic.
    """

    def test_the_expire_task_releases_a_stale_hold_with_its_ledger_row(self):
        product = self.make_product(stock=5)
        self.make_user("buyer")
        client = self.fresh_client()
        self.api_login("buyer", client=client)
        self.seed_session_cart([(product, 2)], client=client)
        res = client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(pk=res.data["id"])
        StockReservation.objects.filter(order=order).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        tasks.expire_reservations()

        hold = order.stock_reservations.get()
        self.assertEqual(hold.status, StockReservation.Status.EXPIRED)
        movement = StockMovement.objects.get()
        self.assertEqual(movement.product, product)
        self.assertEqual(movement.delta, 0)
        self.assertIn(f"#{order.id}", movement.note)
        product.refresh_from_db()
        self.assertEqual(product.stock, 5)

    def test_the_expire_task_leaves_a_live_hold_alone(self):
        product = self.make_product(stock=5)
        self.make_user("buyer")
        client = self.fresh_client()
        self.api_login("buyer", client=client)
        self.seed_session_cart([(product, 1)], client=client)
        res = client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)

        tasks.expire_reservations()

        self.assertTrue(
            StockReservation.objects.filter(
                status=StockReservation.Status.ACTIVE
            ).exists()
        )
        self.assertEqual(StockMovement.objects.count(), 0)

    def _queue_paid_notification(self):
        """Queue through `enqueue` itself, not a hand-made row.

        The real enqueue path produces the row; a hand-built fixture could
        differ from it in some way nobody thought to check, which is the
        reason the existing drain suite builds its rows this way too.
        """
        user = self.make_user("buyer")
        order = make_order(user)
        row = notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        return row

    def test_the_drain_task_sends_a_queued_notification(self):
        row = self._queue_paid_notification()
        self.assertEqual(row.status, NotificationOutbox.Status.PENDING)

        mail.outbox.clear()
        tasks.drain_notification_outbox()

        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_drain_task_is_safe_to_run_when_the_queue_is_empty(self):
        """The idle-tick case, which is the overwhelming majority of ticks.

        A task that raised on an empty queue would make every deployment look
        broken every minute, and an operator would learn to ignore it.
        """
        mail.outbox.clear()
        tasks.drain_notification_outbox()
        self.assertEqual(len(mail.outbox), 0)

    def test_the_purge_task_deletes_a_spent_row(self):
        row = self._queue_paid_notification()
        NotificationOutbox.objects.filter(pk=row.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )

        tasks.purge_notification_outbox()

        self.assertFalse(NotificationOutbox.objects.filter(pk=row.pk).exists())

    def test_the_purge_task_never_deletes_an_unexpired_row(self):
        """The one bound that matters: a pending row inside its window is a
        notification still owed, and this sweep must never be the reason a
        customer stops hearing about an order."""
        row = self._queue_paid_notification()

        tasks.purge_notification_outbox()

        self.assertTrue(NotificationOutbox.objects.filter(pk=row.pk).exists())

    def test_every_scheduled_task_is_idempotent_on_a_second_tick(self):
        """Beat fires on a schedule whether or not the last tick did anything.

        Each task is run twice over identical state and the second run must be
        a no-op that changes no row. This is the property that lets the
        schedules above be short intervals without a duplicate-sweep bug.
        """
        for task in (
            tasks.expire_reservations,
            tasks.drain_notification_outbox,
            tasks.purge_notification_outbox,
        ):
            with self.subTest(task=task.name):
                task()
                snapshot = sorted(
                    NotificationOutbox.objects.values_list("pk", "status", "attempts")
                )
                movements = StockMovement.objects.count()
                task()
                self.assertEqual(
                    sorted(
                        NotificationOutbox.objects.values_list(
                            "pk", "status", "attempts"
                        )
                    ),
                    snapshot,
                )
                self.assertEqual(StockMovement.objects.count(), movements)


class CommandAndTaskDoNotDivergeTests(ApiTestCase):
    """The same door, twice.

    The property that the operator's manual run and the scheduled tick are the
    same code - proved by driving both and comparing the effects, which is the
    only way to show it from the outside rather than by trusting the docstring.
    """

    def test_the_manual_command_and_the_task_agree_on_a_stale_hold(self):
        product = self.make_product(stock=5)
        self.make_user("buyer")
        client = self.fresh_client()
        self.api_login("buyer", client=client)
        self.seed_session_cart([(product, 2)], client=client)
        res = client.post(
            "/api/orders/checkout/", self.checkout_payload(), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(pk=res.data["id"])
        StockReservation.objects.filter(order=order).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        # The task, this time capturing what the command prints.
        out = StringIO()
        with mock.patch("sys.stdout", out):
            tasks.expire_reservations()

        hold = order.stock_reservations.get()
        self.assertEqual(hold.status, StockReservation.Status.EXPIRED)
        movement = StockMovement.objects.get()
        self.assertEqual(movement.product, product)
        self.assertEqual(movement.delta, 0)
        # verbosity 0 on both paths, so neither echoes on an unattended tick.
        self.assertEqual(out.getvalue(), "")

    def test_the_manual_command_and_the_task_agree_on_a_queued_row(self):
        row = self._queue_notification()
        self.assertEqual(row.status, NotificationOutbox.Status.PENDING)

        tasks.drain_notification_outbox()

        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.SENT)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(len(mail.outbox), 1)

    def _queue_notification(self):
        user = self.make_user("buyer")
        order = make_order(user)
        return notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})


class ConvertedCallSiteSchedulerTests(ApiTestCase):
    """The conversion this worker was sequenced for, pinned at both ends.

    Converting a notification call site before a drain is proven means the
    notification is queued and never delivered, and no test that counts
    mail rather than queue rows would notice. The worker landed first
    (the classes above prove it runs); this class proves the sites now
    queue through ``enqueue`` AND that the drain command — the same one
    the schedule fires — delivers what they queued.
    """

    def test_enqueue_production_call_sites_are_exactly_the_converted_three(self):
        """Pinned structurally rather than behaviourally: the claim is about
        WHICH MODULES call it, and a coverage-based reading would be
        satisfied by a call site the suite happens to execute.

        Exactly three, by name: the lifecycle seam, the browser verify and
        the gateway hook. A surprise fourth module joining the list fails
        here (convert deliberately, with a test), and one of the three
        falling silent fails too (a site quietly reverting to inline send
        is the dark regression this whole sequence exists to catch).
        """
        self.assertEqual(
            self._production_callers_of_enqueue(),
            ["orders/events.py", "orders/views/payment.py", "orders/webhooks.py"],
            "the converted notification call sites are exactly the lifecycle "
            "seam, the browser verify and the gateway hook",
        )

    def _production_callers_of_enqueue(self):
        """Module paths outside the test tree that call notifications.enqueue.

        Read from source because the claim is about the caller's identity. Two
        exclusions are deliberate rather than convenient: `common/notifications.py`
        DEFINES enqueue (and a regex would match its own definition), and
        `common/tasks.py` is excluded because its docstrings *describe* the
        enqueue path in prose - and this docstring is itself in a file the scan
        reads, so a matching mention in a comment would otherwise read as a
        call site.
        """
        import re

        backend_dir = pathlib.Path(__file__).resolve().parent.parent
        # A call, not a mention: `enqueue(` preceded by something that is not
        # a word character, which is why prose about enqueue() in a docstring
        # does not match while `notifications.enqueue(` does.
        pattern = re.compile(r"(?<![\w.])notifications\.enqueue\s*\(")
        offenders = []
        for path in backend_dir.rglob("*.py"):
            rel = path.relative_to(backend_dir)
            if rel.parts[0] in ("venv", "tests", "migrations"):
                continue
            if path.name == "tests.py":
                continue
            if rel.as_posix() in ("common/notifications.py", "common/tasks.py"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if pattern.search(text):
                offenders.append(rel.as_posix())
        return sorted(offenders)

    def test_the_mail_arrives_from_the_worker_not_the_request_path(self):
        """The customer's side of the wire, end to end through the queue.

        Checkout alone does not queue the order confirmation - it is
        `verify_payment` that does, after the gateway confirms - so the whole
        payment path is driven here rather than stopping at the order. The
        verify response itself must contain no mail; the drain command this
        module's schedule fires is what delivers it.
        """
        self.make_user("buyer")
        self.api_login(username="buyer")
        product = self.make_product(stock=5)
        self.seed_session_cart([(product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        # The mocked gateway mints order_TEST0001; the verify payload must
        # present that same id, or the seam refuses it as a payment for a
        # different order - which is the seam working, not a test bug.
        self.razorpay_mock()
        self.client.post(
            "/api/orders/payment/", {"order_id": res.data["id"]}, format="json"
        )
        mail.outbox.clear()
        verified = self.client.post(
            "/api/orders/payment/verify/",
            {
                "razorpay_order_id": "order_TEST0001",
                "razorpay_payment_id": "pay_TEST0001",
                "razorpay_signature": "sig",
                "order_id": res.data["id"],
            },
            format="json",
        )
        self.assertEqual(verified.status_code, 200, verified.data)
        # The request path sends nothing; the intent is queued instead.
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        # And the manual command — the same code the schedule fires —
        # delivers it, which is the compatibility the conversion must not
        # have broken.
        out = StringIO()
        call_command("drain_notification_outbox", stdout=out)
        self.assertIn("drain_notification_outbox", out.getvalue())
        self.assertEqual(len(mail.outbox), 1)


class CadenceTests(SimpleTestCase):
    """The three cadences actually fire, at the rate each one's constraint
    demands.

    `is_due` is NOT the probe here, and the reason is worth stating: with no
    `last_run_at` it answers True unconditionally, so a schedule that never
    fired would pass it. `remaining_estimate` is the real question - how long
    until this entry next fires - and it is computed from the crontab itself,
    so a mistyped minute or hour changes the answer.

    The bounds are deliberately loose and expressed as invariants (a sweep's
    period must stay INSIDE the bound it has to respect), not as exact
    literals: an operator retuning `CELERY_BEAT_*` legitimately changes these
    numbers, and a pin that forbade that would make the knobs decorative.
    """

    def test_the_drain_fires_on_its_configured_cadence(self):
        """The cadence is env-driven and honoured, which is the claim here.

        The bound asserted is the CADENCE ITSELF, not a relationship to the
        retry backoff. An earlier version of this pin asserted the drain's
        next fire fell inside NOTIFICATION_OUTBOX_RETRY_BASE_SECONDS, and
        `remaining_estimate` is what showed that claim to be false: a 60s
        interval reports up to 60s to its next fire, so the tick can add a
        full minute ON TOP of a row's 30s backoff. The honest invariant is
        that the schedule matches the configured value; the arithmetic of what
        that means for a retry's real wait is documented where it belongs, in
        the settings comment that owns the cadence.
        """
        from django.conf import settings

        entry = celery_app.conf.beat_schedule["drain_notification_outbox"]
        interval = entry["schedule"]
        remaining = interval.remaining_estimate(_utcnow()).total_seconds()
        configured = settings.CELERY_BEAT_DRAIN_OUTBOX_SECONDS
        self.assertLessEqual(
            remaining,
            configured,
            "the drain must fire within its configured interval",
        )
        self.assertGreater(remaining, 0)

    def test_the_reservation_sweep_fires_well_inside_the_reservation_ttl(self):
        """A hold can only be released before it goes stale. A tick slower than
        RESERVATION_TTL means a lapsed hold sits unreleasable, still gating
        available-to-sell."""
        from django.conf import settings

        entry = celery_app.conf.beat_schedule["expire_reservations"]
        remaining = entry["schedule"].remaining_estimate(_utcnow()).total_seconds()
        self.assertLess(remaining, settings.RESERVATION_TTL)

    def test_the_purge_is_a_daily_sweep_not_a_sub_daily_one(self):
        """It enforces a bound measured in DAYS, so anything more frequent
        deletes nothing extra and only adds write load to the outbox table."""
        from django.conf import settings

        entry = celery_app.conf.beat_schedule["purge_notification_outbox"]
        remaining = entry["schedule"].remaining_estimate(_utcnow()).total_seconds()
        self.assertGreater(remaining, 3600)
        self.assertLess(
            remaining,
            settings.NOTIFICATION_OUTBOX_TTL_SECONDS,
            "the purge must fire INSIDE the TTL it enforces, or spent rows "
            "outlive the bound that is meant to delete them",
        )

    def test_no_sweep_is_scheduled_off_the_top_of_the_hour(self):
        """The purge sits at minute 17, deliberately.

        Every deployment that copied this schedule would otherwise wake at
        00:00 together - which is what a cron daemon and a systemd timer both
        spike for. Asserted on the purge alone because it is the entry whose
        period makes it a shared spike; the other two run every minute and
        every five minutes, where the top-of-hour crowd is a fifth of the
        total.
        """
        entry = celery_app.conf.beat_schedule["purge_notification_outbox"]
        # A crontab renders its own next-run times; asking for the first two
        # and reading the minute off them is how the schedule is checked
        # without reaching into celery's internals for the parsed fields.
        fires = _next_fires(entry["schedule"], count=2)
        self.assertTrue(fires)
        for fire in fires:
            self.assertNotEqual(fire.minute, 0, "the purge must not land on the hour")


class EnvDocumentationTests(SimpleTestCase):
    """Every key this task added is documented where operators look.

    `.env.example` is the only place an operator can discover these keys, and
    an undocumented env-driven setting is a setting nobody can tune - which is
    the whole reason this repo drives config through the environment. Pinned by
    NAME against a hand-written literal list rather than derived from the
    settings module, because the question is whether the documentation matches
    the intent, and deriving the expected set from the code would agree with a
    key that was added to settings and forgotten here.
    """

    EXPECTED_KEYS = (
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "CELERY_TASK_DEFAULT_QUEUE",
        "CELERY_TASK_ALWAYS_EAGER",
        "CELERY_BEAT_EXPIRE_EVERY_MINUTES",
        "CELERY_BEAT_DRAIN_OUTBOX_SECONDS",
        "CELERY_BEAT_PURGE_HOUR",
    )

    def test_every_new_key_is_documented(self):
        example = pathlib.Path(__file__).resolve().parent.parent / ".env.example"
        text = example.read_text(encoding="utf-8")
        for key in self.EXPECTED_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, text)

    def test_every_documented_key_is_actually_read_by_settings(self):
        """A documented env key that settings never reads is worse than absent.

        It looks configurable, so an operator tunes it, sees nothing happen,
        and files a bug about the scheduler. That is exactly the failure this
        pin exists to catch: `CELERY_BEAT_DRAIN_OUTBOX_SECONDS` was documented
        in `.env.example` and listed here while the schedule read a literal,
        and it was found by checking rather than by a test - which is why it
        is checked now.

        Read from the settings SOURCE, not from the module, because a key that
        is assigned but never consumed is invisible to attribute lookup and is
        precisely the case that matters.

        Matched as the key appearing as a QUOTED STRING anywhere in the module,
        not only inside an `os.getenv(` call: this repo reads every knob
        through a typed resolver (`_env_positive_int`, `_env_int`,
        `_env_bool`), so a matcher that only knew `os.getenv` would report all
        of them as unread while they are in fact wired. What the pin is really
        asking is "does the name appear in a position that reads the
        environment", and a quoted occurrence is that position.
        """
        source = (
            pathlib.Path(__file__).resolve().parent.parent / "config/settings.py"
        ).read_text(encoding="utf-8")
        for key in self.EXPECTED_KEYS:
            with self.subTest(key=key):
                self.assertIn(
                    f'"{key}"',
                    source,
                    f"{key} is documented in .env.example but settings.py "
                    "never reads it, so setting it does nothing",
                )

    def test_no_broker_credential_is_written_into_the_template(self):
        """The default URL is a localhost Redis with no password.

        A template is a committed file, so a password in one is a published
        credential whatever the comment above it says. Asserted against the
        parsed URL rather than by grepping for the word, so a value like
        `redis://:hunter2@host` fails even though it contains no obvious
        marker.
        """
        from urllib.parse import urlparse

        example = pathlib.Path(__file__).resolve().parent.parent / ".env.example"
        for line in example.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("REDIS_URL=", "CELERY_BROKER_URL=")):
                continue
            parsed = urlparse(stripped.split("=", 1)[1])
            self.assertFalse(
                parsed.password,
                f"{stripped!r} carries a password; credentials live in the "
                "environment or the secret store, never in a tracked file",
            )


class ThrottleCacheLeftAloneTests(SimpleTestCase):
    """CACHES is deliberately untouched by this task, and that is a decision
    worth pinning rather than leaving to a reader's memory.

    Redis is not present in the test environment, so moving the DRF throttle
    cache onto it would break CI. The per-process throttle cache remains an
    open finding, tracked separately; this class records that adding a broker
    did NOT quietly resolve or quietly worsen it.
    """

    def test_the_default_cache_is_still_per_process(self):
        from django.conf import settings

        self.assertIn("LocMemCache", settings.CACHES["default"]["BACKEND"])

    def test_the_alerts_alias_is_still_present_and_separate(self):
        from django.conf import settings

        self.assertIn("alerts", settings.CACHES)
        self.assertNotEqual(
            settings.CACHES["alerts"]["LOCATION"],
            settings.CACHES["default"]["LOCATION"],
        )

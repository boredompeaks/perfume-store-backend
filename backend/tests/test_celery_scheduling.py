import pathlib
from datetime import UTC, datetime, timedelta
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
    """A real order: a queue row whose order cannot be resolved is dead-lettered by the claim predicate."""
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


EXPECTED_SWEEPS = (
    "expire_reservations",
    "drain_notification_outbox",
    "purge_notification_outbox",
)


class BeatScheduleShapeTests(SimpleTestCase):
    def test_the_schedule_schedules_exactly_the_three_sweeps(self):
        schedule = celery_app.conf.beat_schedule
        self.assertEqual(
            set(schedule),
            set(EXPECTED_SWEEPS),
            "the beat schedule must schedule exactly the three sweeps this "
            "task exists to schedule, keyed by management-command name",
        )

    def test_every_entry_runs_the_task_named_after_its_command(self):
        for command in EXPECTED_SWEEPS:
            with self.subTest(command=command):
                self.assertEqual(
                    celery_app.conf.beat_schedule[command]["task"],
                    f"common.tasks.{command}",
                )

    def test_every_scheduled_task_is_actually_registered(self):
        registered = celery_app.tasks
        for command in EXPECTED_SWEEPS:
            with self.subTest(command=command):
                dotted = celery_app.conf.beat_schedule[command]["task"]
                self.assertIn(dotted, registered)
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
        ours = {name for name in celery_app.tasks if name.startswith("common.tasks.")}
        self.assertEqual(
            ours,
            {f"common.tasks.{command}" for command in EXPECTED_SWEEPS},
        )


# Cadence claims are evaluated at frozen probe instants, never the wall clock: the wait to the next fire legitimately drops below an hour before the daily purge.
CADENCE_PROBE_INSTANTS = (
    datetime(2026, 6, 1, 2, 30, 0, tzinfo=UTC),
    datetime(2026, 6, 1, 12, 45, 7, tzinfo=UTC),
)


def _frozen_remaining(schedule, when):
    """Celery reads two clocks for one estimate (the last_run_at argument and the schedule's nowfun); both are pinned to one instant or the difference is garbage."""
    with mock.patch.object(schedule, "nowfun", return_value=when):
        return schedule.remaining_estimate(when).total_seconds()


def _next_fires(schedule, now, count=3):
    fires = []
    cursor = now
    for _ in range(count):
        cursor = cursor + timedelta(seconds=_frozen_remaining(schedule, cursor))
        fires.append(cursor)
    return fires


class BrokerConfigTests(SimpleTestCase):
    def test_the_broker_and_result_backend_default_to_redis(self):
        self.assertTrue(celery_app.conf.broker_url.startswith("redis://"))
        self.assertTrue(celery_app.conf.result_backend.startswith("redis://"))

    def test_redis_url_is_the_single_default_for_both(self):
        from django.conf import settings

        self.assertEqual(settings.CELERY_BROKER_URL, settings.REDIS_URL)
        self.assertEqual(settings.CELERY_RESULT_BACKEND, settings.REDIS_URL)

    def test_the_broker_and_backend_can_be_split(self):
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
        self.assertIn("REDIS redis://shared.example.test:6379/0", res.stdout)

    def test_serializers_are_json_on_both_legs(self):
        self.assertEqual(celery_app.conf.task_serializer, "json")
        self.assertEqual(celery_app.conf.result_serializer, "json")
        self.assertEqual(list(celery_app.conf.accept_content), ["json"])

    def test_importing_the_app_opened_no_broker_connection(self):
        from tests.test_settings_security import _NON_DEBUG_ENV, run_settings_import

        snippet = (
            "import config;"
            "app = config.celery_app;"
            "print('URL', app.conf.broker_url);"
            "app.loader.import_default_modules();"
            "print('TASKS', sorted(n for n in app.tasks if n.startswith('common.')))"
        )
        res = run_settings_import(dict(_NON_DEBUG_ENV), snippet=snippet)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("URL redis://", res.stdout)
        for command in EXPECTED_SWEEPS:
            self.assertIn(f"common.tasks.{command}", res.stdout)


class TaskInvokesCommandTests(SimpleTestCase):
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
        for command in EXPECTED_SWEEPS:
            with self.subTest(command=command):
                with mock.patch.object(
                    tasks, "call_command", autospec=True
                ) as call_command_mock:
                    getattr(tasks, command)()
                self.assertEqual(call_command_mock.call_args.kwargs.get("verbosity"), 0)

    def test_a_command_failure_propagates_as_a_task_failure(self):
        with mock.patch.object(
            tasks, "call_command", side_effect=RuntimeError("sweep blew up")
        ):
            with self.assertRaises(RuntimeError):
                tasks.drain_notification_outbox()


class EagerDispatchTests(SimpleTestCase):
    """The result backend must be repointed through override_settings: the app's conf is a live view of Django settings, so assigning to it directly is overwritten."""

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
        from django.conf import settings

        self.assertFalse(settings.CELERY_TASK_ALWAYS_EAGER)


class ScheduledSweepEffectTests(ApiTestCase):
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
        row = self._queue_paid_notification()

        tasks.purge_notification_outbox()

        self.assertTrue(NotificationOutbox.objects.filter(pk=row.pk).exists())

    def test_every_scheduled_task_is_idempotent_on_a_second_tick(self):
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

        out = StringIO()
        with mock.patch("sys.stdout", out):
            tasks.expire_reservations()

        hold = order.stock_reservations.get()
        self.assertEqual(hold.status, StockReservation.Status.EXPIRED)
        movement = StockMovement.objects.get()
        self.assertEqual(movement.product, product)
        self.assertEqual(movement.delta, 0)
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
    def test_enqueue_production_call_sites_are_exactly_the_converted_three(self):
        self.assertEqual(
            self._production_callers_of_enqueue(),
            ["orders/events.py", "orders/views/payment.py", "orders/webhooks.py"],
            "the converted notification call sites are exactly the lifecycle "
            "seam, the browser verify and the gateway hook",
        )

    def _production_callers_of_enqueue(self):
        import re

        backend_dir = pathlib.Path(__file__).resolve().parent.parent
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
        self.make_user("buyer")
        self.api_login(username="buyer")
        product = self.make_product(stock=5)
        self.seed_session_cart([(product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        # The mocked gateway mints order_TEST0001, so the verify payload must
        # present that same id or the seam refuses it as a different order.
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
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        out = StringIO()
        call_command("drain_notification_outbox", stdout=out)
        self.assertIn("drain_notification_outbox", out.getvalue())
        self.assertEqual(len(mail.outbox), 1)


class CadenceTests(SimpleTestCase):
    """`is_due` answers True unconditionally with no last_run_at, so the cadence probe uses remaining_estimate."""

    def test_the_drain_fires_on_its_configured_cadence(self):
        from django.conf import settings

        entry = celery_app.conf.beat_schedule["drain_notification_outbox"]
        interval = entry["schedule"]
        configured = settings.CELERY_BEAT_DRAIN_OUTBOX_SECONDS
        for when in CADENCE_PROBE_INSTANTS:
            with self.subTest(when=when):
                remaining = _frozen_remaining(interval, when)
                self.assertLessEqual(
                    remaining,
                    configured,
                    "the drain must fire within its configured interval",
                )
                self.assertGreater(remaining, 0)

    def test_the_reservation_sweep_fires_well_inside_the_reservation_ttl(self):
        from django.conf import settings

        entry = celery_app.conf.beat_schedule["expire_reservations"]
        for when in CADENCE_PROBE_INSTANTS:
            with self.subTest(when=when):
                remaining = _frozen_remaining(entry["schedule"], when)
                self.assertLess(remaining, settings.RESERVATION_TTL)

    def test_the_purge_is_a_daily_sweep_not_a_sub_daily_one(self):
        from django.conf import settings

        entry = celery_app.conf.beat_schedule["purge_notification_outbox"]
        for when in CADENCE_PROBE_INSTANTS:
            with self.subTest(when=when):
                to_fire = _frozen_remaining(entry["schedule"], when)
                self.assertGreater(to_fire, 0, "a fire must always be ahead")
                period = _frozen_remaining(
                    entry["schedule"], when + timedelta(seconds=to_fire)
                )
                self.assertGreater(period, 3600, "the purge must not be sub-daily")
                self.assertLess(
                    period,
                    settings.NOTIFICATION_OUTBOX_TTL_SECONDS,
                    "the purge must fire INSIDE the TTL it enforces, or spent rows "
                    "outlive the bound that is meant to delete them",
                )

    def test_no_sweep_is_scheduled_off_the_top_of_the_hour(self):
        entry = celery_app.conf.beat_schedule["purge_notification_outbox"]
        for when in CADENCE_PROBE_INSTANTS:
            with self.subTest(when=when):
                fires = _next_fires(entry["schedule"], when, count=2)
                self.assertTrue(fires)
                for fire in fires:
                    self.assertNotEqual(
                        fire.minute, 0, "the purge must not land on the hour"
                    )


class EnvDocumentationTests(SimpleTestCase):
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


class ThrottleCacheSwitchTests(SimpleTestCase):
    _PRINT = (
        "import config.settings as s; print('BACKEND', s.CACHES['default']['BACKEND'])"
    )

    def test_the_default_cache_stays_per_process_without_redis_url(self):
        from tests.test_settings_security import _DEV_ENV, run_settings_import

        res = run_settings_import(dict(_DEV_ENV), snippet=self._PRINT)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            "BACKEND django.core.cache.backends.locmem.LocMemCache", res.stdout
        )

    def test_the_default_cache_is_shared_redis_with_redis_url(self):
        from tests.test_settings_security import _DEV_ENV, run_settings_import

        res = run_settings_import(
            {**_DEV_ENV, "REDIS_URL": "redis://127.0.0.1:6390/14"},
            snippet=self._PRINT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("BACKEND django.core.cache.backends.redis.RedisCache", res.stdout)

    def test_the_alerts_alias_is_still_present_and_separate(self):
        from django.conf import settings

        self.assertIn("alerts", settings.CACHES)
        self.assertNotEqual(
            settings.CACHES["alerts"]["LOCATION"],
            settings.CACHES["default"]["LOCATION"],
        )

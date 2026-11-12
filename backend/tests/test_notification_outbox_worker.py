import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import caches
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    OperationalError,
    close_old_connections,
    connection,
    transaction,
)
from django.template import TemplateDoesNotExist
from django.test import (
    TransactionTestCase,
    override_settings,
    tag,
)
from django.utils import timezone

from common import notifications
from common.models import AuditEvent, NotificationOutbox
from common.notifications import UnresolvableNotification
from common.testing import ApiTestCase
from orders.models import Order

ALERT_RECIPIENT = "outbox-oncall@example.com"

UNRESOLVABLE_PAYLOADS = {
    "deleted reference": (
        lambda order: {"order": {"label": "orders.order", "pk": order.pk}},
        "is gone",
    ),
    "unregistered label": (
        lambda order: {"order": {"label": "nope.nope", "pk": 1}},
        "no model registered",
    ),
    "label the registry cannot parse": (
        lambda order: {"order": {"label": "a.b.c", "pk": 1}},
        "no model registered",
    ),
    "unaddressable primary key": (
        lambda order: {"order": {"label": "orders.order", "pk": "not-a-pk"}},
        "is gone",
    ),
}


def past():
    return timezone.now() - timedelta(minutes=5)


def future():
    return timezone.now() + timedelta(minutes=5)


def make_order(user=None, total="499.99", **overrides):
    fields = dict(
        user=user,
        full_name="Drain Buyer",
        phone="9876543210",
        address="12 Rose Lane",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        total_amount=Decimal(total),
    )
    fields.update(overrides)
    return Order.objects.create(**fields)


@tag("notifications")
class OutboxDrainTestCase(ApiTestCase):
    def setUp(self):
        self.user = self.make_user("drain")
        self.order = make_order(self.user)

    def queue(self, order=None, **overrides):
        row = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": order if order is not None else self.order},
        )
        if overrides:
            NotificationOutbox.objects.filter(pk=row.pk).update(**overrides)
            row.refresh_from_db()
        return row

    def queue_many(self, count, **overrides):
        rows = []
        for index in range(count):
            buyer = self.make_user(f"drain{index}")
            rows.append(self.queue(order=make_order(buyer), **overrides))
        return rows

    def queue_expired(self, **overrides):
        fields = {"expires_at": past()}
        fields.update(overrides)
        return self.queue(**fields)

    def assert_dead(self, row, needle):
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.DEAD)
        self.assertIn(needle, row.last_error)


@tag("notifications")
class OutboxDrainDeliveryTests(OutboxDrainTestCase):
    def test_a_queued_row_is_sent_and_stamped(self):
        row = self.queue()
        result = notifications.drain_notifications()
        self.assertEqual(result.examined, 1)
        self.assertEqual(result.sent, 1)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.SENT)
        self.assertIsNotNone(row.sent_at)
        self.assertEqual(row.last_error, "")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])

    def test_the_email_carries_live_state_not_enqueue_time_state(self):
        row = self.queue()
        Order.objects.filter(pk=row.payload["order"]["pk"]).update(
            total_amount=Decimal("750.25")
        )
        notifications.drain_notifications()
        self.assertIn("750.25", mail.outbox[-1].body)
        self.assertIn(str(self.order.pk), mail.outbox[-1].subject)

    def test_an_empty_table_sends_nothing_and_costs_one_query(self):
        result = notifications.drain_notifications()
        self.assertEqual(result.examined, 0)
        self.assertEqual(len(mail.outbox), 0)
        with self.assertNumQueries(1):
            self.assertIsNone(notifications.claim_next_notification())

    def test_a_batch_is_drained_oldest_first_and_stops_at_its_limit(self):
        rows = self.queue_many(3)
        result = notifications.drain_notifications(batch_size=2)
        self.assertEqual(result.examined, 2)
        self.assertEqual(result.sent, 2)
        statuses = list(
            NotificationOutbox.objects.order_by("created_at", "pk").values_list(
                "status", flat=True
            )
        )
        self.assertEqual(statuses, ["sent", "sent", "pending"])
        drained = set(
            NotificationOutbox.objects.filter(status="sent").values_list(
                "pk", flat=True
            )
        )
        self.assertEqual(drained, {rows[0].pk, rows[1].pk})

    def test_a_row_another_worker_took_first_is_skipped_not_waited_on(self):
        from django.db.models import QuerySet

        loser = self.queue(order=make_order(self.make_user("loser")))
        winner = self.queue(order=make_order(self.make_user("winner")))
        real_lock = QuerySet.select_for_update

        def reserve_the_first_row_first(queryset, *args, **kwargs):
            # Another worker claims the older row between our peek and our lock; this runs BEFORE the lock so it cannot deadlock.
            NotificationOutbox.objects.filter(pk=loser.pk).update(
                status=NotificationOutbox.Status.DEAD
            )
            return real_lock(queryset, *args, **kwargs)

        with mock.patch.object(
            QuerySet, "select_for_update", reserve_the_first_row_first
        ):
            claimed = notifications.claim_next_notification()

        self.assertEqual(claimed, winner.pk)
        loser.refresh_from_db()
        self.assertEqual(loser.attempts, 0, "the row was claimed after being refused")

    def test_the_second_pass_drains_what_the_first_left(self):
        self.queue_many(3)
        notifications.drain_notifications(batch_size=2)
        result = notifications.drain_notifications()
        self.assertEqual(result.sent, 1)
        self.assertEqual(NotificationOutbox.objects.filter(status="pending").count(), 0)


@tag("notifications")
class OutboxBackoffTests(OutboxDrainTestCase):
    def test_a_failed_send_is_recorded_and_not_immediately_retried(self):
        row = self.queue()
        with mock.patch.object(
            notifications, "send_email", side_effect=OSError("provider down")
        ):
            result = notifications.drain_notifications()
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.dead, 0)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.FAILED)
        self.assertEqual(row.attempts, 1)
        self.assertIn("OSError: provider down", row.last_error)
        self.assertGreater(row.next_attempt_at, timezone.now())

    def test_a_row_whose_next_attempt_is_in_the_future_is_not_claimed(self):
        row = self.queue()
        with mock.patch.object(
            notifications, "send_email", side_effect=OSError("provider down")
        ):
            notifications.drain_notifications()
        row.refresh_from_db()
        self.assertGreater(row.next_attempt_at, timezone.now())

        with mock.patch.object(notifications, "send_email") as send:
            self.assertIsNone(notifications.claim_next_notification())
            result = notifications.drain_notifications()
        send.assert_not_called()
        self.assertEqual(result.examined, 0)
        row.refresh_from_db()
        self.assertEqual(row.attempts, 1)

    def test_a_failed_row_is_claimed_again_once_its_backoff_lapses(self):
        row = self.queue()
        with mock.patch.object(
            notifications, "send_email", side_effect=OSError("provider down")
        ):
            notifications.drain_notifications()
        NotificationOutbox.objects.filter(pk=row.pk).update(next_attempt_at=past())
        result = notifications.drain_notifications()
        self.assertEqual(result.sent, 1)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.SENT)
        self.assertEqual(row.attempts, 2)

    @override_settings(
        NOTIFICATION_OUTBOX_RETRY_BASE_SECONDS=7,
        NOTIFICATION_OUTBOX_RETRY_MAX_SECONDS=9,
    )
    def test_the_backoff_is_env_driven_and_capped(self):
        self.assertEqual(notifications.retry_delay_seconds(1), 7)
        self.assertEqual(notifications.retry_delay_seconds(2), 9)
        self.assertEqual(notifications.retry_delay_seconds(9), 9)

    @override_settings(
        NOTIFICATION_OUTBOX_RETRY_BASE_SECONDS=5,
        NOTIFICATION_OUTBOX_RETRY_MAX_SECONDS=10000,
    )
    def test_the_backoff_grows_exponentially_between_the_bounds(self):
        self.assertEqual(notifications.retry_delay_seconds(1), 5)
        self.assertEqual(notifications.retry_delay_seconds(2), 10)
        self.assertEqual(notifications.retry_delay_seconds(3), 20)
        self.assertEqual(notifications.retry_delay_seconds(4), 40)

    def test_an_absurd_attempt_count_cannot_build_an_absurd_backoff(self):
        # Literal defaults (base 30s, cap 3600s): comparing the function to itself would prove only that it is deterministic.
        self.assertEqual(notifications.retry_delay_seconds(1), 30)
        self.assertEqual(notifications.retry_delay_seconds(2), 60)
        self.assertEqual(notifications.retry_delay_seconds(10**9), 3600)
        self.assertEqual(
            notifications.retry_delay_seconds(notifications._BACKOFF_EXPONENT_CAP + 1),
            3600,
        )

    def test_a_row_past_its_expiry_is_never_sent(self):
        row = self.queue_expired()
        self.assertEqual(
            list(notifications.claimable_notifications().values_list("pk", flat=True)),
            [],
            "the claim predicate still admits an expired row",
        )
        result = notifications.drain_notifications()
        self.assertEqual(result.examined, 0)
        self.assertEqual(len(mail.outbox), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.PENDING)


@tag("notifications")
class OutboxDeadLetterTests(OutboxDrainTestCase):
    @override_settings(NOTIFICATION_OUTBOX_MAX_ATTEMPTS=3)
    def test_a_row_that_keeps_failing_reaches_a_terminal_state_and_stops(self):
        row = self.queue()
        with mock.patch.object(
            notifications, "send_email", side_effect=OSError("refused")
        ):
            for _ in range(6):
                row.refresh_from_db()
                NotificationOutbox.objects.filter(pk=row.pk).update(
                    next_attempt_at=past()
                )
                notifications.drain_notifications()

        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.DEAD)
        # Exactly the configured number of attempts, not "at most six": a loop
        # retrying forever would also end DEAD here, and only the count separates them.
        self.assertEqual(row.attempts, 3)
        self.assertIsNone(notifications.claim_next_notification(now=future()))

    def test_a_dead_row_is_not_claimed_even_long_after_it_failed(self):
        row = self.queue()
        NotificationOutbox.objects.filter(pk=row.pk).update(
            status=NotificationOutbox.Status.DEAD,
            attempts=99,
            next_attempt_at=past(),
        )
        self.assertIsNone(notifications.claim_next_notification())
        result = notifications.drain_notifications()
        self.assertEqual(result.examined, 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_each_unresolvable_payload_is_dead_lettered_on_its_first_attempt(self):
        for name, (build_payload, needle) in UNRESOLVABLE_PAYLOADS.items():
            with self.subTest(payload=name):
                order = make_order(self.make_user(f"poison{name[:6]}"))
                row = self.queue(order=order)
                if name == "deleted reference":
                    order.delete()
                NotificationOutbox.objects.filter(pk=row.pk).update(
                    payload=build_payload(order)
                )
                result = notifications.drain_notifications()
                self.assertEqual(result.dead, 1, msg=name)
                self.assertEqual(result.failed, 0, msg=name)
                self.assertEqual(len(mail.outbox), 0, msg=name)
                self.assert_dead(row, needle)

    def test_a_row_whose_handler_is_gone_is_dead_lettered_not_retried(self):
        row = self.queue()
        NotificationOutbox.objects.filter(pk=row.pk).update(
            event_type="order.never_registered",
            status=NotificationOutbox.Status.PENDING,
        )
        result = notifications.drain_notifications()
        self.assertEqual(result.dead, 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assert_dead(row, "no notification registered")

    def test_an_unfixable_row_in_the_middle_does_not_end_the_batch(self):
        good_before = self.queue(order=make_order(self.make_user("before")))
        poison = self.queue(order=make_order(self.make_user("poison")))
        good_after = self.queue(order=make_order(self.make_user("after")))
        NotificationOutbox.objects.filter(pk=poison.pk).update(
            payload={"order": {"label": "orders.order", "pk": 999999}}
        )

        result = notifications.drain_notifications()

        self.assertEqual(result.examined, 3)
        self.assertEqual(result.sent, 2)
        self.assertEqual(result.dead, 1)
        self.assertEqual(len(mail.outbox), 2)
        self.assert_dead(poison, "is gone")
        self.assertEqual(
            NotificationOutbox.objects.filter(pk=good_before.pk).values_list(
                "status", flat=True
            )[0],
            NotificationOutbox.Status.SENT,
        )
        self.assertEqual(
            NotificationOutbox.objects.filter(pk=good_after.pk).values_list(
                "status", flat=True
            )[0],
            NotificationOutbox.Status.SENT,
        )

    def test_a_row_purged_between_the_claim_and_the_stamp_is_not_reported_sent(self):
        self.queue()
        with mock.patch.object(notifications, "resolve_context", _purge_then({})):
            result = notifications.drain_notifications()
        self.assertEqual(result.vanished, 1)
        self.assertEqual(result.sent, 0)
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    def test_a_row_purged_between_the_claim_and_a_failure_is_not_dead_lettered(self):
        self.queue()
        with mock.patch.object(
            notifications, "resolve_context", _purge_then({"order": self.order})
        ):
            with mock.patch.object(
                notifications, "send_email", side_effect=OSError("provider down")
            ):
                result = notifications.drain_notifications()
        self.assertEqual(result.vanished, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.dead, 0)

    def test_a_row_purged_before_the_worker_reads_it_is_reported_vanished(self):
        row = self.queue()
        NotificationOutbox.objects.all().delete()
        with mock.patch.object(
            notifications, "claim_next_notification", return_value=row.pk
        ):
            result = notifications.drain_notifications(batch_size=1)
        self.assertEqual(result.vanished, 1)
        self.assertEqual(result.examined, 1)

    def test_a_row_purged_before_a_dead_letter_is_not_reported_dead(self):
        row = self.queue()
        NotificationOutbox.objects.filter(pk=row.pk).update(
            payload={"order": {"label": "orders.order", "pk": 999999}}
        )
        with mock.patch.object(
            notifications,
            "resolve_context",
            _purge_then(raise_it=UnresolvableNotification("orders.order pk=999999")),
        ):
            result = notifications.drain_notifications()
        self.assertEqual(result.vanished, 1)
        self.assertEqual(result.dead, 0)


def _purge_then(context=None, raise_it=None):
    def _resolve(payload):
        NotificationOutbox.objects.all().delete()
        if raise_it is not None:
            raise raise_it
        return context

    return _resolve


@tag("notifications")
class OutboxStatusVocabularyTests(OutboxDrainTestCase):
    def test_the_vocabulary_is_exactly_these_four_values(self):
        self.assertEqual(
            list(NotificationOutbox.Status.values),
            ["pending", "sent", "failed", "dead"],
        )
        self.assertEqual(
            sorted(outcome.value for outcome in notifications.DrainOutcome),
            ["dead", "failed", "sent", "vanished"],
        )
        self.assertEqual(
            notifications.DrainResult._fields,
            ("sent", "failed", "dead", "vanished", "examined"),
        )
        self.assertEqual(
            tuple(outcome.value for outcome in notifications.DrainOutcome)
            + ("examined",),
            notifications.DrainResult._fields,
        )

    def test_every_status_is_reachable_and_each_says_whether_it_is_claimable(self):
        expected_claimable = {
            "pending": True,
            "sent": False,
            "failed": True,
            "dead": False,
        }
        for status in NotificationOutbox.Status.values:
            with self.subTest(status=status):
                order = make_order(self.make_user(f"status{status}"))
                row = self.queue(order=order)
                NotificationOutbox.objects.filter(pk=row.pk).update(
                    status=status, next_attempt_at=past()
                )
                claimed = notifications.claim_next_notification()
                self.assertEqual(
                    claimed is not None,
                    expected_claimable[status],
                    f"{status} claimability",
                )

    def test_the_status_report_counts_every_value_including_the_empty_ones(self):
        self.assertEqual(
            notifications.outbox_status_counts(),
            {"pending": 0, "sent": 0, "failed": 0, "dead": 0},
        )
        self.queue()
        NotificationOutbox.objects.update(status=NotificationOutbox.Status.DEAD)
        self.assertEqual(
            notifications.outbox_status_counts(),
            {"pending": 0, "sent": 0, "failed": 0, "dead": 1},
        )


@tag("notifications")
class OutboxAtLeastOnceTests(OutboxDrainTestCase):
    def test_a_worker_that_dies_after_sending_loses_the_row_from_the_next_pass(self):
        row = self.queue()
        with mock.patch.object(
            notifications, "_record_success", side_effect=SystemExit("killed")
        ):
            with self.assertRaises(SystemExit):
                notifications.drain_notifications()
        self.assertEqual(len(mail.outbox), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.PENDING)
        self.assertEqual(row.attempts, 1)
        self.assertIsNone(row.sent_at)

    def test_that_lost_row_is_sent_again_once_its_lease_lapses(self):
        row = self.queue()
        with mock.patch.object(
            notifications, "_record_success", side_effect=SystemExit("killed")
        ):
            with self.assertRaises(SystemExit):
                notifications.drain_notifications()

        self.assertIsNone(notifications.claim_next_notification())
        self.assertEqual(len(mail.outbox), 1)

        NotificationOutbox.objects.filter(pk=row.pk).update(next_attempt_at=past())
        result = notifications.drain_notifications()
        self.assertEqual(result.sent, 1)
        self.assertEqual(len(mail.outbox), 2)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.SENT)
        self.assertEqual(row.attempts, 2)

    @override_settings(NOTIFICATION_OUTBOX_LEASE_SECONDS=600)
    def test_the_lease_length_is_env_driven(self):
        self.queue()
        with mock.patch.object(
            notifications, "_record_success", side_effect=SystemExit("killed")
        ):
            with self.assertRaises(SystemExit):
                notifications.drain_notifications()
        row = NotificationOutbox.objects.get()
        self.assertGreater(row.next_attempt_at, timezone.now() + timedelta(minutes=9))


@tag("notifications", "e2e")
class OutboxClaimLockShapeTests(ApiTestCase):
    def test_the_claim_asks_the_orm_to_lock_the_row_on_every_engine(self):
        from django.db.models import QuerySet

        locked = set()
        original = QuerySet.select_for_update

        def spy(queryset, *args, **kwargs):
            locked.add(queryset.model)
            return original(queryset, *args, **kwargs)

        row = NotificationOutbox.objects.create(
            event_type="order.paid",
            dedup_key="lock-shape",
            payload={},
        )
        with mock.patch.object(QuerySet, "select_for_update", spy):
            notifications.drain_notifications()
        self.assertIn(NotificationOutbox, locked)
        self.assertEqual(
            NotificationOutbox.objects.filter(pk=row.pk).values_list(
                "status", flat=True
            )[0],
            NotificationOutbox.Status.SENT,
        )

    def test_the_locked_statement_joins_nothing(self):
        """NotificationOutbox carries no foreign key, so the claim cannot produce the LEFT OUTER JOIN ... FOR UPDATE PostgreSQL rejects."""
        row = NotificationOutbox.objects.create(
            event_type="order.paid", dedup_key="lock-shape", payload={}
        )
        with transaction.atomic():
            sql, _params = (
                NotificationOutbox.objects.select_for_update()
                .filter(pk=row.pk)
                .query.get_compiler("default")
                .as_sql()
            )
        self.assertNotIn("JOIN", sql.upper())
        if connection.features.has_select_for_update:
            self.assertIn("FOR UPDATE", sql.upper())

    def test_the_claimable_queryset_joins_nothing_either(self):
        with transaction.atomic():
            sql, _params = (
                notifications.claimable_notifications()
                .query.get_compiler("default")
                .as_sql()
            )
        self.assertNotIn("JOIN", sql.upper())


@tag("notifications", "e2e")
class OutboxTwoWorkerTests(TransactionTestCase):
    reset_sequences = True

    def _make_row(self, username):
        # Users are made directly: this TransactionTestCase needs two separate
        # connections, and the factory lives on the TestCase base.
        user = get_user_model().objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="S3cure-Passphrase!",
        )
        order = make_order(user)
        return notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})

    def _race(self, barrier=False):
        """On SQLite a concurrent write raises OperationalError: database table is locked rather than blocking, so errors are returned, not swallowed."""
        claimed = []
        errors = []
        gate = threading.Barrier(2) if barrier else None

        def worker():
            close_old_connections()
            try:
                if gate is not None:
                    gate.wait()
                claimed.append(notifications.claim_next_notification())
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return claimed, errors

    def _assert_engine_wrote_the_expected_way(self, errors):
        """On an engine with real row locks both workers must get a clean answer; on one without, a lock refusal is the documented consequence."""
        if connection.features.has_select_for_update:
            self.assertEqual(errors, [], "a worker failed on an engine that locks rows")
            return
        for error in errors:
            self.assertIsInstance(error, OperationalError)
            self.assertIn("lock", str(error).lower())

    def test_two_workers_racing_one_row_claim_it_exactly_once(self):
        """The barrier puts both workers in the same window; the assertion is on the row's own attempts count, so it holds whichever worker won and on both engines."""
        row = self._make_row("two-worker")
        _claimed, errors = self._race(barrier=True)
        self._assert_engine_wrote_the_expected_way(errors)

        row.refresh_from_db()
        self.assertLessEqual(
            row.attempts,
            1,
            "two workers claimed the same notification",
        )
        if connection.features.has_select_for_update:
            self.assertEqual(
                row.attempts,
                1,
                "neither worker claimed the row on an engine with row locks",
            )
        # Engine-gated like the exact count: an engine that serialises writes may refuse BOTH claims, so None is asserted only where the row was claimed.
        follow_up = notifications.claim_next_notification()
        if row.attempts:
            self.assertIsNone(
                follow_up,
                "a claimed row stayed claimable: the lease did not take effect",
            )
        else:
            self.assertEqual(
                follow_up,
                row.pk,
                "an unclaimed row must still be claimable, by this or another worker",
            )

    def test_two_workers_racing_two_rows_still_send_each_one_exactly_once(self):
        first = self._make_row("two-worker-a")
        second = self._make_row("two-worker-b")
        _claimed, errors = self._race(barrier=True)
        self._assert_engine_wrote_the_expected_way(errors)

        for row in (first, second):
            row.refresh_from_db()
            self.assertLessEqual(
                row.attempts,
                1,
                "a row was claimed twice: two workers sent the same notification",
            )
        if connection.features.has_select_for_update:
            for row in (first, second):
                row.refresh_from_db()
                self.assertEqual(row.attempts, 1)

        NotificationOutbox.objects.update(next_attempt_at=past())
        notifications.drain_notifications()
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(NotificationOutbox.objects.filter(status="sent").count(), 2)

    def test_the_unguarded_follow_up_assertion_is_what_used_to_flake(self):
        """Deterministic pin for BUG-6: SQLite may refuse BOTH racing writes, so an unclaimed row is still claimable and claim_next_notification correctly returns its pk."""
        row = self._make_row("follow-up-guard")

        self.assertEqual(
            notifications.claim_next_notification(),
            row.pk,
            "an untouched row must be claimable",
        )

        self.assertIsNone(
            notifications.claim_next_notification(),
            "the lease must put a claimed row out of reach",
        )
        row.refresh_from_db()
        self.assertEqual(row.attempts, 1)

    def test_one_worker_drains_a_row_two_workers_must_share(self):
        self._make_row("two-worker-drain")
        notifications.drain_notifications()
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        self.assertEqual(
            NotificationOutbox.objects.values_list("status", flat=True)[0],
            NotificationOutbox.Status.SENT,
        )


@tag("notifications")
class CallSiteConvertedByWorkerTests(ApiTestCase):
    def setUp(self):
        self.user = self.make_user("livepath")
        _, self.token = self.api_login(username=self.user.username)
        self.product = self.make_product(stock=5)

    def test_verify_payment_queues_and_only_the_worker_sends(self):
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        self.razorpay_mock()
        self.client.post(
            "/api/orders/payment/", {"order_id": res.data["id"]}, format="json"
        )
        res = self.client.post(
            "/api/orders/payment/verify/",
            {
                "razorpay_order_id": "order_TEST0001",
                "razorpay_payment_id": "pay_TEST0001",
                "razorpay_signature": "sig",
                "order_id": res.data["id"],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        notifications.drain_notifications()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])

    def test_draining_an_empty_outbox_sends_nothing(self):
        result = notifications.drain_notifications()
        self.assertEqual(result.examined, 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_purge_command_is_still_the_deletion_owner(self):
        row = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": make_order(self.user)},
            occurrence="purge-ownership-probe",
        )
        NotificationOutbox.objects.filter(pk=row.pk).update(
            expires_at=past(), payload={}
        )
        notifications.drain_notifications()
        self.assertTrue(NotificationOutbox.objects.filter(pk=row.pk).exists())


@tag("notifications")
class DrainCommandTests(OutboxDrainTestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("drain_notification_outbox", *args, stdout=out)
        return out.getvalue()

    def test_the_command_drains_and_reports_what_it_did(self):
        self.queue()
        output = self._run()
        self.assertIn("examined 1", output)
        self.assertIn("sent 1", output)
        self.assertIn("pending 0", output)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_command_is_quiet_at_verbosity_zero_for_a_cron(self):
        out = StringIO()
        call_command("drain_notification_outbox", verbosity=0, stdout=out)
        self.assertEqual(out.getvalue(), "")

    def test_status_only_reports_without_sending(self):
        self.queue()
        output = self._run("--status-only")
        self.assertIn("--status-only", output)
        self.assertIn("pending 1", output)
        self.assertEqual(len(mail.outbox), 0)

    def test_retry_dead_re_opens_a_dead_row_and_this_run_sends_it(self):
        row = self.queue()
        NotificationOutbox.objects.filter(pk=row.pk).update(
            status=NotificationOutbox.Status.DEAD,
            attempts=9,
            last_error="OSError: gone",
        )
        output = self._run("--retry-dead")
        self.assertIn("re-opened 1 dead row", output)
        self.assertIn("sent 1", output)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.SENT)
        self.assertEqual(row.attempts, 10)
        self.assertEqual(row.last_error, "")

    @override_settings(NOTIFICATION_OUTBOX_MAX_ATTEMPTS=3)
    def test_a_re_opened_row_that_fails_again_is_dead_lettered_not_retried(self):
        row = self.queue()
        NotificationOutbox.objects.filter(pk=row.pk).update(
            status=NotificationOutbox.Status.DEAD, attempts=3
        )
        with mock.patch.object(
            notifications, "send_email", side_effect=OSError("still down")
        ):
            self._run("--retry-dead")
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.DEAD)
        self.assertEqual(row.attempts, 4)

    def test_retry_dead_can_be_narrowed_to_one_event_type(self):
        wanted = self.queue(order=make_order(self.make_user("retrywanted")))
        other = self.queue(order=make_order(self.make_user("retryother")))
        NotificationOutbox.objects.filter(pk=other.pk).update(
            status=NotificationOutbox.Status.DEAD, attempts=1
        )
        NotificationOutbox.objects.filter(pk=wanted.pk).update(
            status=NotificationOutbox.Status.DEAD,
            attempts=1,
            event_type="order.never_registered",
        )
        output = self._run("--retry-dead", "--event-type", "order.never_registered")
        self.assertIn("re-opened 1 dead row", output)
        wanted.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(wanted.attempts, 2)
        self.assertEqual(other.attempts, 1)

    def test_purge_expired_is_opt_in(self):
        row = self.queue_expired()
        self._run()
        self.assertTrue(NotificationOutbox.objects.filter(pk=row.pk).exists())
        self._run("--purge-expired")
        self.assertFalse(NotificationOutbox.objects.filter(pk=row.pk).exists())

    def test_batch_size_is_honoured_from_the_command_line(self):
        self.queue_many(3)
        output = self._run("--batch-size", "2")
        self.assertIn("examined 2", output)
        self.assertIn("pending 1", output)

    @override_settings(NOTIFICATION_OUTBOX_DRAIN_BATCH_SIZE=7)
    def test_the_batch_size_default_comes_from_settings(self):
        self.queue_many(9)
        result = notifications.drain_notifications()
        self.assertEqual(result.examined, 7)
        self.assertEqual(result.sent, 7)


@tag("notifications")
class OutboxErrorTextTests(OutboxDrainTestCase):
    def test_the_failure_text_names_the_exception_type(self):
        row = self.queue()
        with mock.patch.object(
            notifications, "send_email", side_effect=TemplateDoesNotExist("missing")
        ):
            notifications.drain_notifications()
        row.refresh_from_db()
        self.assertEqual(row.last_error, "TemplateDoesNotExist: missing")

    def test_an_absurd_error_message_is_bounded_and_marked_as_cut(self):
        row = self.queue()
        with mock.patch.object(
            notifications,
            "send_email",
            side_effect=OSError("x" * (notifications.LAST_ERROR_MAX_LENGTH * 2)),
        ):
            notifications.drain_notifications()
        row.refresh_from_db()
        self.assertLessEqual(len(row.last_error), notifications.LAST_ERROR_MAX_LENGTH)
        self.assertTrue(row.last_error.endswith("..."))


@tag("notifications")
class OutboxDeadLetterAlertTests(OutboxDrainTestCase):
    def setUp(self):
        super().setUp()
        # The cooldown is a cache-backed, per-alert-type window; a previous
        # test's entry would suppress this class's first alert.
        caches["alerts"].clear()

    def alert_mails(self):
        return [message for message in mail.outbox if message.to == [ALERT_RECIPIENT]]

    def queue_undeliverable(self, buyer="alertbuyer"):
        order = make_order(self.make_user(buyer))
        row = self.queue(order=order)
        Order.objects.filter(pk=order.pk).delete()
        return row

    def test_a_row_that_can_never_be_resolved_raises_an_admin_alert(self):
        row = self.queue_undeliverable("alertunresolvable")
        with override_settings(ALERT_RECIPIENTS=ALERT_RECIPIENT):
            result = notifications.drain_notifications()
        self.assertEqual(result.dead, 1)
        alerts_sent = self.alert_mails()
        self.assertEqual(len(alerts_sent), 1)
        self.assertIn(str(row.pk), alerts_sent[0].body)
        self.assertIn("is gone", alerts_sent[0].body)

    @override_settings(NOTIFICATION_OUTBOX_MAX_ATTEMPTS=1)
    def test_a_row_that_exhausts_its_attempts_raises_an_admin_alert(self):
        def refuse(context):
            raise OSError("SMTPRecipientsRefused: nobody@example.com")

        row = self.queue(order=make_order(self.make_user("alertattempts")))
        with override_settings(ALERT_RECIPIENTS=ALERT_RECIPIENT):
            with mock.patch.dict(
                notifications._EVENT_HANDLERS,
                {AuditEvent.EventType.ORDER_PAID: refuse},
            ):
                result = notifications.drain_notifications()
        self.assertEqual(result.dead, 1)
        alerts_sent = self.alert_mails()
        self.assertEqual(len(alerts_sent), 1)
        self.assertIn(str(row.pk), alerts_sent[0].body)
        self.assertIn("SMTPRecipientsRefused", alerts_sent[0].body)

    def test_repeated_failures_alert_again_once_the_cooldown_lapses(self):
        first = self.queue_undeliverable("alertrepeatone")
        with override_settings(ALERT_RECIPIENTS=ALERT_RECIPIENT):
            notifications.drain_notifications()
            self.assertEqual(len(self.alert_mails()), 1)

            self.queue_undeliverable("alertrepeattwo")
            notifications.drain_notifications()
            self.assertEqual(len(self.alert_mails()), 1)

            caches["alerts"].clear()
            self.queue_undeliverable("alertrepeatthree")
            notifications.drain_notifications()
        self.assertEqual(len(self.alert_mails()), 2)
        self.assertNotEqual(first.pk, 0)

    def test_with_no_recipient_configured_the_row_still_dies_and_nothing_is_sent(self):
        row = self.queue_undeliverable("alertnocrecipient")
        with override_settings(ALERT_RECIPIENTS=""):
            result = notifications.drain_notifications()
        self.assertEqual(result.dead, 1)
        self.assertEqual(len(mail.outbox), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.DEAD)

    def test_an_alert_send_failure_never_breaks_the_drain_loop(self):
        row = self.queue_undeliverable("alertraisesnothing")
        with mock.patch.object(
            notifications, "send_email", side_effect=OSError("down")
        ):
            with override_settings(ALERT_RECIPIENTS=ALERT_RECIPIENT):
                result = notifications.drain_notifications()
        self.assertEqual(result.dead, 1)
        self.assertEqual(len(mail.outbox), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.DEAD)


@tag("notifications")
class OutboxRetryRetentionTests(OutboxDrainTestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("drain_notification_outbox", *args, stdout=out)
        return out.getvalue()

    def test_a_dead_row_past_its_expiry_stays_dead_and_is_named_in_the_report(self):
        row = self.queue_expired(
            status=NotificationOutbox.Status.DEAD,
            attempts=3,
            last_error="OSError: gone",
        )
        output = self._run("--retry-dead")
        self.assertIn("re-opened 0 dead row", output)
        self.assertIn("dead 1", output)
        self.assertEqual(len(mail.outbox), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.DEAD)
        self.assertEqual(row.attempts, 3)
        self.assertEqual(row.last_error, "OSError: gone")

    def test_the_report_counts_the_refusals_next_to_the_rows_that_did_retry(self):
        retryable = self.queue(
            order=make_order(self.make_user("retrylive")),
            status=NotificationOutbox.Status.DEAD,
            attempts=1,
        )
        expired = [
            self.queue_expired(
                order=make_order(self.make_user(f"retrypast{index}")),
                status=NotificationOutbox.Status.DEAD,
                attempts=2,
            )
            for index in range(2)
        ]
        output = self._run("--retry-dead")
        self.assertIn("re-opened 1 dead row", output)
        self.assertIn("left dead past their expiry", output)
        retryable.refresh_from_db()
        self.assertEqual(retryable.status, NotificationOutbox.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)
        for row in expired:
            row.refresh_from_db()
            self.assertEqual(row.status, NotificationOutbox.Status.DEAD)
        self.assertEqual(
            NotificationOutbox.objects.filter(
                status=NotificationOutbox.Status.DEAD
            ).count(),
            2,
        )

    def test_a_row_whose_expiry_has_not_arrived_still_retries(self):
        row = self.queue(
            expires_at=future(),
            status=NotificationOutbox.Status.DEAD,
            attempts=1,
        )
        self._run("--retry-dead")
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.SENT)

    def test_the_retry_never_moves_a_row_past_the_instant_it_expires(self):
        row = self.queue_expired(status=NotificationOutbox.Status.DEAD, attempts=1)
        original_expiry = row.expires_at
        self._run("--retry-dead")
        row.refresh_from_db()
        self.assertEqual(row.expires_at, original_expiry)

    def test_the_refusal_is_specific_to_the_retry_flag(self):
        row = self.queue_expired(status=NotificationOutbox.Status.DEAD, attempts=3)
        self._run()
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.DEAD)
        self.assertIn("dead 1", self._run("--status-only"))

    def test_a_long_refusal_list_is_bounded_in_the_log_but_never_in_the_count(self):
        for index in range(23):
            self.queue_expired(
                order=make_order(self.make_user(f"manyrefused{index}")),
                status=NotificationOutbox.Status.DEAD,
                attempts=2,
            )
        with self.assertLogs("common.notifications", level="INFO") as captured:
            output = self._run("--retry-dead")
        self.assertIn("23 left dead past their expiry", output)
        line = "\n".join(captured.output)
        self.assertIn("23 left dead past their retention window", line)
        self.assertIn("(+3 more)", line)
        # Exactly DEAD_RETRY_PK_LOG_LIMIT pks are spelled out, every one a bare integer.
        pk_list = line.split("pks=[")[1].split("]")[0]
        parts = [part.strip() for part in pk_list.split(", ")]
        self.assertEqual(parts[-1], "(+3 more)")
        spelled_out = parts[:-1]
        self.assertEqual(len(spelled_out), notifications.DEAD_RETRY_PK_LOG_LIMIT)
        self.assertTrue(all(part.isdigit() for part in spelled_out))
        shortfall = int(parts[-1].strip("()+ more"))
        self.assertEqual(shortfall, 23 - notifications.DEAD_RETRY_PK_LOG_LIMIT)

    def test_status_only_still_reports_a_retry_that_ran(self):
        self.queue(
            order=make_order(self.make_user("statusonlylive")),
            status=NotificationOutbox.Status.DEAD,
            attempts=1,
        )
        self.queue_expired(
            order=make_order(self.make_user("statusonlypast")),
            status=NotificationOutbox.Status.DEAD,
            attempts=1,
        )
        output = self._run("--status-only", "--retry-dead")
        self.assertIn("--status-only", output)
        self.assertIn("re-opened 1 dead row(s)", output)
        self.assertIn("1 left dead past their expiry", output)
        self.assertEqual(len(mail.outbox), 0)

    def test_status_only_without_the_retry_flag_keeps_its_own_one_line_shape(self):
        output = self._run("--status-only")
        self.assertNotIn("re-opened", output)


@tag("notifications")
class OutboxBatchSizeArgumentTests(OutboxDrainTestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("drain_notification_outbox", *args, stdout=out)
        return out.getvalue()

    def test_a_zero_batch_size_is_refused_by_name(self):
        self.queue_many(2)
        with self.assertRaises(CommandError) as caught:
            self._run("--batch-size", "0")
        self.assertIn("--batch-size", str(caught.exception))
        self.assertIn("positive", str(caught.exception))
        self.assertEqual(len(mail.outbox), 0)

    def test_a_negative_batch_size_is_refused_rather_than_silently_accepted(self):
        self.queue_many(2)
        with self.assertRaises(CommandError) as caught:
            self._run("--batch-size", "-3")
        self.assertIn("--batch-size", str(caught.exception))
        self.assertEqual(len(mail.outbox), 0)

    def test_a_non_numeric_batch_size_is_refused(self):
        with self.assertRaises(CommandError) as caught:
            self._run("--batch-size", "many")
        self.assertIn("--batch-size", str(caught.exception))

    def test_a_positive_batch_size_is_still_honoured(self):
        self.queue_many(3)
        output = self._run("--batch-size", "2")
        self.assertIn("examined 2", output)
        self.assertIn("pending 1", output)
        self.assertEqual(len(mail.outbox), 2)


@tag("notifications")
class OutboxStatusReportStrengthTests(OutboxDrainTestCase):
    def seed(self, status, count, buyer_prefix, age_minutes):
        for index in range(count):
            self.queue(
                order=make_order(self.make_user(f"{buyer_prefix}{index}")),
                status=status,
                created_at=past() - timedelta(minutes=age_minutes + index),
            )

    def test_the_report_sums_every_row_in_a_status_not_just_the_last_group(self):
        self.seed(NotificationOutbox.Status.PENDING, 3, "sumpending", 10)
        self.seed(NotificationOutbox.Status.SENT, 2, "sumsent", 30)
        self.seed(NotificationOutbox.Status.FAILED, 4, "sumfailed", 50)
        self.seed(NotificationOutbox.Status.DEAD, 1, "sumdead", 70)
        self.assertEqual(
            notifications.outbox_status_counts(),
            {"pending": 3, "sent": 2, "failed": 4, "dead": 1},
        )

    def test_the_report_is_still_all_zeros_on_an_empty_table(self):
        self.assertEqual(
            notifications.outbox_status_counts(),
            {"pending": 0, "sent": 0, "failed": 0, "dead": 0},
        )

    def test_a_status_that_only_ever_appears_alone_still_counts(self):
        self.queue(status=NotificationOutbox.Status.PENDING)
        NotificationOutbox.objects.update(status=NotificationOutbox.Status.SENT)
        self.assertEqual(
            notifications.outbox_status_counts(),
            {"pending": 0, "sent": 1, "failed": 0, "dead": 0},
        )

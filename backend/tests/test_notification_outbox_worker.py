"""ASYNC-2c2: the outbox drain loop — claim, send, bounded retry, dead-letter.

The substrate ``tests/test_notification_outbox.py`` builds is inert; this is
what makes it move. What is pinned here, and against which plausible wrong
implementation:

- **No call site is converted.** The last class asserts the customer's side of
  the wire — the mail still arrives from ``verify_payment``, and the outbox is
  still empty afterwards. Converting a site before a drain exists would mean a
  notification that is never delivered, and only a test rewritten to count
  queue rows would notice. That is the dark regression this whole task is
  ordered around, so it is pinned rather than asserted in prose.
- **The guarantee is at-least-once, and the tests prove it rather than
  trusting the docstring.** ``WorkerCrashTests`` kills a worker between the send
  and the stamp and asserts the row is sent AGAIN once its lease lapses. A
  worker that dropped the row instead would fail; one that never re-sent would
  fail. The dedup key cannot help here and is not asked to: it is unique and
  byte-identical before and after a send, so it carries no send state.
- **A second worker cannot take a claimed row.** Proved twice: engine-
  independently (the claim predicate is false for the loser, because the claim
  pushed ``next_attempt_at`` out by a lease) and with two REAL connections on
  PostgreSQL. The engine-independent half is the one that can be vacuous, so
  it is pinned at two levels and its premise is asserted first.
- **The locked queryset joins nothing.** ``NotificationOutbox`` has no foreign
  keys, so the ``LEFT OUTER JOIN ... FOR UPDATE`` that once 500'd every payment
  verification on PostgreSQL is not reachable here — asserted against the SQL
  the real claim issues, on both engines, and asserted to contain no JOIN at
  all rather than only no nullable one.
- **Retry is bounded.** Every payload that can fail at send time is enumerated
  and driven: a deleted reference, an unregistered model label, a label the
  registry cannot parse, an unaddressable primary key, an event whose handler
  is gone, and a send that keeps raising. Each reaches a terminal state and
  stops being claimed, and one of them sits in the MIDDLE of a batch to prove
  one unfixable row does not end the pass.
- **Backoff is a decision, not a constant.** Every knob is env-driven and read
  from settings, and the invariant pinned is the one that survives a retune:
  a row whose next attempt is in the future is not claimed.
- **Every status value is driven**, because coverage measures lines executed and
  a value nothing ever assigns is a line that looks covered and is not.

Two levels are deliberately separated, as in ``test_postgres_row_locking.py``:
assertions phrased in terms of the ORM fire on SQLite AND PostgreSQL, and only
the "the clause is actually emitted" half is PostgreSQL-only, because SQLite's
compiler drops ``FOR UPDATE``. Nothing here is claimed to be pinned on an engine
that cannot fire it.
"""

import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
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

# The mailbox the admin alert module is pointed at where an alert is expected.
# Named here rather than imported so this file's expectations do not move when
# the alert module's own test helpers are refactored.
ALERT_RECIPIENT = "outbox-oncall@example.com"

# Poison payloads, one row each. Every way the substrate's own docstrings say a
# stored context can fail at send time, so "the batch survives a bad row" is
# tested against the real set rather than the one that happened to come to mind.
# Paired with the text the dead-letter is expected to carry, so a case whose
# failure is reported some other way cannot pass by matching nothing.
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
    """A real order for the real handler to render a real email about."""
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
    """Shared fixture: an order, its queued row, and a helper to make more."""

    def setUp(self):
        self.user = self.make_user("drain")
        self.order = make_order(self.user)

    def queue(self, order=None, **overrides):
        """Write one outbox row for ``order.paid``, bypassing the dedup key.

        Deliberately built through ``enqueue`` with a distinct payload per row
        (each names its own order), so the row under test is a row the real
        enqueue path produced rather than a hand-made fixture that could
        differ from it in some way nobody thought to check.
        """
        row = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": order if order is not None else self.order},
        )
        if overrides:
            NotificationOutbox.objects.filter(pk=row.pk).update(**overrides)
            row.refresh_from_db()
        return row

    def queue_many(self, count, **overrides):
        """``count`` rows, each about its OWN order, oldest first.

        Each gets a real Order so the dedup key is genuinely distinct; the
        shared fixture order would collapse them all into one row and the
        "FIFO order of a batch" assertion would be measuring nothing.
        """
        rows = []
        for index in range(count):
            buyer = self.make_user(f"drain{index}")
            rows.append(self.queue(order=make_order(buyer), **overrides))
        return rows

    def queue_expired(self, **overrides):
        """One row whose retention window has already closed."""
        fields = {"expires_at": past()}
        fields.update(overrides)
        return self.queue(**fields)

    def assert_dead(self, row, needle):
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.DEAD)
        self.assertIn(needle, row.last_error)


@tag("notifications")
class OutboxDrainDeliveryTests(OutboxDrainTestCase):
    """The happy path, and the batch shape around it."""

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
        """Resolution happens at drain time, so an amended order is reflected.

        This is the property that lets a queued notification be a durable
        intent rather than a frozen snapshot, and it is what the drain path
        would lose if it re-rendered from the stored payload instead of
        resolving references.
        """
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
        # The pass must stop on the first empty claim rather than looping to
        # the batch limit; one peek is all an idle table should cost.
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
        # The two drained are the two oldest, by the model's own Meta.ordering.
        drained = set(
            NotificationOutbox.objects.filter(status="sent").values_list(
                "pk", flat=True
            )
        )
        self.assertEqual(drained, {rows[0].pk, rows[1].pk})

    def test_a_row_another_worker_took_first_is_skipped_not_waited_on(self):
        """The lost race, driven deterministically.

        The window between the cheap peek and the row lock is where two workers
        meet. This closes it from inside: the lock is taken for a row that the
        peek offered, but the row has been reserved by somebody else by then,
        so the re-check must refuse it AND the loop must move on to the next
        candidate rather than end the pass having delivered nothing.
        """
        from django.db.models import QuerySet

        loser = self.queue(order=make_order(self.make_user("loser")))
        winner = self.queue(order=make_order(self.make_user("winner")))
        real_lock = QuerySet.select_for_update

        def reserve_the_first_row_first(queryset, *args, **kwargs):
            # Another worker claims the older row in the window between our
            # peek and our lock. Runs BEFORE the lock is taken, so it cannot
            # deadlock against the very lock it is standing in for.
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
    """Retry policy with backoff, and the invariant that expresses it."""

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
        """The invariant, stated as behaviour rather than as a count.

        Phrased this way because it survives every retune of the two knobs: it
        is true for any base, cap or attempt number, where "waited exactly 30
        seconds" is only true for the current defaults.
        """
        row = self.queue()
        with mock.patch.object(
            notifications, "send_email", side_effect=OSError("provider down")
        ):
            notifications.drain_notifications()
        row.refresh_from_db()
        self.assertGreater(row.next_attempt_at, timezone.now())

        # Same row, same instant, no send attempt at all.
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
        """Both knobs, and the cap.

        The literals are the point: a fixed 30-second constant in code would
        pass every other test in this class and fail here, which is the only
        way a policy that a deployment cannot tune gets noticed.
        """
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
        """The exponent is clamped, not the value trusted.

        ``attempts`` is a column a migrated or hand-edited row can carry at
        anything, and ``2 ** 10**9`` is slow enough on its own to look like a
        wedged worker.
        """
        self.assertEqual(
            notifications.retry_delay_seconds(10**9),
            notifications.retry_delay_seconds(notifications._BACKOFF_EXPONENT_CAP + 1),
        )

    def test_a_row_past_its_expiry_is_never_sent(self):
        """The second gate, and why it is a gate rather than a nicety.

        A payload may hold one-time token material whose validity
        ``PASSWORD_RESET_TIMEOUT`` has already revoked. Sending the mail anyway
        produces a link that looks like a link and does not work, which is the
        exact failure ``serialize_context`` refuses to create by truncation.

        Both layers are asserted separately — the PREDICATE must exclude the
        row, and the claim must refuse it — because the loop checks the expiry
        in both places (once in the cheap indexed peek, once under the lock).
        A test that only asserted "nothing was sent" would pass with either
        layer removed, which is coverage that cannot see half its own subject.
        """
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
    """Bounded retries, and a row that cannot ever succeed stopping the batch."""

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
        # that kept retrying a dead row forever would also end up DEAD here, and
        # only the count separates the two.
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
        """Every resolution failure the substrate documents, driven one by one.

        One test over a dict rather than four tests, so adding a case to
        ``UNRESOLVABLE_PAYLOADS`` cannot quietly add an unexercised branch: the
        case name appears in every failure message, so a new entry that behaves
        differently is a failure that names itself.

        Each case gets its own order because the rows are real enqueued rows
        and the dedup key would otherwise collapse them into one.
        """
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
        """The sixth failure mode, and it is not a resolution failure.

        The event had a handler when the row was queued and has none now —
        nothing can render it and no retry will ever produce a handler, so
        rescheduling it would only spend attempts on a certainty. The row is
        built by enqueueing a real event and then changing its event_type,
        because ``enqueue`` itself refuses to write a row for an event with no
        handler (so that converting a call site cannot create an
        undeliverable row in the first place).
        """
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
        """The promise ``resolve_context``'s docstring makes, exercised.

        Three good rows, one poison row in the MIDDLE, one good row after it.
        A pass that stopped on the poison row would send two emails and report
        one sent; one that died on it would raise. Only "all three good rows
        delivered, poison dead-lettered" distinguishes the correct loop from a
        lucky one that happened to be handed a clean batch.
        """
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
        """The purge command can remove a row the worker has already claimed.

        ``VANISHED`` exists so the summary can say so. A loop that counted it
        as sent would report a notification delivered that was never recorded,
        which is the one number an operator would not question — and Django's
        ``save()`` would do worse than misreport: on a row deleted underneath
        it, ``save()`` re-INSERTs the very payload the purge deleted.
        """
        self.queue()
        with mock.patch.object(notifications, "resolve_context", _purge_then({})):
            result = notifications.drain_notifications()
        self.assertEqual(result.vanished, 1)
        self.assertEqual(result.sent, 0)
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    def test_a_row_purged_between_the_claim_and_a_failure_is_not_dead_lettered(self):
        """The same race on the failure path.

        The context handed back names a real order, so the handler genuinely
        attempts a send and genuinely fails — an earlier version returned an
        empty context, which made the handler log-and-return and never fail at
        all, so this test passed for the wrong reason.
        """
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
        """The same race one step earlier, and it needs its own seam.

        The purge command can win between the claim committing and the worker
        reading the row at all. ``claim_next_notification`` is patched to
        return a pk whose row is already gone, which is exactly the state the
        loop would find — and without this the branch is one no test can reach
        through the public entry point.
        """
        row = self.queue()
        NotificationOutbox.objects.all().delete()
        with mock.patch.object(
            notifications, "claim_next_notification", return_value=row.pk
        ):
            result = notifications.drain_notifications(batch_size=1)
        self.assertEqual(result.vanished, 1)
        self.assertEqual(result.examined, 1)

    def test_a_row_purged_before_a_dead_letter_is_not_reported_dead(self):
        """And on the close-out path, which is a different statement again."""
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
    """A stand-in for the purge command running mid-delivery.

    Returns the context it is handed (or raises), having deleted the outbox
    first. This is the only reachable way to race the purge against a claimed
    row without threads, and it exercises the same branch a real race does.
    """

    def _resolve(payload):
        NotificationOutbox.objects.all().delete()
        if raise_it is not None:
            raise raise_it
        return context

    return _resolve


@tag("notifications")
class OutboxStatusVocabularyTests(OutboxDrainTestCase):
    """Every value the vocabulary admits is driven, and each is reachable.

    Coverage counts lines executed, so a status nothing ever assigns would read
    as covered code. This asserts the mapping in both directions: every value
    is a real choice in the enum, and every one of them is a row the loop
    either produces or refuses to claim.
    """

    def test_the_vocabulary_is_exactly_these_four_values(self):
        """Written out, not derived.

        A check that built its expectation from the enum would agree with a
        wrong enum; these are literals, so adding or renaming a status without
        deciding what it means for the loop fails here.
        """
        self.assertEqual(
            list(NotificationOutbox.Status.values),
            ["pending", "sent", "failed", "dead"],
        )
        # Three of the four statuses are something a delivery can END as, and
        # VANISHED is a fourth outcome that is deliberately not a status: the
        # row is gone, so there is nothing left to record a state on. PENDING
        # is the one status with no outcome counterpart, because a row that
        # has never been attempted has not had a delivery at all.
        self.assertEqual(
            sorted(outcome.value for outcome in notifications.DrainOutcome),
            ["dead", "failed", "sent", "vanished"],
        )

    def test_every_status_is_reachable_and_each_says_whether_it_is_claimable(self):
        """One row per status, and the claim answer for each, written out.

        A table derived from ``CLAIMABLE_STATUSES`` would agree with a wrong
        constant from both sides, so the expected answer is a literal here and
        the rows are real enqueued rows, each about its own order.
        """
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
        """A report that omits a zero cannot be told from one that cannot count.

        Four keys are present no matter what is in the table, so "no rows are
        dead-lettered" and "this query cannot see dead-lettered rows" are
        different strings.
        """
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
    """The delivery guarantee, broken on purpose and observed.

    The substrate's own docstring claims a worker that dies between sending and
    stamping re-sends the row. These are the tests that make that claim
    falsifiable, and they were written to FAIL against a loop that stamped the
    row before sending it (the row would be SENT and never re-sent) and against
    one that marked it handled without a status at all (the row would be
    dropped).
    """

    def test_a_worker_that_dies_after_sending_loses_the_row_from_the_next_pass(self):
        row = self.queue()
        # The send happens; the stamp does not. The worker's process "dies"
        # exactly where a SIGKILL would take it.
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
        """The other half of at-least-once, and the half a duplicate is the price of.

        Not claimed while the lease holds — that is what stops a second worker
        starting the same send while this one is still inside it — and claimed
        again after it, which is what stops the row being lost.
        """
        row = self.queue()
        with mock.patch.object(
            notifications, "_record_success", side_effect=SystemExit("killed")
        ):
            with self.assertRaises(SystemExit):
                notifications.drain_notifications()

        # Inside the lease: nobody else may take it.
        self.assertIsNone(notifications.claim_next_notification())
        self.assertEqual(len(mail.outbox), 1)

        # After it: the notification goes out again. A duplicate email is the
        # documented price of at-least-once and is strictly better than the
        # alternative this whole table exists to remove.
        NotificationOutbox.objects.filter(pk=row.pk).update(next_attempt_at=past())
        result = notifications.drain_notifications()
        self.assertEqual(result.sent, 1)
        self.assertEqual(len(mail.outbox), 2)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.SENT)
        self.assertEqual(row.attempts, 2)

    @override_settings(NOTIFICATION_OUTBOX_LEASE_SECONDS=600)
    def test_the_lease_length_is_env_driven(self):
        """A lease hard-coded to 60 would fail here and nowhere else."""
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
    """The claim's SQL, read on the engine where this defect actually lives.

    SQLite serialises writes and drops ``select_for_update`` entirely, so a
    concurrency claim measured on SQLite alone is unmeasured. Both halves are
    pinned at the level where each can actually fail.
    """

    def test_the_claim_asks_the_orm_to_lock_the_row_on_every_engine(self):
        """ORM-level, so it fires on SQLite too.

        The observation is the CALL, not the clause: SQLite's compiler drops
        ``FOR UPDATE`` so a SQL-phrased assertion cannot fire there, but the
        call is still made and is still recorded. A row exists, so the claim
        really reaches the lock — otherwise this would pass with no work done.
        """
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
        """No JOIN at all, on either engine.

        ``NotificationOutbox`` carries no foreign key, so the claim cannot
        produce the ``LEFT OUTER JOIN ... FOR UPDATE`` PostgreSQL rejects and
        which 500'd every payment verification in production. Asserting "no
        JOIN" rather than "no nullable-FK join" is deliberate: there is no
        nullable FK here to name, and the broader assertion is the one that
        fails if someone adds a join in future.
        """
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
        """The peek runs on every pass, so it is pinned like the lock."""
        with transaction.atomic():
            sql, _params = (
                notifications.claimable_notifications()
                .query.get_compiler("default")
                .as_sql()
            )
        self.assertNotIn("JOIN", sql.upper())


@tag("notifications", "e2e")
class OutboxTwoWorkerTests(TransactionTestCase):
    """Two real connections, two workers, one send.

    ``TransactionTestCase`` and not ``TestCase``: the point is two SEPARATE
    database connections holding real row locks, and a test-case transaction
    would put both workers in the same one, where the race cannot happen.
    ``close_old_connections`` per worker is what makes them genuinely separate.
    """

    reset_sequences = True

    def _make_row(self, username):
        # Users are made directly rather than through ApiTestCase's factory:
        # this class is a TransactionTestCase on purpose (two separate
        # connections), and the factory lives on the TestCase base.
        user = get_user_model().objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="S3cure-Passphrase!",
        )
        order = make_order(user)
        return notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})

    def _race(self, barrier=False):
        """Run two claims on two real connections and report what happened.

        Returns ``(claimed_pks, errors)``. Errors are NOT re-raised: on SQLite
        a genuinely concurrent write raises ``OperationalError: database table
        is locked`` rather than blocking, because that engine serialises all
        writes and never emits ``FOR UPDATE``. That is the engine doing the
        mutual exclusion instead of the row lock, so it must not be reported as
        a fault — but neither must it be hidden, which is why this returns the
        errors instead of swallowing them. The assertion that matters is on
        the row, not on the return value: how many times it was claimed.
        """
        claimed = []
        errors = []
        gate = threading.Barrier(2) if barrier else None

        def worker():
            close_old_connections()
            try:
                if gate is not None:
                    gate.wait()
                claimed.append(notifications.claim_next_notification())
            except Exception as exc:  # reported, never swallowed
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
        """Which half of the guarantee this engine can actually exercise.

        On an engine with real row locks, both workers must get a clean answer
        and nothing may be refused. On one without, a refusal is the documented
        consequence of the engine's own write serialisation — and any error
        that is NOT a lock refusal is a real fault and fails here, so this is a
        gate and not a shrug.
        """
        if connection.features.has_select_for_update:
            self.assertEqual(errors, [], "a worker failed on an engine that locks rows")
            return
        for error in errors:
            self.assertIsInstance(error, OperationalError)
            self.assertIn("lock", str(error).lower())

    def test_two_workers_racing_one_row_claim_it_exactly_once(self):
        """The guarantee, on two connections, released together.

        The barrier puts both workers inside the same window between reading the
        claimable set and taking the lock, which is the window the lock and the
        lease have to close. Without it this test passes against a loop with no
        guard at all, often enough to be worth nothing.

        The assertion is on ``attempts`` — the row's own count of claims — and
        not on which worker returned what. That holds whichever worker won and
        whether or not the engine serialised the write for us, so it is a real
        measurement on BOTH engines; a double-claim would show as two. The
        exact count is asserted only where the engine can be relied on to let
        one of the two writes through, because SQLite may refuse both.
        """
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
        # And the row is now nobody else's to take.
        self.assertIsNone(notifications.claim_next_notification())

    def test_two_workers_racing_two_rows_still_send_each_one_exactly_once(self):
        """End to end, on the customer's side of the wire.

        Asserted on the MAIL, because a double send is the failure and a claim
        is not a send. The leases are released before the draining pass so the
        rows whose claims were interrupted (or which simply lost the race and
        are waiting) are all delivered — and the count of emails is what says
        each was delivered once.

        The claim count is asserted as an UPPER bound on both engines, which is
        the half of the guarantee the claim implements: "at most once in
        flight". The lower bound ("each row was claimed") is asserted only
        where the engine can express it, because SQLite may refuse BOTH
        concurrent writes outright rather than serialising them, and a test
        that demanded it there would be asserting SQLite's locking policy.
        """
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

    def test_one_worker_drains_a_row_two_workers_must_share(self):
        """The end-to-end shape: one pass sends it once, not twice.

        Asserting on the MAIL rather than on the claim, because a double send
        is the failure and a claim is not a send.
        """
        self._make_row("two-worker-drain")
        notifications.drain_notifications()
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        self.assertEqual(
            NotificationOutbox.objects.values_list("status", flat=True)[0],
            NotificationOutbox.Status.SENT,
        )


@tag("notifications")
class NoCallSiteConvertedByWorkerTests(ApiTestCase):
    """The load-bearing constraint, pinned on the customer's side of the wire.

    ASYNC-2c3 converts call sites; this task makes that safe. If anything here
    were converted, the notification would exist only as a row and no test that
    counted rows would notice, so these assert an email ARRIVES — with the real
    ``api_login`` token, never ``force_authenticate``.
    """

    def setUp(self):
        self.user = self.make_user("livepath")
        _, self.token = self.api_login(username=self.user.username)
        self.product = self.make_product(stock=5)

    def test_verify_payment_still_sends_inline_and_queues_nothing(self):
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        self.razorpay_mock()
        self.client.post(
            "/api/orders/payment/", {"order_id": res.data["id"]}, format="json"
        )
        with self.captureOnCommitCallbacks(execute=True):
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
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    def test_draining_an_empty_outbox_sends_nothing(self):
        """A worker with nothing to do must not send, or would double-mail.

        Once call sites ARE converted this is the assertion that keeps the
        drain loop from being a second sender.
        """
        result = notifications.drain_notifications()
        self.assertEqual(result.examined, 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_purge_command_is_still_the_deletion_owner(self):
        """The worker's opt-in does not take the deletion job away from it.

        Without --purge-expired a pass must not delete anything, so an expired
        row is still there for the named owner to remove.
        """
        row = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": make_order(self.user)},
            # A payload with no reference cannot be delivered, so this row is
            # SENT as a plain body with no order in it: enough to prove the
            # claim happened, and it never leaves an undelivered customer.
            occurrence="purge-ownership-probe",
        )
        NotificationOutbox.objects.filter(pk=row.pk).update(
            expires_at=past(), payload={}
        )
        notifications.drain_notifications()
        self.assertTrue(NotificationOutbox.objects.filter(pk=row.pk).exists())


@tag("notifications")
class DrainCommandTests(OutboxDrainTestCase):
    """The operator surface: the pass, the status report, and manual retry."""

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
        """Manual retry, end to end through the operator's command.

        ``attempts`` is preserved, so a row that fails again is dead again on
        the same attempt — asserted in the next test rather than trusted here.
        """
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
        # The history of the failure survives the success as a count.
        self.assertEqual(row.attempts, 10)
        self.assertEqual(row.last_error, "")

    @override_settings(NOTIFICATION_OUTBOX_MAX_ATTEMPTS=3)
    def test_a_re_opened_row_that_fails_again_is_dead_lettered_not_retried(self):
        """Why ``attempts`` is not reset: the bound must survive manual retry."""
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
        """The filter, asserted on what actually changed rather than on status.

        Both rows are dead before the run and dead after it (the wanted row is
        re-opened, then dead again the moment it finds it has no handler), so
        status alone cannot tell them apart. The attempt count can: only the
        row the filter matched was claimed.
        """
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
        """Off by default, and the flag is what turns it on.

        The default is the safety property: a dead-lettered row is the one an
        operator has not looked at yet, and a pass that deleted it unasked
        would remove the evidence before anyone read it.
        """
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
        """Env-driven, observed as behaviour rather than read off the helper.

        Nine claimable rows, a configured ceiling of seven and no argument: the
        pass must stop at seven. Asserting that the private reader returns the
        setting would pass just as well if nothing ever called it.
        """
        self.queue_many(9)
        result = notifications.drain_notifications()
        self.assertEqual(result.examined, 7)
        self.assertEqual(result.sent, 7)


@tag("notifications")
class OutboxErrorTextTests(OutboxDrainTestCase):
    """What the operator is left holding when a row dies."""

    def test_the_failure_text_names_the_exception_type(self):
        """The class name is the part that survives a backend's own formatting.

        "SMTPRecipientsRefused" and "TemplateDoesNotExist" are different
        operator problems, and a message body that reads the same for both
        costs the on-call engineer the one thing the row was kept for.
        """
        row = self.queue()
        with mock.patch.object(
            notifications, "send_email", side_effect=TemplateDoesNotExist("missing")
        ):
            notifications.drain_notifications()
        row.refresh_from_db()
        self.assertEqual(row.last_error, "TemplateDoesNotExist: missing")

    def test_an_absurd_error_message_is_bounded_and_marked_as_cut(self):
        """Truncated, and visibly so.

        The opposite of ``PAYLOAD_VALUE_MAX_LENGTH``, which refuses rather than
        cuts: refusing here would mean refusing to record why a notification
        failed, which is the one thing a dead-letter exists to say. The ellipsis
        is appended so nobody reads a cut sentence as the whole error.
        """
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


# ---------------------------------------------------------------------------
# ASYNC-2c2 cycle 2: the four findings audit cycle 1 left open.
# ---------------------------------------------------------------------------


@tag("notifications")
class OutboxDeadLetterAlertTests(OutboxDrainTestCase):
    """A dead-lettered row reaches the admin alert module (spec 19.2).

    The dead-letter transition used to write ONE log line. ``ops.alerts`` is
    where every admin alert this product sends already goes, it already holds
    the recipient list and the per-type cooldown, and it already has the
    in-tree precedent of a caller raising an alert beside its own audit hook
    (a password-reset success beside the AUTH_PASSWORD_RESET record). Spec 19.2
    names failed notification deliveries as an admin notification and no such
    alert type existed.

    **These assert on MAIL, never on a log record.** A log line is exactly what
    the pre-fix code already emitted, so a test that asserted on one would pass
    against the defect it exists to close.
    """

    def setUp(self):
        super().setUp()
        # The cooldown is a cache-backed, per-alert-type window; a previous
        # test's entry would suppress this class's first alert.
        cache.clear()

    def alert_mails(self):
        return [message for message in mail.outbox if message.to == [ALERT_RECIPIENT]]

    def queue_undeliverable(self, buyer="alertbuyer"):
        """A queued row whose stored reference will not resolve at send time.

        Deleting the order is what makes this the *unresolvable* dead-letter
        path rather than the exhausted-attempts one, so the two tests below
        exercise the two places a row can be closed out.
        """
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
        # The body has to identify WHICH row died and WHY, or the operator has
        # a mailbox full of indistinguishable alerts and a table to go read.
        self.assertIn(str(row.pk), alerts_sent[0].body)
        self.assertIn("is gone", alerts_sent[0].body)

    @override_settings(NOTIFICATION_OUTBOX_MAX_ATTEMPTS=1)
    def test_a_row_that_exhausts_its_attempts_raises_an_admin_alert(self):
        """The other dead-letter path: every attempt failed.

        The handler is replaced rather than ``send_email``, and that is the
        point: the alert rides the SAME single send path as the notification,
        so patching ``send_email`` would break the alert too and this test
        could not distinguish "the alert fired" from "the alert tried and the
        stub ate it".
        """

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
        """Dead rows keep alerting once the cooldown window is cleared.

        Each drain in this test closes out one more unresolvable row, and the
        alert is bounded by the cooldown window rather than by the row: the
        pass inside the window is silent and the pass after a cleared window
        alerts again.

        "Alerting on repeated failures" is a per-type cooldown bounded repeat,
        so the property to pin is that the alert REPEATS. The cooldown itself
        is this project's existing mechanism and cycle 2 changed nothing
        inside it; the window is cleared between passes here purely so the
        repeat is observable rather than suppressed by design.
        """
        first = self.queue_undeliverable("alertrepeatone")
        with override_settings(ALERT_RECIPIENTS=ALERT_RECIPIENT):
            notifications.drain_notifications()
            self.assertEqual(len(self.alert_mails()), 1)

            # Inside the cooldown the mechanism this task does not own
            # collapses the repeat. Asserted so a change to it is noticed
            # rather than discovered in an operator's inbox.
            self.queue_undeliverable("alertrepeattwo")
            notifications.drain_notifications()
            self.assertEqual(len(self.alert_mails()), 1)

            cache.clear()
            self.queue_undeliverable("alertrepeatthree")
            notifications.drain_notifications()
        self.assertEqual(len(self.alert_mails()), 2)
        self.assertNotEqual(first.pk, 0)

    def test_with_no_recipient_configured_the_row_still_dies_and_nothing_is_sent(self):
        """An unconfigured alert budget must not cost the dead-letter.

        The alert module's own contract is log-only and never raises into the
        flow that tripped it; this pins that the drain loop's terminal
        transition is that flow, so an empty ``ALERT_RECIPIENTS`` degrades to
        "no admin hears about it" and never to "the row is not recorded".
        """
        row = self.queue_undeliverable("alertnocrecipient")
        with override_settings(ALERT_RECIPIENTS=""):
            result = notifications.drain_notifications()
        self.assertEqual(result.dead, 1)
        self.assertEqual(len(mail.outbox), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.DEAD)

    def test_an_alert_send_failure_never_breaks_the_drain_loop(self):
        """Log-only, like every other alert in this project.

        If raising the alert could abort a pass, a broken mail provider on the
        ALERT path would stop notifications being delivered at all — the
        monitoring would become the outage. The row is queued BEFORE the stub,
        because ``enqueue`` refuses an event with no registered handler and a
        patch applied first would silently leave nothing to dead-letter.
        """
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
    """``--retry-dead`` against a row whose retention window has closed.

    The cycle-2 decision, in tests. A dead row past ``expires_at`` is NOT
    re-opened, because the payload is deliberately not scrubbed of one-time
    token material and deletion on a clock is the compensating control — so
    extending an expired row's life to make a retry work would extend the
    retention of exactly the material the expiry exists to bound. And it could
    not deliver anyway: ``PASSWORD_RESET_TIMEOUT`` has almost certainly
    invalidated the token, and a link that cannot work is worse than no mail.

    The consequence this whole class guards is the one that made the defect a
    defect: the observability signal must SURVIVE the retry. A re-opened
    expired row became PENDING, was never claimed, and left the status report
    reading as though the failure had never happened.
    """

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
        # The reason the row died is the evidence the row was kept for.
        self.assertEqual(row.last_error, "OSError: gone")

    def test_the_report_counts_the_refusals_next_to_the_rows_that_did_retry(self):
        """Mixed batch: the operator sees both halves, not a single total."""
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
        """The refusal turns on the expiry and on nothing else.

        Pinned from both sides on purpose: a retry that refused everything, or
        one that refused only rows it happened to see first, would both pass a
        test that only drove an expired row.
        """
        row = self.queue(
            expires_at=future(),
            status=NotificationOutbox.Status.DEAD,
            attempts=1,
        )
        self._run("--retry-dead")
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.SENT)

    def test_the_retry_never_moves_a_row_past_the_instant_it_expires(self):
        """The retention bound is not the retry's to extend.

        Asserted on the stored value rather than on the status, because a
        re-opened row that reaches ``SENT`` on a hand-extended expiry would
        satisfy every other test in this class while quietly holding credential
        material past the bound that exists to bound it.
        """
        row = self.queue_expired(status=NotificationOutbox.Status.DEAD, attempts=1)
        original_expiry = row.expires_at
        self._run("--retry-dead")
        row.refresh_from_db()
        self.assertEqual(row.expires_at, original_expiry)

    def test_the_refusal_is_specific_to_the_retry_flag(self):
        """A plain drain leaves a dead row alone — it was never claimable.

        So the status report after an ordinary pass still shows it, and the
        operator who never asked for a retry never loses the signal either.
        """
        row = self.queue_expired(status=NotificationOutbox.Status.DEAD, attempts=3)
        self._run()
        row.refresh_from_db()
        self.assertEqual(row.status, NotificationOutbox.Status.DEAD)
        self.assertIn("dead 1", self._run("--status-only"))

    def test_a_long_refusal_list_is_bounded_in_the_log_but_never_in_the_count(self):
        """More refusals than the log will spell out.

        The count an operator acts on must be exact however long the pk list
        gets; only the list is capped, and the cap is marked rather than
        silent, so a reader cannot mistake a truncated list for the whole one.
        """
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
        # Exactly DEAD_RETRY_PK_LOG_LIMIT pks are spelled out, not all 23, and
        # every one of them is a bare integer — a bounded list that quietly grew
        # a word in it would still be unreadable at 20 entries.
        pk_list = line.split("pks=[")[1].split("]")[0]
        parts = [part.strip() for part in pk_list.split(", ")]
        self.assertEqual(parts[-1], "(+3 more)")
        spelled_out = parts[:-1]
        self.assertEqual(len(spelled_out), notifications.DEAD_RETRY_PK_LOG_LIMIT)
        self.assertTrue(all(part.isdigit() for part in spelled_out))
        # The marker must state the real shortfall, or a reader cannot tell a
        # bounded list from a wrong one.
        shortfall = int(parts[-1].strip("()+ more"))
        self.assertEqual(shortfall, 23 - notifications.DEAD_RETRY_PK_LOG_LIMIT)

    def test_status_only_still_reports_a_retry_that_ran(self):
        """The one combination an operator uses to DECIDE whether to retry.

        It sends nothing, so its output is the whole of what it says — which is
        why it has to carry the retry clause too, and why the clause is keyed
        on the flag rather than on the counts.
        """
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
        """No clause, so its presence keeps meaning "a retry ran".

        The regression guard on the choice above: keying the clause on the
        counts instead would print it here for an idle table, and an operator
        reading that would be told a retry happened when none did.
        """
        output = self._run("--status-only")
        self.assertNotIn("re-opened", output)


@tag("notifications")
class OutboxBatchSizeArgumentTests(OutboxDrainTestCase):
    """``--batch-size`` is validated, because a wrong one is silent otherwise.

    A negative limit used to make the pass examine nothing at all and a zero
    used to fall through to the configured default — both without an error, so
    an operator who typed ``--batch-size 0`` got a full pass they had not asked
    for and one who typed ``--batch-size -1`` got an empty report that read
    exactly like an idle queue.
    """

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
        """The validation must not swallow the argument it was added for."""
        self.queue_many(3)
        output = self._run("--batch-size", "2")
        self.assertIn("examined 2", output)
        self.assertIn("pending 1", output)
        self.assertEqual(len(mail.outbox), 2)


@tag("notifications")
class OutboxStatusReportStrengthTests(OutboxDrainTestCase):
    """The status report's pin, strengthened against the hazard beside it.

    ``outbox_status_counts`` assigns into a dict keyed by status from grouped
    rows. That is last-write-wins, and it would silently under-report if the
    ORM ever folded the model's declared ``Meta.ordering`` into the GROUP BY:
    one status would then arrive as several groups, and the last one written
    would win. The earlier pin could not see that, because it only ever put at
    most ONE row in each status — a hazard that merges nothing is invisible to
    a fixture that holds nothing to merge.

    Every row here therefore gets its own distinct ``created_at``, which is
    what makes the fold observable: grouped by ``created_at`` as well as
    status, four rows in one status arrive as four groups of one.
    """

    def seed(self, status, count, buyer_prefix, age_minutes):
        for index in range(count):
            self.queue(
                order=make_order(self.make_user(f"{buyer_prefix}{index}")),
                status=status,
                created_at=past() - timedelta(minutes=age_minutes + index),
            )

    def test_the_report_sums_every_row_in_a_status_not_just_the_last_group(self):
        """Hand-written literals, so a wrong report cannot agree with itself."""
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
        """The single-row case the stronger fixture above replaced.

        Kept as its own test so dropping to one row per status is a visible
        edit here rather than a silent weakening of the pin.
        """
        self.queue(status=NotificationOutbox.Status.PENDING)
        NotificationOutbox.objects.update(status=NotificationOutbox.Status.SENT)
        self.assertEqual(
            notifications.outbox_status_counts(),
            {"pending": 0, "sent": 1, "failed": 0, "dead": 0},
        )

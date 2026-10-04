"""ASYNC-2c1: the durable notification outbox — storage and enqueue only.

The substrate ASYNC-2c2 drains and ASYNC-2c3 converts call sites onto. It is
built and tested here with **no live call site converted**, which is the whole
safety argument for landing it early: converting a site before a drain loop
exists would mean the notification is silently never delivered, and only a test
rewritten to assert on queue rows instead of mail would notice. So the last
class here pins the current behaviour — sites still send inline, nothing is
queued — and it fails the moment someone converts one.

What is pinned, and against which plausible wrong implementation:

- **Nothing instance-shaped is stored.** Asserted on the raw column text, not
  on a re-read attribute: an implementation that dumped the model, or stored a
  rendered subject/template, fails here.
- **The row is written inside the caller's transaction**, visible before the
  block exits, and dies with a rollback. An ``on_commit`` enqueue fails the
  visibility test; a detached/own-transaction enqueue fails the rollback one.
- **The dedup key identifies the event, not its state.** Amending the order
  between two enqueues must still collide; a key derived from the order's
  mutable contents would not.
- **The stored form round-trips** back to live rows, and resolution reads
  CURRENT state — a snapshot implementation fails the amend-then-resolve test.
- **A vanished row fails in a defined way** (``UnresolvableNotification``)
  rather than crashing a drain loop or silently rendering nothing.
- **An unhandled event writes no row**, so converting a call site can never
  trade a working no-op for a permanently undelivered queue entry.
- **A failed enqueue propagates**, unlike ``dispatch``. That divergence is
  deliberate and is argued in ``notifications.enqueue``; if it is ever
  reverted to log-and-swallow, the dual-write this substrate removes comes
  back.
- **The dedup key is injective and order-independent.** Two distinct contexts
  must not collide on a separator, and the same context built in a different
  key order must produce the same key. Both were false of a ``":".join`` key.
- **A repeat is expressible.** ``occurrence`` is the deliberate escape hatch;
  without it a resend or a second reminder was structurally impossible.
- **An over-long value is refused, not cut.** A silently truncated token or
  URL renders as a working-looking link and fails at the customer.
- **The unsanitised payload has a control now.** ``expires_at`` is a field
  default, so no code path can omit it, and ``purge_notification_outbox`` is
  the named deletion owner. Both are asserted, not assumed.
"""

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase, override_settings, tag
from django.utils import timezone
from django.utils.translation import gettext_lazy

from common import notifications
from common.models import (
    NOTIFICATION_OUTBOX_DEFAULT_TTL_SECONDS,
    AuditEvent,
    NotificationOutbox,
    default_notification_outbox_expiry,
)
from common.notifications import NotificationPayloadError, UnresolvableNotification
from common.testing import ApiTestCase
from orders.models import Order


def _make_order(user, total="499.99"):
    return Order.objects.create(
        user=user,
        full_name="Outbox Buyer",
        phone="9876543210",
        address="12 Rose Lane",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        total_amount=Decimal(total),
    )


@tag("notifications")
class OutboxSerialisationTests(ApiTestCase):
    """What a row holds: identifiers and scalars, never live objects."""

    def test_a_model_context_is_stored_as_a_label_and_a_primary_key(self):
        order = _make_order(self.make_user("outboxref"))
        row = notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        self.assertIsNotNone(row)
        self.assertEqual(
            row.payload,
            {"order": {"label": "orders.order", "pk": order.pk}},
        )
        # The column itself, read back over SQL rather than through the
        # attribute: a repr of the instance, or a pre-rendered subject, would
        # be in this text and not in the attribute above.
        with connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM common_notificationoutbox")
            stored = cursor.fetchone()[0]
        if not isinstance(stored, str):
            # Some backends hand a JSON column back already parsed.
            stored = json.dumps(stored)
        self.assertEqual(json.loads(stored), row.payload)
        self.assertNotIn("<Order:", stored)
        self.assertNotIn("499.99", stored)
        self.assertNotIn("Rose Lane", stored)

    def test_decimal_date_and_lazy_values_are_stored_as_bounded_text(self):
        order = _make_order(self.make_user("outboxvals"))
        row = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {
                "order": order,
                # A money value must never round-trip through a float
                # (conventions.md), and a lazy translation string must not
                # survive into a row the draining process could resolve
                # against its own language.
                "amount": Decimal("499.99"),
                "label": gettext_lazy("Back in stock"),
                "rows": [order, None],
                "extra": {"nested": Decimal("1.50")},
            },
        )
        self.assertEqual(row.payload["amount"], "499.99")
        self.assertIsInstance(row.payload["amount"], str)
        self.assertEqual(row.payload["label"], "Back in stock")
        self.assertIsInstance(row.payload["label"], str)
        self.assertEqual(
            row.payload["rows"],
            [{"label": "orders.order", "pk": order.pk}, None],
        )
        self.assertEqual(row.payload["extra"]["nested"], "1.50")

    def test_an_over_long_value_is_refused_rather_than_truncated(self):
        # The defect this replaces: an over-long value was cut at an arbitrary
        # boundary and stored, which a probe could not detect (a bare "…" is
        # indistinguishable from a real one the author wrote) and which landed
        # mid-token - a 233-character reset URL was stored at 201 characters,
        # still starting "https://" and still containing "token=", so it
        # rendered as a working link and failed at the customer. Refusing puts
        # the failure at the call site instead.
        order = _make_order(self.make_user("outboxlong"))
        long_url = "https://shop.example/reset/" + "t" * 233
        with self.assertRaises(NotificationPayloadError) as caught:
            notifications.enqueue(
                AuditEvent.EventType.ORDER_PAID,
                {"order": order, "url": long_url},
            )
        self.assertIn(
            str(notifications.PAYLOAD_VALUE_MAX_LENGTH), str(caught.exception)
        )
        # Nothing was written: the refusal happens before the insert, so a bad
        # context cannot leave a half-queued row behind either.
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    def test_a_value_at_the_bound_is_stored_whole(self):
        exact = "v" * notifications.PAYLOAD_VALUE_MAX_LENGTH
        row = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": _make_order(self.make_user("outboxbound")), "code": exact},
        )
        self.assertEqual(row.payload["code"], exact)

    def test_a_set_context_value_is_ordered_deterministically(self):
        # Python's str() of a set walks it in hash order, which varies with
        # PYTHONHASHSEED - so the same context enqueued by two different
        # gunicorn workers used to produce two different dedup keys and the
        # duplicate suppression silently did not happen across workers. The
        # described value is sorted by its canonical JSON form instead.
        described = notifications.serialize_context({"tags": {"beta", "alpha"}})
        # Hand-written expectation, not one recomputed from the code under test.
        self.assertEqual(described, {"tags": ["alpha", "beta"]})

    def test_a_repeated_element_in_a_set_is_described_once(self):
        described = notifications.serialize_context({"tags": {"alpha", "alpha"}})
        self.assertEqual(described, {"tags": ["alpha"]})

    def test_a_non_mapping_context_is_stored_as_an_empty_payload(self):
        # Matches AuditEvent.record's treatment of a detail: a context is
        # always a mapping, and coercing something else into one would invent
        # data. Asserted against serialize_context directly so the enqueue path
        # does not have to be reachable with a broken context to hit it.
        self.assertEqual(notifications.serialize_context(None), {})
        self.assertEqual(notifications.serialize_context(["order"]), {})

    def test_the_stored_form_round_trips_back_to_live_rows(self):
        order = _make_order(self.make_user("outboxrt"))
        payload = notifications.serialize_context(
            {"order": order, "amount": Decimal("1.00")}
        )
        resolved = notifications.resolve_context(payload)
        self.assertIsInstance(resolved["order"], Order)
        self.assertEqual(resolved["order"], order)
        self.assertEqual(resolved["amount"], "1.00")

    def test_references_nested_in_mappings_and_sequences_also_resolve(self):
        # A bulk notification ("your three items shipped") carries a list of
        # rows, not one flat key, so resolution has to recurse exactly as
        # serialisation does. A resolver that only handled top-level keys
        # would hand the handler the raw reference mappings and the template
        # would render a dict instead of an order.
        first = _make_order(self.make_user("outboxnest1"))
        second = _make_order(self.make_user("outboxnest2"))
        payload = notifications.serialize_context(
            {
                "order": first,
                "batch": [second, None],
                "meta": {"primary": first},
            }
        )
        resolved = notifications.resolve_context(payload)
        self.assertEqual(resolved["order"], first)
        self.assertEqual(resolved["batch"][0], second)
        self.assertIsNone(resolved["batch"][1])
        self.assertEqual(resolved["meta"]["primary"], first)


@tag("notifications")
class OutboxTransactionTests(ApiTestCase):
    """Enqueue is part of the business transaction, not a post-commit extra."""

    def test_the_row_is_written_inside_the_caller_transaction(self):
        # Deliberately NOT inside captureOnCommitCallbacks: an enqueue
        # registered on transaction.on_commit would leave this assertion
        # looking at an empty table, which is the whole difference between
        # the two placements.
        with transaction.atomic():
            order = _make_order(self.make_user("outboxtx"))
            notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
            # Still inside the block, locks hypothetically held: the intent is
            # already durable in the same transaction as the write.
            self.assertEqual(NotificationOutbox.objects.count(), 1)
            self.assertEqual(Order.objects.count(), 1)

    def test_enqueue_rolls_back_with_the_business_write(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                order = _make_order(self.make_user("outboxrb"))
                notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
                raise RuntimeError("later in the money block failed")
        # Neither survives. The dual-write this task removes is an order that
        # commits with its notification missing, not two independent outcomes.
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    def test_enqueue_commits_with_the_business_write(self):
        # Paired half of the rollback test. The committed-row proof proper
        # needs a transaction that really commits, which TestCase's wrapper
        # cannot give — see OutboxCommitVisibilityTests for that.
        with transaction.atomic():
            order = _make_order(self.make_user("outboxcommit"))
            notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        self.assertEqual(Order.objects.count(), 1)

    def test_a_failed_enqueue_propagates_instead_of_being_swallowed(self):
        # The deliberate divergence from dispatch, which logs and swallows an
        # SMTP failure. Here the write IS the transaction's own record of what
        # it owes, so swallowing a database failure would recreate the dual
        # write. The checkout rolls back, visibly, instead.
        order = _make_order(self.make_user("outboxfail"))
        with mock.patch(
            "common.notifications.NotificationOutbox.objects.create",
            side_effect=DatabaseError("outbox table missing"),
        ):
            with self.assertRaises(DatabaseError):
                notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})


@tag("notifications")
class OutboxDedupTests(ApiTestCase):
    """The enqueue-side guard: one queued row per notification identity.

    What this key is NOT: the drain loop's "already handled" guard. It is
    derived only from the event type and the payload, is byte-identical before
    and after any send, and is unique, so it can never match a second row and
    carries no send state at all. At-least-once delivery is the locked
    status/sent_at claim, which ASYNC-2c2 has not built.
    """

    def test_a_second_enqueue_of_the_same_event_is_a_no_op(self):
        order = _make_order(self.make_user("outboxdup"))
        first = notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        with self.assertLogs("common.notifications", level="INFO") as logs:
            second = notifications.enqueue(
                AuditEvent.EventType.ORDER_PAID, {"order": order}
            )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        self.assertIn("already queued", "\n".join(logs.output))

    def test_the_key_survives_a_change_to_the_referenced_row(self):
        # The property the key's stability exists for: the two facts it is
        # built from (the event type, and the label+pk of the referenced row)
        # cannot change, so a retry after the order was corrected still
        # collides. A key derived from the order's total, its rendered subject
        # or its repr would miss and queue a duplicate.
        order = _make_order(self.make_user("outboxkey"))
        first = notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        order.total_amount = Decimal("999.00")
        order.save(update_fields=["total_amount"])
        second = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID, {"order": order}
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(NotificationOutbox.objects.count(), 1)

    def test_the_key_names_the_event_and_the_row_it_is_about(self):
        # A shape check by hand-written substrings, NOT by rebuilding the key
        # from the code under test: a key naming neither the event nor its
        # subject would still collide correctly for one event and still be
        # useless to whoever reads the table.
        order = _make_order(self.make_user("outboxshape"))
        row = notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        self.assertIn("order.paid", row.dedup_key)
        self.assertIn("orders.order", row.dedup_key)
        self.assertIn(str(order.pk), row.dedup_key)

    def test_the_key_does_not_depend_on_the_order_the_context_was_built_in(self):
        # The two paths that fire for one payment are different call sites, and
        # a caller may build its context either way round. The previous key
        # appended scalars in insertion order, so the same notification built
        # differently produced two keys and both rows were queued - defeating
        # the exact collapse the key exists for.
        self.assertEqual(
            notifications._dedup_key("order.paid", {"a": "1", "b": "2"}),
            notifications._dedup_key("order.paid", {"b": "2", "a": "1"}),
        )

    def test_two_distinct_contexts_do_not_collide_on_a_separator(self):
        # The previous key was a ":".join of rendered fragments, so an
        # unescaped separator made these two different notifications share one
        # key - and the second was silently swallowed, because the unique
        # constraint read it as "already queued".
        self.assertNotEqual(
            notifications._dedup_key("order.paid", {"a": "1", "b": "2"}),
            notifications._dedup_key("order.paid", {"a": "1:b=2"}),
        )

    def test_distinct_referenced_rows_get_distinct_rows(self):
        first_order = _make_order(self.make_user("outboxa"))
        second_order = _make_order(self.make_user("outboxb"))
        self.assertIsNotNone(
            notifications.enqueue(
                AuditEvent.EventType.ORDER_PAID, {"order": first_order}
            )
        )
        self.assertIsNotNone(
            notifications.enqueue(
                AuditEvent.EventType.ORDER_PAID, {"order": second_order}
            )
        )
        self.assertEqual(NotificationOutbox.objects.count(), 2)

    def test_the_default_occurrence_is_the_one_off_case(self):
        # Left at None, the key says "this business event owes one
        # notification" - which is right for a confirmation.
        order = _make_order(self.make_user("outboxonce"))
        self.assertIsNotNone(
            notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        )
        self.assertIsNone(
            notifications.enqueue(
                AuditEvent.EventType.ORDER_PAID, {"order": order}, occurrence=None
            )
        )
        self.assertEqual(NotificationOutbox.objects.count(), 1)

    def test_a_repeat_notification_is_expressible_and_gets_its_own_row(self):
        # Before this existed, a resend after a mis-send and a second reminder
        # were structurally impossible: same type, same row, same scalars
        # meant enqueue returned None forever - no nonce, no attempt
        # discriminator, no force, no key retirement. An occurrence makes the
        # repeat a NEW row with its own send state, which is what "this
        # notification is still owed" actually means, rather than a mutation
        # of the first.
        order = _make_order(self.make_user("outboxrepeat"))
        first = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID, {"order": order}, occurrence="reminder-1"
        )
        again = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID, {"order": order}, occurrence="reminder-1"
        )
        second = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID, {"order": order}, occurrence="reminder-2"
        )
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertIsNotNone(second)
        self.assertEqual(NotificationOutbox.objects.count(), 2)
        self.assertNotEqual(first.dedup_key, second.dedup_key)

    def test_a_repeat_does_not_collide_with_the_one_off_row(self):
        order = _make_order(self.make_user("outboxmixed"))
        notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        repeat = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID, {"order": order}, occurrence="resend-1"
        )
        self.assertIsNotNone(repeat)
        self.assertEqual(NotificationOutbox.objects.count(), 2)

    def test_a_row_is_created_pending_and_unsent(self):
        order = _make_order(self.make_user("outboxstate"))
        row = notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        self.assertEqual(row.status, NotificationOutbox.Status.PENDING)
        self.assertIsNone(row.sent_at)
        self.assertIn(AuditEvent.EventType.ORDER_PAID, str(row))
        self.assertIn("pending", str(row))

    def test_an_event_with_no_registered_handler_writes_no_row(self):
        # The registry-hook events (order.shipped and friends) resolve no
        # handler yet, and dispatch no-ops on them. Enqueue must agree, or
        # converting such a site would queue rows nothing knows how to send.
        with self.assertLogs("common.notifications", level="DEBUG") as logs:
            self.assertIsNone(notifications.enqueue("order.shipped", {"order": None}))
        self.assertEqual(NotificationOutbox.objects.count(), 0)
        self.assertIn("no notification registered", "\n".join(logs.output))


@tag("notifications")
class OutboxRetentionTests(ApiTestCase):
    """ASYNC-2c1 cycle 2: the control that makes the unsanitised payload safe.

    The payload is deliberately NOT run through the audit trail's
    credential-field scrubber, because a password-reset or verification
    notification is built out of exactly the one-time token material that
    scrubber drops. That decision is only defensible with a compensating
    control in place, so the bound and the deletion owner exist now rather
    than being deferred to the call-site conversions.
    """

    def test_every_row_carries_an_expiry_inside_the_configured_bound(self):
        order = _make_order(self.make_user("outboxttl"))
        before = timezone.now()
        with override_settings(NOTIFICATION_OUTBOX_TTL_SECONDS=120):
            row = notifications.enqueue(
                AuditEvent.EventType.ORDER_PAID, {"order": order}
            )
        after = timezone.now()
        # The bound is the field's own default, so no code path can create a
        # row without one - there is nowhere to forget it.
        self.assertGreaterEqual(row.expires_at, before + timedelta(seconds=120))
        self.assertLessEqual(row.expires_at, after + timedelta(seconds=120))

    def test_the_purge_command_deletes_expired_rows_and_keeps_live_ones(self):
        spent = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": _make_order(self.make_user("outboxpurged"))},
        )
        live = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": _make_order(self.make_user("outboxlive"))},
            occurrence="still-owed",
        )
        self._expire(spent)
        call_command("purge_notification_outbox", verbosity=0)
        self.assertFalse(NotificationOutbox.objects.filter(pk=spent.pk).exists())
        # A row inside its window is a notification still owed, so the purge
        # must never be the reason a customer stops hearing about an order.
        self.assertTrue(NotificationOutbox.objects.filter(pk=live.pk).exists())

    def test_the_purge_command_deletes_a_stranded_pending_row_too(self):
        # Nothing drains the table until ASYNC-2c2, so an expired PENDING row
        # is stranded rather than finished. Its payload may hold credential
        # material, so the bound does not wait for a status to change.
        stranded = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": _make_order(self.make_user("outboxstranded"))},
        )
        self.assertEqual(stranded.status, NotificationOutbox.Status.PENDING)
        self._expire(stranded)
        call_command("purge_notification_outbox", verbosity=0)
        self.assertFalse(NotificationOutbox.objects.filter(pk=stranded.pk).exists())

    def test_the_purge_command_is_idempotent(self):
        row = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": _make_order(self.make_user("outboxidem"))},
        )
        self._expire(row)
        call_command("purge_notification_outbox", verbosity=0)
        call_command("purge_notification_outbox", verbosity=0)
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    def test_the_purge_command_dry_run_deletes_nothing(self):
        row = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": _make_order(self.make_user("outboxdry"))},
        )
        self._expire(row)
        out = StringIO()
        call_command("purge_notification_outbox", dry_run=True, stdout=out)
        self.assertIn("would delete 1", out.getvalue())
        self.assertTrue(NotificationOutbox.objects.filter(pk=row.pk).exists())

    def test_the_purge_command_honours_a_grace_window_and_never_subtracts(self):
        expired = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": _make_order(self.make_user("outboxgrace"))},
        )
        self._expire(expired)
        # Expired, but only just: a grace window must keep it.
        call_command("purge_notification_outbox", grace_seconds=3600, verbosity=0)
        self.assertTrue(NotificationOutbox.objects.filter(pk=expired.pk).exists())
        # A second row that is NOT expired yet, and expires in a minute. The
        # negative grace is clamped at zero, so it cannot move the cutoff
        # forward past this row and delete a notification that is still owed.
        #
        # The row's expiry is set forward explicitly because the default
        # three-day bound is far longer than any plausible grace value: with
        # the bound left alone, a clamped and an unclamped cutoff would both
        # keep the row and the assertion below could not tell them apart. The
        # previous version of this test asserted the opposite thing on an
        # already-expired row, so its comment described one property and its
        # assertion another.
        live = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": _make_order(self.make_user("outboxnotexpired"))},
            occurrence="inside-its-window",
        )
        NotificationOutbox.objects.filter(pk=live.pk).update(
            expires_at=timezone.now() + timedelta(seconds=60)
        )
        call_command("purge_notification_outbox", grace_seconds=-3600, verbosity=0)
        self.assertTrue(NotificationOutbox.objects.filter(pk=live.pk).exists())
        self.assertFalse(NotificationOutbox.objects.filter(pk=expired.pk).exists())

    def test_a_missing_bound_falls_back_to_the_documented_default(self):
        # A deployment that never sets the key still gets a bounded expiry
        # rather than an unbounded one.
        self.assertEqual(NOTIFICATION_OUTBOX_DEFAULT_TTL_SECONDS, 259200)
        with override_settings():
            del settings.NOTIFICATION_OUTBOX_TTL_SECONDS
            expiry = default_notification_outbox_expiry()
        self.assertGreater(expiry, timezone.now() + timedelta(seconds=259200 - 60))

    def _expire(self, row):
        NotificationOutbox.objects.filter(pk=row.pk).update(
            expires_at=timezone.now() - timedelta(seconds=5)
        )


@tag("notifications")
class OutboxResolveFailureTests(ApiTestCase):
    """A row that has since gone away fails in a defined, catchable way."""

    def test_a_deleted_reference_raises_a_defined_error(self):
        order = _make_order(self.make_user("outboxgone"))
        row = notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        order.delete()
        with self.assertRaises(UnresolvableNotification) as caught:
            notifications.resolve_context(row.payload)
        self.assertIn("orders.order", str(caught.exception))

    def test_an_unregistered_model_label_raises_a_defined_error(self):
        payload = {"order": {"label": "nosuchapp.NoSuchModel", "pk": 1}}
        with self.assertRaises(UnresolvableNotification) as caught:
            notifications.resolve_context(payload)
        self.assertIn("nosuchapp.NoSuchModel", str(caught.exception))

    def test_a_label_the_registry_cannot_parse_raises_the_defined_error(self):
        # The gap this closes. apps.get_model signals an unparseable label with
        # ValueError, not LookupError - "a.b.c" unpacks into three parts there -
        # so catching only LookupError let it escape uncaught. That is the one
        # failure a drain loop filtering on UnresolvableNotification cannot
        # survive, and it is reachable from a hand-edited or migrated row, which
        # is exactly what _load_reference's docstring claimed was in scope.
        payload = {"order": {"label": "a.b.c", "pk": 1}}
        with self.assertRaises(UnresolvableNotification) as caught:
            notifications.resolve_context(payload)
        self.assertIn("a.b.c", str(caught.exception))
        # And the one class a drain loop can catch, provably not a ValueError.
        self.assertNotIsInstance(caught.exception, ValueError)

    def test_an_unaddressable_primary_key_raises_a_defined_error(self):
        payload = {"order": {"label": "orders.order", "pk": "not-a-pk"}}
        with self.assertRaises(UnresolvableNotification) as caught:
            notifications.resolve_context(payload)
        self.assertIn("not-a-pk", str(caught.exception))

    def test_resolution_reads_current_state_not_enqueue_time_state(self):
        # Nothing pre-rendered is stored, so the subject and body are built at
        # drain time from the live row and current settings. A snapshotting
        # enqueue (storing the total, or a rendered subject) would hand the
        # handler the corrected-away value here.
        order = _make_order(self.make_user("outboxamend"))
        row = notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        order.total_amount = Decimal("750.25")
        order.save(update_fields=["total_amount"])
        resolved = notifications.resolve_context(row.payload)
        self.assertEqual(resolved["order"].total_amount, Decimal("750.25"))
        with self.assertLogs("common.notifications", level="INFO"):
            notifications.dispatch(AuditEvent.EventType.ORDER_PAID, resolved)
        self.assertIn("750.25", mail.outbox[-1].body)


@tag("notifications")
class NoCallSiteConvertedTests(ApiTestCase):
    """ASYNC-2c3 has not happened. This is what must still be true.

    The dark regression this task is sized to avoid: converting a site to
    ``enqueue`` before ASYNC-2c2 lands a drain loop means the customer is
    never emailed, and a suite that had been rewritten to count queue rows
    would report it green. These tests keep the assertion on the customer's
    side of the wire — the mail — while the substrate waits for its worker.
    """

    def setUp(self):
        self.user = self.make_user("outboxproof")
        _, self.token = self.api_login(username=self.user.username)
        self.product = self.make_product(stock=5)

    def test_verify_payment_still_sends_inline_and_queues_nothing(self):
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        order_id = res.data["id"]
        self.razorpay_mock()
        self.client.post("/api/orders/payment/", {"order_id": order_id}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                "/api/orders/payment/verify/",
                {
                    "razorpay_order_id": "order_TEST0001",
                    "razorpay_payment_id": "pay_TEST0001",
                    "razorpay_signature": "sig",
                    "order_id": order_id,
                },
                format="json",
            )
        self.assertEqual(res.status_code, 200, res.data)
        # The mail arrives, and the outbox is untouched — the queue is a
        # substrate, not yet a participant.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    def test_a_lifecycle_transition_still_dispatches_inline(self):
        from orders.events import notify_transition

        order = _make_order(self.user)
        with mock.patch("orders.events.notifications.dispatch") as dispatch_mock:
            notify_transition(order, "confirmed", "shipped")
        dispatch_mock.assert_called_once_with("order.shipped", {"order": order})
        self.assertEqual(NotificationOutbox.objects.count(), 0)


class OutboxCommitVisibilityTests(TransactionTestCase):
    """The committed-row proof, over a transaction that really commits."""

    def test_a_committed_enqueue_is_readable_by_a_fresh_query(self):
        with transaction.atomic():
            order = Order.objects.create(
                full_name="Fresh Query Buyer",
                phone="9876543210",
                address="12 Rose Lane",
                city="Mumbai",
                state="Maharashtra",
                pincode="400001",
                total_amount=Decimal("499.99"),
                guest_email="fresh-query@example.com",
                guest_token="fresh-query-token",
            )
            notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        # No ambient transaction here, so this read is genuinely post-COMMIT.
        self.assertFalse(transaction.get_connection().in_atomic_block)
        row = NotificationOutbox.objects.get()
        self.assertEqual(row.event_type, AuditEvent.EventType.ORDER_PAID)
        self.assertEqual(
            row.payload, {"order": {"label": "orders.order", "pk": order.pk}}
        )

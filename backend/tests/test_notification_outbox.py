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
"""

import json
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase, tag
from django.utils.translation import gettext_lazy

from common import notifications
from common.models import AuditEvent, NotificationOutbox
from common.notifications import UnresolvableNotification
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

    def test_an_over_long_value_is_bounded_and_marked_as_cut(self):
        row = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID,
            {"order": _make_order(self.make_user("outboxlong")), "note": "x" * 300},
        )
        self.assertEqual(
            row.payload["note"],
            "x" * notifications.PAYLOAD_VALUE_MAX_LENGTH + "…",
        )

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
    """The at-least-once guard: one key per logical notification."""

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
        # The property that makes the key usable by ASYNC-2c2: the two facts
        # it is built from (event type, and the label+pk of the referenced
        # row) cannot change, so a retry after the order was corrected still
        # collides. A key built from the order's total, its rendered subject
        # or its repr would miss and queue a duplicate email.
        order = _make_order(self.make_user("outboxkey"))
        first = notifications.enqueue(AuditEvent.EventType.ORDER_PAID, {"order": order})
        order.total_amount = Decimal("999.00")
        order.save(update_fields=["total_amount"])
        second = notifications.enqueue(
            AuditEvent.EventType.ORDER_PAID, {"order": order}
        )
        self.assertIsNone(second)
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        self.assertEqual(
            NotificationOutbox.objects.get().dedup_key,
            f"order.paid:order=orders.order:{order.pk}",
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

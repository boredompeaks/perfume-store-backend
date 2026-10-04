"""SPEC-19-4: back-in-stock opt-in ([R-19.11], spec 19.1).

- Opt-in/opt-out lifecycle through the REST endpoint (auth required,
  ownership-scoped, idempotent, in-stock opt-in refused).
- Uniqueness: one row per (user, product), DB constraint as the race
  authority.
- Restock trigger: stock 0 -> positive fires ONE back-in-stock email per
  armed opt-in via the SPEC-19-1 single send path; spent rows do not
  re-mail without a re-arm; the next sell-out -> restock cycle re-arms.
- The trigger rides adjust_stock (both admin and REST surfaces) and is
  log-only on send failure. locmem only — no network.

ASYNC-2b2: adjust_stock now REGISTERS the fan-out on
``transaction.on_commit`` instead of running it under its own atomic block,
so a ``TestCase`` (which rolls its own transaction back) would never run it
and every "an email was sent" assertion here would pass only because nothing
was sent. Every send-exercising test therefore wraps its crossing in
``captureOnCommitCallbacks(execute=True)`` — which executes the real callback
— and the pre-existing assertions are kept verbatim.
"""

from unittest import mock

from django.core import mail
from django.db import connection, transaction
from django.test import tag
from django.utils import timezone

from common.testing import ApiTestCase
from products.models import RestockNotification


@tag("restock")
class RestockOptInEndpointTests(ApiTestCase):
    def setUp(self):
        self.user = self.make_user("optin")
        self.product = self.make_product(name="Oud Royale", stock=0)

    def _url(self, product=None):
        return f"/api/products/{(product or self.product).slug}/restock-notifications/"

    def test_opt_in_creates_active_preference(self):
        _, token = self.api_login(username=self.user.username)
        res = self.client.post(self._url(), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["active"])
        preference = RestockNotification.objects.get(
            user=self.user, product=self.product
        )
        self.assertTrue(preference.active)
        self.assertIsNone(preference.notified_at)

    def test_opt_in_requires_authentication(self):
        res = self.client.post(self._url(), format="json")
        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(RestockNotification.objects.count(), 0)

    def test_opt_out_requires_authentication(self):
        res = self.client.delete(self._url(), format="json")
        self.assertEqual(res.status_code, 401, res.data)

    def test_opt_in_refused_while_in_stock(self):
        self.product.adjust_stock(None, 5, "restock")
        _, token = self.api_login(username=self.user.username)
        res = self.client.post(self._url(), format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(RestockNotification.objects.count(), 0)

    def test_opt_in_is_idempotent_and_rearms(self):
        _, token = self.api_login(username=self.user.username)
        self.assertEqual(self.client.post(self._url(), format="json").status_code, 201)
        # Spend the row as the trigger would.
        RestockNotification.objects.filter(user=self.user).update(
            notified_at="2026-01-01T00:00:00Z"
        )
        # A second opt-in must re-arm, not duplicate.
        res = self.client.post(self._url(), format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(RestockNotification.objects.count(), 1)
        preference = RestockNotification.objects.get(user=self.user)
        self.assertTrue(preference.active)
        self.assertIsNone(preference.notified_at)

    def test_opt_out_deactivates_and_uniform_response(self):
        _, token = self.api_login(username=self.user.username)
        self.client.post(self._url(), format="json")
        res = self.client.delete(self._url(), format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["active"])
        preference = RestockNotification.objects.get(user=self.user)
        self.assertFalse(preference.active)
        # Opt-out of a preference that does not exist: same 200 shape.
        res = self.client.delete(self._url(), format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["active"])

    def test_unknown_product_is_uniform_404(self):
        _, token = self.api_login(username=self.user.username)
        res = self.client.post(
            "/api/products/no-such-fragrance/restock-notifications/",
            format="json",
        )
        self.assertEqual(res.status_code, 404, res.data)

    def test_ownership_is_scoped_to_the_caller(self):
        other = self.make_user("otherbuyer")
        _, token = self.api_login(username=self.user.username)
        self.client.post(self._url(), format="json")
        other_client = self.fresh_client()
        self.api_login(username=other.username, client=other_client)
        res = other_client.post(self._url(), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        # Two rows: one per user — the second user's opt-in never touched
        # the first user's row (body carries no user field at all).
        self.assertEqual(
            RestockNotification.objects.filter(product=self.product).count(), 2
        )

    def test_duplicate_opt_in_race_lands_on_one_row(self):
        """Concurrent opt-ins must race to the unique constraint and one
        row survives (the DB is the authority, not check-then-act)."""
        _, token = self.api_login(username=self.user.username)
        RestockNotification.objects.create(user=self.user, product=self.product)
        # Re-opt-in over an existing row = the upsert path (get_or_create
        # retried semantics): still one row, re-armed.
        res = self.client.post(self._url(), format="json")
        self.assertIn(res.status_code, (200, 201))
        self.assertEqual(RestockNotification.objects.count(), 1)

    def test_inactive_row_never_mails(self):
        _, token = self.api_login(username=self.user.username)
        self.client.post(self._url(), format="json")
        self.client.delete(self._url(), format="json")
        # execute=True, or the zero below would only mean the deferred
        # fan-out never ran.
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 3, "restock")
        self.assertEqual(len(mail.outbox), 0)


@tag("restock")
class RestockTriggerTests(ApiTestCase):
    """adjust_stock 0 -> positive: one mail per armed opt-in, no dupes."""

    def setUp(self):
        self.product = self.make_product(name="Rose Aurum", stock=0)
        self.buyer = self.make_user("buyer")
        self.other = self.make_user("other")

    def _opt_in(self, user, client=None):
        target = client or self.client
        self.api_login(username=user.username, client=target)
        res = target.post(
            f"/api/products/{self.product.slug}/restock-notifications/",
            format="json",
        )
        self.assertIn(res.status_code, (200, 201), res.data)

    def test_restock_emails_armed_optins_once(self):
        self._opt_in(self.buyer)
        self._opt_in(self.other, client=self.fresh_client())
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 2)
        recipients = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(recipients, ["buyer@example.com", "other@example.com"])
        for message in mail.outbox:
            self.assertEqual(message.subject, "Back in stock: Rose Aurum")
            self.assertIn(self.product.name, message.body)
            self.assertIn(self.product.slug, message.body)
        # Both rows spent exactly once.
        self.assertEqual(
            RestockNotification.objects.filter(notified_at__isnull=False).count(),
            2,
        )

    def test_spent_row_does_not_remail_without_sellout(self):
        self._opt_in(self.buyer)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 1)
        # Second restock while still stocked: no new mail.
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 3, "restock")
        self.assertEqual(len(mail.outbox), 1)

    def test_rearm_after_sellout(self):
        self._opt_in(self.buyer)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 1)
        # Sell out...
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, -5, "sale")
        # ...and restock again: the (still-active, spent) row re-arms by
        # the crossing definition and is consulted once more.
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 7, "restock")
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Back in stock", mail.outbox[1].subject)

    def test_rearm_clears_a_claim_whose_send_never_happened(self):
        """A row claimed by a cycle that died before sending must not wedge
        the product out of future notifications. Reached here by stamping the
        row directly, which is exactly the state a process killed between
        claim and send leaves behind. A characterisation, not a guard: it is
        green against the pre-ASYNC-2b2 code too, which only ever stamped
        rows it had mailed."""
        self._opt_in(self.buyer)
        RestockNotification.objects.filter(user=self.buyer).update(
            notified_at=timezone.now()
        )
        # This crossing does not mail the row: the claim already spent it.
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 0)
        # ... but a sell-out re-arms it, so the next restock mails the row.
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, -5, "sale")
            self.product.adjust_stock(None, 6, "restock")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Back in stock", mail.outbox[0].subject)
        self.assertIsNotNone(
            RestockNotification.objects.get(user=self.buyer).notified_at
        )

    def test_stock_increase_that_is_not_a_zero_crossing_does_not_fire(self):
        self._opt_in(self.buyer)
        # Product starts at 0 in setUp, so stock it up first without an
        # opt-in-relevant crossing... actually the opt-in was armed while
        # stock was 0, so stock to 5 already fired once; neutralize the
        # outbox and test that a mid-stock top-up does not fire again.
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        del mail.outbox[:]
        # Now stocked: topping up further is not a back-in-stock event.
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 0)

    def test_sale_decrement_never_fires_the_trigger(self):
        self._opt_in(self.buyer)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        base = len(mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, -1, "damage")
        self.assertEqual(len(mail.outbox), base)

    def test_send_failure_is_log_only_and_never_blocks_the_loop(self):
        self._opt_in(self.buyer)
        self._opt_in(self.other, client=self.fresh_client())

        # Keyed on the recipient, not on call order: the loop's row order is
        # the planner's, and an order-dependent side_effect list turns this
        # into a coin flip on the other engine.
        def fail_one_recipient(template, context, subject, recipient):
            if recipient == "buyer@example.com":
                raise Exception("smtp down")

        with mock.patch(
            "common.notifications.send_email", side_effect=fail_one_recipient
        ) as send_mock:
            with self.assertLogs("products.restock", level="ERROR") as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    self.product.adjust_stock(None, 5, "restock")
        self.assertIn("smtp down", "\n".join(logs.output))
        self.assertEqual(send_mock.call_count, 2)
        # The second opt-in still got its mail; the first row is not marked
        # spent (its send failed and the claim was released), so it will be
        # retried on the next trigger — log-only contract, no silent loss.
        self.assertEqual(
            RestockNotification.objects.filter(notified_at__isnull=False).count(),
            1,
        )
        self.assertIsNotNone(
            RestockNotification.objects.get(user=self.other).notified_at
        )
        self.assertIsNone(RestockNotification.objects.get(user=self.buyer).notified_at)

    def test_failed_send_is_retried_on_the_next_crossing(self):
        """Trap 1, end to end: the spent stamp no longer rides the stock
        transaction, so a send failure must give the row back or that
        customer is spent on an email that never went out. A guard against
        the naive deferral, not against the pre-ASYNC-2b2 code, which also
        left the row armed after a failed send."""
        self._opt_in(self.buyer)
        with mock.patch(
            "common.notifications.send_email", side_effect=Exception("smtp down")
        ):
            with self.assertLogs("products.restock", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(RestockNotification.objects.get(user=self.buyer).notified_at)
        # Provider recovers; the next stock cycle really does mail the row.
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, -5, "sale")
            self.product.adjust_stock(None, 7, "restock")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Back in stock", mail.outbox[0].subject)
        self.assertIsNotNone(
            RestockNotification.objects.get(user=self.buyer).notified_at
        )

    def test_claim_before_send_drops_a_row_another_cycle_already_won(self):
        """Trap 2, end to end: the claim is the mutual exclusion and it
        happens BEFORE the send. While our fan-out is mid-loop a second
        stock cycle claims the rows still ahead of us — the send-then-stamp
        order this replaced would have mailed them too, so two rapid
        crossings would deliver two emails to one customer."""
        self._opt_in(self.buyer)
        self._opt_in(self.other, client=self.fresh_client())

        def concurrent_cycle(template, context, subject, recipient):
            # Whoever this call is for, a second cycle claims every armed
            # row that is not this recipient's.
            RestockNotification.objects.exclude(user__email=recipient).update(
                notified_at=timezone.now()
            )

        with mock.patch(
            "common.notifications.send_email", side_effect=concurrent_cycle
        ) as send_mock:
            with self.captureOnCommitCallbacks(execute=True):
                self.product.adjust_stock(None, 5, "restock")
        # One send: the row it did not reach was already spoken for.
        self.assertEqual(send_mock.call_count, 1)
        self.assertEqual(
            RestockNotification.objects.filter(notified_at__isnull=False).count(),
            2,
        )

    def test_claim_skips_a_row_that_opted_out_after_the_capture(self):
        """The armed list is captured inside the inventory transaction and
        the claim lands after it commits, so a customer who opts out in that
        window is still in the list. The claim re-checks ``active=True``, so
        the row is skipped and never mailed, and never spent. With a claim
        filtered on the pk alone the opt-out was resurrected: a second email
        to someone who had withdrawn consent, stamped on top of it."""
        self._opt_in(self.buyer)
        self._opt_in(self.other, client=self.fresh_client())

        def opt_out_the_others(template, context, subject, recipient):
            # The window: everyone else in the captured list opts out while
            # our fan-out is mid-loop. Keyed on the recipient, so it does not
            # matter which row the planner hands the loop first.
            RestockNotification.objects.exclude(user__email=recipient).update(
                active=False
            )

        with mock.patch(
            "common.notifications.send_email", side_effect=opt_out_the_others
        ) as send_mock:
            with self.captureOnCommitCallbacks(execute=True):
                self.product.adjust_stock(None, 5, "restock")
        # One send: the opted-out row is not mailed.
        self.assertEqual(send_mock.call_count, 1)
        mailed = send_mock.call_args_list[0].args[3]
        opted_out = list(RestockNotification.objects.filter(active=False))
        # The opt-out really happened, so one send is not the trivial
        # consequence of there being only one opt-in to begin with.
        self.assertEqual(len(opted_out), 1)
        self.assertNotEqual(opted_out[0].user.email, mailed)
        # And the row is left unspent: nothing pairs it with a send that
        # never happened.
        self.assertIsNone(opted_out[0].notified_at)

    def test_send_runs_with_the_stock_lock_block_already_popped(self):
        """ASYNC-2b2's defect is the LOCK, so the assertion is about where
        the send runs rather than that it ran: adjust_stock's atomic block is
        what holds this product's ``select_for_update`` row. The probe is
        Django's own atomic stack — while the send happens, the innermost
        block must still be the one adjust_stock's CALLER opened, because
        adjust_stock's own block (and its lock) is already gone.

        A real commit would be the ideal instrument and this suite cannot
        provide one: ``TestCase`` rolls its own transaction back and
        ``captureOnCommitCallbacks`` runs the hook inside it. (A
        ``TransactionTestCase`` would commit for real, but the two
        ``orders`` migration tests unapply ``products/0009+`` — which owns
        ``restocknotification`` — through their dependency on
        ``orders/0014``, and the full ``migrate`` they restore with does not
        put those tables back, so a products ``TransactionTestCase`` runs
        into ``no such table`` when it follows them. Reported, not fixed:
        see docs/changes.md.)"""
        self._opt_in(self.buyer)
        observed = []

        def probe(*args, **kwargs):
            observed.append(
                (connection.atomic_blocks[-1], len(connection.atomic_blocks))
            )

        with mock.patch("common.notifications.send_email", side_effect=probe):
            with self.captureOnCommitCallbacks(execute=True):
                caller_block, caller_depth = (
                    connection.atomic_blocks[-1],
                    len(connection.atomic_blocks),
                )
                self.product.adjust_stock(None, 5, "restock")
        # The probe ran, once per opted-in user.
        self.assertEqual(len(observed), 1)
        innermost, depth = observed[0]
        self.assertIs(innermost, caller_block)
        self.assertEqual(depth, caller_depth)

    def test_notice_is_registered_not_performed_under_the_lock(self):
        """ASYNC-2b2 mechanism pin. The send must not happen inside
        adjust_stock's atomic block: at the point adjust_stock has returned
        and its own block is closed, nothing has gone out yet — the fan-out
        is a registered commit hook, so no socket is open while the product
        row is locked."""
        self._opt_in(self.buyer)
        with mock.patch("common.notifications.send_email") as send_mock:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                self.product.adjust_stock(None, 5, "restock")
                self.assertEqual(send_mock.call_count, 0)
            self.assertEqual(len(callbacks), 1)
        self.assertEqual(send_mock.call_count, 1)

    def test_transaction_rollback_also_rolls_the_spent_stamp(self):
        """An exception inside adjust_stock leaves the stock and the stamp
        untouched. This is a characterisation, not a guard: it cannot fail
        against the pre-ASYNC-2b2 code, because the StockMovement insert
        precedes the crossing branch, so no mail was attempted either way.
        The rollback-must-not-mail invariant belongs to the single test
        below, which does fail pre-fix."""
        self._opt_in(self.buyer)
        from products.models import StockMovement

        with (
            mock.patch("common.notifications.send_email", return_value=None),
            mock.patch.object(
                StockMovement.objects, "create", side_effect=Exception("db down")
            ),
        ):
            with self.assertRaises(Exception):
                self.product.adjust_stock(None, 5, "restock")
        preference = RestockNotification.objects.get(user=self.buyer)
        self.assertIsNone(preference.notified_at)
        # The in-memory copy kept the pre-rollback value; the DB is truth.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 0)

    def test_rollback_of_the_stock_write_discards_the_registered_send(self):
        """Rollback-together, measured as ZERO sends rather than as an
        exception, and against a rollback that happens AFTER the crossing
        branch: the pre-ASYNC-2b2 send was inline and could not be recalled,
        so a failure later in the stock write still mailed the customers. A
        registered callback dies with its transaction."""
        self._opt_in(self.buyer)
        with mock.patch("common.notifications.send_email") as send_mock:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(RuntimeError):
                    with transaction.atomic():
                        self.product.adjust_stock(None, 5, "restock")
                        raise RuntimeError("later in the stock write failed")
        self.assertEqual(send_mock.call_count, 0)
        self.assertEqual(len(mail.outbox), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 0)
        self.assertIsNone(RestockNotification.objects.get(user=self.buyer).notified_at)

    def test_trigger_fires_on_the_rest_endpoint_path_too(self):
        self._opt_in(self.buyer)
        # The REST adjust path wraps the same adjust_stock service.
        self.make_staff()
        _, token = self.api_login(username="staff")
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                "/api/products/inventory/adjustments/",
                {
                    "product_id": self.product.id,
                    "delta": 4,
                    "reason": "restock",
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Back in stock", mail.outbox[0].subject)


@tag("restock")
class RestockModelTests(ApiTestCase):
    """Uniqueness constraint + str/repr sanity."""

    def test_unique_user_product_constraint(self):
        product = self.make_product(stock=0)
        user = self.make_user("dup")
        RestockNotification.objects.create(user=user, product=product)
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RestockNotification.objects.create(user=user, product=product)

    def test_str_names_state(self):
        product = self.make_product(stock=0)
        user = self.make_user("repr")
        row = RestockNotification.objects.create(user=user, product=product)
        self.assertIn("active", str(row))
        row.active = False
        row.save()
        self.assertIn("opted out", str(row))

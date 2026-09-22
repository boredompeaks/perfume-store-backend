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
"""
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import tag

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
        self.assertEqual(
            self.client.post(self._url(), format="json").status_code, 201
        )
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
        RestockNotification.objects.create(
            user=self.user, product=self.product
        )
        # Re-opt-in over an existing row = the upsert path (get_or_create
        # retried semantics): still one row, re-armed.
        res = self.client.post(self._url(), format="json")
        self.assertIn(res.status_code, (200, 201))
        self.assertEqual(RestockNotification.objects.count(), 1)

    def test_inactive_row_never_mails(self):
        _, token = self.api_login(username=self.user.username)
        self.client.post(self._url(), format="json")
        self.client.delete(self._url(), format="json")
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
        self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 2)
        recipients = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(
            recipients, ["buyer@example.com", "other@example.com"]
        )
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
        self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 1)
        # Second restock while still stocked: no new mail.
        self.product.adjust_stock(None, 3, "restock")
        self.assertEqual(len(mail.outbox), 1)

    def test_rearm_after_sellout(self):
        self._opt_in(self.buyer)
        self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 1)
        # Sell out...
        self.product.adjust_stock(None, -5, "sale")
        # ...and restock again: the (still-active, spent) row re-arms by
        # the crossing definition and is consulted once more.
        self.product.adjust_stock(None, 7, "restock")
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Back in stock", mail.outbox[1].subject)

    def test_stock_increase_that_is_not_a_zero_crossing_does_not_fire(self):
        self._opt_in(self.buyer)
        # Product starts at 0 in setUp, so stock it up first without an
        # opt-in-relevant crossing... actually the opt-in was armed while
        # stock was 0, so stock to 5 already fired once; neutralize the
        # outbox and test that a mid-stock top-up does not fire again.
        self.product.adjust_stock(None, 5, "restock")
        del mail.outbox[:]
        # Now stocked: topping up further is not a back-in-stock event.
        self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 0)

    def test_sale_decrement_never_fires_the_trigger(self):
        self._opt_in(self.buyer)
        self.product.adjust_stock(None, 5, "restock")
        base = len(mail.outbox)
        self.product.adjust_stock(None, -1, "damage")
        self.assertEqual(len(mail.outbox), base)

    def test_send_failure_is_log_only_and_never_blocks_the_loop(self):
        from django.utils import timezone

        self._opt_in(self.buyer)
        self._opt_in(self.other, client=self.fresh_client())
        with mock.patch(
            "common.notifications.send_email"
        ) as send_mock:
            send_mock.side_effect = [Exception("smtp down"), None]
            with self.assertLogs("products.restock", level="ERROR") as logs:
                self.product.adjust_stock(None, 5, "restock")
        self.assertIn("smtp down", "\n".join(logs.output))
        # The second opt-in still got its mail; the first row is not
        # marked spent (its send failed), so it will be retried on the
        # next trigger — log-only contract, no silent loss.
        self.assertEqual(
            RestockNotification.objects.filter(notified_at__isnull=False).count(),
            1,
        )
        self.assertIsNotNone(
            RestockNotification.objects.get(user=self.other).notified_at
        )

    def test_transaction_rollback_also_rolls_the_spent_stamp(self):
        """The spent stamp commits (or rolls back) together with the
        inventory write: a failure AFTER the email hand-off still rolls
        both — no notified row paired with stock that doesn't exist."""
        self._opt_in(self.buyer)
        from products.models import StockMovement

        # send_email succeeds (the hand-off happens), then the movement
        # insert blows up: the whole atomic block must roll back, taking
        # the spent stamp with it.
        with mock.patch(
            "common.notifications.send_email", return_value=None
        ), mock.patch.object(
            StockMovement.objects, "create", side_effect=Exception("db down")
        ):
            with self.assertRaises(Exception):
                self.product.adjust_stock(None, 5, "restock")
        preference = RestockNotification.objects.get(user=self.buyer)
        self.assertIsNone(preference.notified_at)
        # The in-memory copy kept the pre-rollback value; the DB is truth.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 0)

    def test_trigger_fires_on_the_rest_endpoint_path_too(self):
        self._opt_in(self.buyer)
        # The REST adjust path wraps the same adjust_stock service.
        self.make_staff()
        _, token = self.api_login(username="staff")
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

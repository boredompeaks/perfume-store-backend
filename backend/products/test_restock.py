from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.db import connection, transaction
from django.test import tag
from django.utils import timezone
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle

from common.testing import ApiTestCase
from products.models import RestockNotification, products


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
        RestockNotification.objects.filter(user=self.user).update(
            notified_at="2026-01-01T00:00:00Z"
        )
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

    def test_opt_in_to_delisted_product_is_uniform_with_unknown_slug(self):
        """A delisted product is not an offer, so on this surface it must be indistinguishable from a nonexistent one, or opt-in becomes an existence oracle."""
        products.objects.filter(pk=self.product.pk).update(is_active=False)
        _, token = self.api_login(username=self.user.username)
        delisted = self.client.post(self._url(), format="json")
        unknown = self.client.post(
            "/api/products/no-such-fragrance/restock-notifications/",
            format="json",
        )
        self.assertEqual(delisted.status_code, 404, delisted.data)
        self.assertEqual(delisted.content, unknown.content)
        self.assertEqual(RestockNotification.objects.count(), 0)

    def test_ownership_is_scoped_to_the_caller(self):
        other = self.make_user("otherbuyer")
        _, token = self.api_login(username=self.user.username)
        self.client.post(self._url(), format="json")
        other_client = self.fresh_client()
        self.api_login(username=other.username, client=other_client)
        res = other_client.post(self._url(), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            RestockNotification.objects.filter(product=self.product).count(), 2
        )

    def test_duplicate_opt_in_race_lands_on_one_row(self):
        """Concurrent opt-ins race to the unique constraint and one row survives: the DB is the authority, not check-then-act."""
        _, token = self.api_login(username=self.user.username)
        RestockNotification.objects.create(user=self.user, product=self.product)
        res = self.client.post(self._url(), format="json")
        self.assertIn(res.status_code, (200, 201))
        self.assertEqual(RestockNotification.objects.count(), 1)

    def test_inactive_row_never_mails(self):
        _, token = self.api_login(username=self.user.username)
        self.client.post(self._url(), format="json")
        self.client.delete(self._url(), format="json")
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 3, "restock")
        self.assertEqual(len(mail.outbox), 0)


@tag("restock")
class RestockTriggerTests(ApiTestCase):
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
        self.assertEqual(
            RestockNotification.objects.filter(notified_at__isnull=False).count(),
            2,
        )

    def test_spent_row_does_not_remail_without_sellout(self):
        self._opt_in(self.buyer)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 3, "restock")
        self.assertEqual(len(mail.outbox), 1)

    def test_rearm_after_sellout(self):
        self._opt_in(self.buyer)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, -5, "sale")
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 7, "restock")
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Back in stock", mail.outbox[1].subject)

    def test_rearm_clears_a_claim_whose_send_never_happened(self):
        """A row claimed by a cycle that died before sending must not wedge the product out of future notifications; this stamps the state such a process leaves behind."""
        self._opt_in(self.buyer)
        RestockNotification.objects.filter(user=self.buyer).update(
            notified_at=timezone.now()
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 0)
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
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        del mail.outbox[:]
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

        # Keyed on the recipient, not call order: an order-dependent side_effect list turns this into a
        # coin flip on the other engine.
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
        # The first row is not marked spent: its send failed and the claim was released, so it is
        # retried on the next trigger -- log-only contract, no silent loss.
        self.assertEqual(
            RestockNotification.objects.filter(notified_at__isnull=False).count(),
            1,
        )
        self.assertIsNotNone(
            RestockNotification.objects.get(user=self.other).notified_at
        )
        self.assertIsNone(RestockNotification.objects.get(user=self.buyer).notified_at)

    def test_failed_send_is_retried_on_the_next_crossing(self):
        """The spent stamp no longer rides the stock transaction, so a send failure must give the row back or that customer is spent on an email that never went out."""
        self._opt_in(self.buyer)
        with mock.patch(
            "common.notifications.send_email", side_effect=Exception("smtp down")
        ):
            with self.assertLogs("products.restock", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(RestockNotification.objects.get(user=self.buyer).notified_at)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, -5, "sale")
            self.product.adjust_stock(None, 7, "restock")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Back in stock", mail.outbox[0].subject)
        self.assertIsNotNone(
            RestockNotification.objects.get(user=self.buyer).notified_at
        )

    def test_claim_before_send_drops_a_row_another_cycle_already_won(self):
        """The claim is the mutual exclusion and it happens BEFORE the send, so a second stock cycle cannot mail rows still ahead of us."""
        self._opt_in(self.buyer)
        self._opt_in(self.other, client=self.fresh_client())

        def concurrent_cycle(template, context, subject, recipient):
            RestockNotification.objects.exclude(user__email=recipient).update(
                notified_at=timezone.now()
            )

        with mock.patch(
            "common.notifications.send_email", side_effect=concurrent_cycle
        ) as send_mock:
            with self.captureOnCommitCallbacks(execute=True):
                self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(send_mock.call_count, 1)
        self.assertEqual(
            RestockNotification.objects.filter(notified_at__isnull=False).count(),
            2,
        )

    def test_claim_skips_a_row_that_opted_out_after_the_capture(self):
        """The armed list is captured inside the inventory transaction and the claim lands after it commits, so the claim must re-check active=True or an opt-out is resurrected."""
        self._opt_in(self.buyer)
        self._opt_in(self.other, client=self.fresh_client())

        def opt_out_the_others(template, context, subject, recipient):
            # The window: everyone else in the captured list opts out while our fan-out is mid-loop, keyed
            # on the recipient so row order does not matter.
            RestockNotification.objects.exclude(user__email=recipient).update(
                active=False
            )

        with mock.patch(
            "common.notifications.send_email", side_effect=opt_out_the_others
        ) as send_mock:
            with self.captureOnCommitCallbacks(execute=True):
                self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(send_mock.call_count, 1)
        mailed = send_mock.call_args_list[0].args[3]
        opted_out = list(RestockNotification.objects.filter(active=False))
        self.assertEqual(len(opted_out), 1)
        self.assertNotEqual(opted_out[0].user.email, mailed)
        self.assertIsNone(opted_out[0].notified_at)

    def test_send_runs_with_the_stock_lock_block_already_popped(self):
        """The assertion is about WHERE the send runs: while it happens, the innermost atomic block must still be the caller's, because adjust_stock's own block is what holds the product row lock."""
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
        self.assertEqual(len(observed), 1)
        innermost, depth = observed[0]
        self.assertIs(innermost, caller_block)
        self.assertEqual(depth, caller_depth)

    def test_notice_is_registered_not_performed_under_the_lock(self):
        """The send must not happen inside adjust_stock's atomic block: the fan-out is a registered commit hook, so no socket is open while the product row is locked."""
        self._opt_in(self.buyer)
        with mock.patch("common.notifications.send_email") as send_mock:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                self.product.adjust_stock(None, 5, "restock")
                self.assertEqual(send_mock.call_count, 0)
            self.assertEqual(len(callbacks), 1)
        self.assertEqual(send_mock.call_count, 1)

    def test_transaction_rollback_also_rolls_the_spent_stamp(self):
        """An exception inside adjust_stock leaves the stock and the spent stamp untouched; the rollback-must-not-mail invariant belongs to the test below."""
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
        """Rollback-together, measured as ZERO sends: the pre-Async send was inline and could not be recalled, whereas a registered callback dies with its transaction."""
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

    # MERCHANT-DELIST-C2: a restock while the product is off sale is suppressed, because the mail
    # would link to a 404; relist releases the waiting rows through the same fan-out.

    def _delist(self):
        products.objects.filter(pk=self.product.pk).update(is_active=False)

    def test_a_restock_while_delisted_sends_no_mail_and_leaves_optins_waiting(self):
        self._opt_in(self.buyer)
        self._delist()
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 0)
        preference = RestockNotification.objects.get(user=self.buyer)
        self.assertTrue(preference.active)
        self.assertIsNone(preference.notified_at)

    def test_relist_releases_the_waiting_optins(self):
        """Suppression is not silence: relist is the moment the product is buyable again, and the waiting rows are released then."""
        from django.contrib.auth.models import User

        User.objects.create_superuser(
            "releaser", "releaser@example.com", "S3cure-Passphrase!"
        )
        self._opt_in(self.buyer)
        self._delist()
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(
            self.client.login(username="releaser", password="S3cure-Passphrase!")
        )
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                "/admin/products/products/",
                {
                    "action": "relist_products",
                    "_selected_action": [str(self.product.id)],
                    "select_across": "0",
                },
                follow=True,
            )
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertIs(self.product.is_active, True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Back in stock", mail.outbox[0].subject)
        self.assertIsNotNone(
            RestockNotification.objects.get(user=self.buyer).notified_at
        )

    def test_relist_with_no_stock_releases_nothing(self):
        from django.contrib.auth.models import User

        User.objects.create_superuser(
            "releaser2", "releaser2@example.com", "S3cure-Passphrase!"
        )
        self._opt_in(self.buyer)
        self._delist()
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, -5, "sale")
        del mail.outbox[:]
        self.assertTrue(
            self.client.login(username="releaser2", password="S3cure-Passphrase!")
        )
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                "/admin/products/products/",
                {
                    "action": "relist_products",
                    "_selected_action": [str(self.product.id)],
                    "select_across": "0",
                },
                follow=True,
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(RestockNotification.objects.get(user=self.buyer).notified_at)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 4, "restock")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Back in stock", mail.outbox[0].subject)


@tag("restock")
class RestockModelTests(ApiTestCase):
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


@tag("restock")
class RestockGuestOptInTests(ApiTestCase):
    """The guest half: an unauthenticated POST carrying a submitted email arms a row (user NULL, email column), and the trigger mails THAT address while the authenticated path keeps mailing user.email."""

    def setUp(self):
        self.product = self.make_product(name="Oud Noir", stock=0)
        self.user = self.make_user("acct")

    def _url(self, product=None):
        return f"/api/products/{(product or self.product).slug}/restock-notifications/"

    def _guest_post(self, payload, **kwargs):
        return self.client.post(self._url(), payload, format="json", **kwargs)

    def test_guest_post_with_email_arms_and_trigger_mails_submitted_address(self):
        res = self._guest_post({"email": "Guest@Example.COM"})
        self.assertEqual(res.status_code, 202, res.data)
        self.assertEqual(
            set(res.data),
            {"product_id", "slug", "active"},
        )
        self.assertEqual(res.data["product_id"], self.product.id)
        preference = RestockNotification.objects.get(product=self.product)
        self.assertIsNone(preference.user_id)
        # Normalized before storage: strip + lower, so case and padding variants converge on one row
        # and one throttle identity.
        self.assertEqual(preference.email, "guest@example.com")
        self.assertTrue(preference.active)
        self.assertIsNone(preference.notified_at)
        # The trigger mails the SUBMITTED address, and no account existed for it -- the user table is
        # never consulted on the guest path.
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["guest@example.com"])

    def test_guest_post_without_email_keeps_the_prior_401_bytes(self):
        no_email = self._guest_post({})
        padding_only = self._guest_post({"email": "   "})
        not_a_string = self._guest_post({"email": 123})
        guest_delete = self.client.delete(self._url(), format="json")
        for res in (no_email, padding_only, not_a_string, guest_delete):
            self.assertEqual(res.status_code, 401, res.data)
            self.assertEqual(res.content, no_email.content)
        self.assertEqual(RestockNotification.objects.count(), 0)

    def test_guest_malformed_json_body_keeps_the_prior_401_bytes(self):
        malformed = self.client.post(
            self._url(), b"{not json", content_type="application/json"
        )
        no_email = self._guest_post({})
        malformed_delete = self.client.delete(
            self._url(), b"{not json", content_type="application/json"
        )
        for res in (malformed, no_email, malformed_delete):
            self.assertEqual(res.status_code, 401, res.data)
            self.assertEqual(res.content, no_email.content)
        self.assertEqual(malformed.data["code"], "not_authenticated")
        self.assertEqual(RestockNotification.objects.count(), 0)
        valid = self._guest_post({"email": "valid@example.org"})
        invalid = self._guest_post({"email": "not-an-email"})
        self.assertEqual(valid.status_code, 202, valid.data)
        self.assertEqual(invalid.status_code, 202, invalid.data)
        self.assertEqual(valid.content, invalid.content)

    def test_authed_malformed_json_body_is_ignored_like_any_other_body(self):
        self.api_login(username=self.user.username)
        malformed = self.client.post(
            self._url(), b"{not json", content_type="application/json"
        )
        other = self.make_user("acct2")
        other_client = self.fresh_client()
        self.api_login(username=other.username, client=other_client)
        empty = other_client.post(self._url())
        self.assertEqual(malformed.status_code, 201, malformed.data)
        self.assertEqual(empty.status_code, 201, empty.data)
        self.assertEqual(malformed.content, empty.content)
        self.assertEqual(
            RestockNotification.objects.filter(user__isnull=False).count(), 2
        )

    def test_guest_invalid_email_is_accepted_uniformly_and_arms_nothing(self):
        valid = self._guest_post({"email": "valid@example.org"})
        invalid = self._guest_post({"email": "not-an-email"})
        overlong = self._guest_post({"email": f"{'a' * 250}@b.co"})
        self.assertEqual(valid.status_code, 202, valid.data)
        self.assertEqual(invalid.status_code, 202, invalid.data)
        self.assertEqual(invalid.content, valid.content)
        self.assertEqual(overlong.content, valid.content)
        self.assertEqual(
            list(RestockNotification.objects.values_list("email", flat=True)),
            ["valid@example.org"],
        )

    def test_guest_repeat_optin_is_uniform_and_rearms(self):
        first = self._guest_post({"email": "repeat@example.org"})
        RestockNotification.objects.filter(user__isnull=True).update(
            notified_at=timezone.now()
        )
        second = self._guest_post({"email": "REPEAT@example.org "})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202, second.data)
        self.assertEqual(second.content, first.content)
        self.assertEqual(RestockNotification.objects.count(), 1)
        preference = RestockNotification.objects.get(user__isnull=True)
        self.assertTrue(preference.active)
        self.assertIsNone(preference.notified_at)

    def test_authed_post_ignores_submitted_email_and_mails_the_account_address(self):
        _, token = self.api_login(username=self.user.username)
        res = self.client.post(
            self._url(), {"email": "someoneelse@example.org"}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            set(res.data),
            {"product_id", "slug", "active"},
        )
        preference = RestockNotification.objects.get(user=self.user)
        self.assertEqual(preference.email, "")
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["acct@example.com"])

    def test_guest_response_does_not_depend_on_registration(self):
        self.assertEqual(self.user.email, "acct@example.com")
        registered = self._guest_post({"email": "acct@example.com"})
        unregistered = self._guest_post({"email": "stranger@nowhere.test"})
        self.assertEqual(registered.status_code, 202)
        self.assertEqual(unregistered.status_code, 202)
        self.assertEqual(unregistered.content, registered.content)
        self.assertEqual(
            RestockNotification.objects.filter(user__isnull=True).count(), 2
        )
        self.assertEqual(RestockNotification.objects.filter(user=self.user).count(), 0)

    def test_guest_in_stock_refusal_matches_authed_bytes(self):
        products.objects.filter(pk=self.product.pk).update(stock=5)
        guest = self._guest_post({"email": "guest@example.com"})
        _, token = self.api_login(username=self.user.username)
        authed = self.client.post(self._url(), format="json")
        self.assertEqual(guest.status_code, 400, guest.data)
        self.assertEqual(authed.status_code, 400, authed.data)
        self.assertEqual(guest.content, authed.content)
        self.assertEqual(RestockNotification.objects.count(), 0)

    def test_unknown_and_delisted_remain_uniform_for_guests(self):
        guest_unknown = self.client.post(
            "/api/products/no-such-fragrance/restock-notifications/",
            {"email": "guest@example.com"},
            format="json",
        )
        products.objects.filter(pk=self.product.pk).update(is_active=False)
        guest_delisted = self._guest_post({"email": "guest@example.com"})
        self.assertEqual(guest_unknown.status_code, 404, guest_unknown.data)
        self.assertEqual(guest_delisted.status_code, 404, guest_delisted.data)
        self.assertEqual(guest_delisted.content, guest_unknown.content)
        self.assertEqual(RestockNotification.objects.count(), 0)

    def test_guest_row_spends_and_rearms_like_account_rows(self):
        self._guest_post({"email": "cycle@example.org"})
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 5, "restock")
        self.assertEqual(len(mail.outbox), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, 3, "restock")
        self.assertEqual(len(mail.outbox), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.product.adjust_stock(None, -8, "sale")
            self.product.adjust_stock(None, 6, "restock")
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[1].to, ["cycle@example.org"])

    def test_guest_send_failure_is_log_only_and_releases_the_claim(self):
        self._guest_post({"email": "flaky@example.org"})
        with (
            mock.patch(
                "common.notifications.send_email", side_effect=Exception("smtp down")
            ),
            self.assertLogs("products.restock", level="ERROR") as logs,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                self.product.adjust_stock(None, 5, "restock")
        self.assertIn("guest flaky@example.org", "\n".join(logs.output))
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(
            RestockNotification.objects.get(user__isnull=True).notified_at
        )


@tag("restock")
class RestockGuestThrottleTests(ApiTestCase):
    """Two throttles run on the endpoint: per-IP 'restock' and per-(IP + email) 'restock-guest'; each engagement test leaves the OTHER budget looser than the one under test."""

    def setUp(self):
        self.product = self.make_product(name="Rose Aurum", stock=0)

    def _post(self, email, ip="127.0.0.1"):
        return self.client.post(
            f"/api/products/{self.product.slug}/restock-notifications/",
            {"email": email},
            format="json",
            REMOTE_ADDR=ip,
        )

    def _rates(self, restock="5/min", guest="5/min"):
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["restock"] = restock
        rates["restock-guest"] = guest
        return rates

    def test_pair_budget_bounds_repeat_submissions_of_one_pair(self):
        with mock.patch.object(
            ScopedRateThrottle, "THROTTLE_RATES", self._rates("10/min", "2/min")
        ):
            cache.clear()
            self.assertEqual(self._post("flood@example.org").status_code, 202)
            self.assertEqual(self._post("flood@example.org").status_code, 202)
            throttled = self._post("flood@example.org")
        self.assertEqual(throttled.status_code, 429, throttled.data)
        self.assertEqual(
            RestockNotification.objects.filter(user__isnull=True).count(), 1
        )

    def test_ip_budget_bounds_one_client_rotating_addresses(self):
        with mock.patch.object(
            ScopedRateThrottle, "THROTTLE_RATES", self._rates("2/min", "10/min")
        ):
            cache.clear()
            self.assertEqual(self._post("a1@example.org").status_code, 202)
            self.assertEqual(self._post("a2@example.org").status_code, 202)
            throttled = self._post("a3@example.org")
        self.assertEqual(throttled.status_code, 429, throttled.data)

    def test_pair_budget_is_keyed_on_the_normalized_pair(self):
        with mock.patch.object(
            ScopedRateThrottle, "THROTTLE_RATES", self._rates("10/min", "2/min")
        ):
            cache.clear()
            self.assertEqual(self._post("Case@Example.org").status_code, 202)
            self.assertEqual(self._post("case@example.org ").status_code, 202)
            throttled = self._post("CASE@example.ORG")
        self.assertEqual(throttled.status_code, 429, throttled.data)

    def test_authed_budget_is_still_the_user_keyed_one(self):
        self.make_user("floodacct")
        self.api_login(username="floodacct")
        with mock.patch.object(
            ScopedRateThrottle, "THROTTLE_RATES", self._rates("2/min", "1/min")
        ):
            cache.clear()
            for expected in (201, 200):
                res = self.client.post(
                    f"/api/products/{self.product.slug}/restock-notifications/",
                    format="json",
                )
                self.assertEqual(res.status_code, expected, res.data)
            throttled = self.client.post(
                f"/api/products/{self.product.slug}/restock-notifications/",
                format="json",
            )
        self.assertEqual(throttled.status_code, 429, throttled.data)

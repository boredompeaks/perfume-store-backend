"""SPEC-19-2: admin alert dispatch (spec 19.2, alerting halves of
[R-19.15]/[R-19.20]/[R-19.21]/[R-19.27]).

Every alert rides the SPEC-19-1 single send path (common.notifications
send_email — no new send_mail anywhere), targets ALERT_RECIPIENTS only,
is suppressed inside its per-type cooldown, and is log-only on send
failure (an alert can never break the flow that tripped it). locmem
backend only — no network.
"""
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import override_settings, tag
from django.utils import timezone

from common.models import AuditEvent
from common.testing import ApiTestCase, extract_link_params
from ops import alerts
from ops.services import check_stock_alerts


def alert_recipients(value):
    """Point ALERT_RECIPIENTS at a test mailbox (empty = disabled)."""
    return override_settings(ALERT_RECIPIENTS=value)


def _clear_cooldowns():
    # The default cache (LocMemCache) is cleared per test by ApiTestCase,
    # but explicit clearing keeps the cooldown tests self-evident.
    cache.clear()


def _seed_failures(count, minutes_ago=1):
    """Record failed-payment audit events, backdated into/out of the
    spike window (AuditEvent.record owns the write path; only the
    timestamps are shifted for window-edge tests)."""
    now = timezone.now()
    for i in range(count):
        marker = f"seed-{now.timestamp()}-{i}"
        AuditEvent.record(
            "payment.signature_rejected", detail={"seeded": marker}
        )
        AuditEvent.objects.filter(detail__seeded=marker).update(
            created_at=now - timedelta(minutes=minutes_ago)
        )


@tag("alerts")
class AlertRecipientTests(ApiTestCase):
    """Recipients parse from ALERT_RECIPIENTS; empty disables everything."""

    def test_recipients_parse_split_strip_dedupe(self):
        with alert_recipients("a@x.com, b@x.com ,a@x.com,"):
            self.assertEqual(alerts._recipients(), ["a@x.com", "b@x.com"])

    def test_no_recipients_disables_every_alert(self):
        with alert_recipients(""):
            _clear_cooldowns()
            sent = alerts.notify_low_stock(
                [{"id": 1, "name": "Rose Aurum", "stock": 2}]
            )
        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)


@tag("alerts")
class LowStockAlertTests(ApiTestCase):
    """[R-19.15] alerting half: low/out-of-stock breach alerts."""

    def test_low_stock_breach_sends_one_mail_per_recipient(self):
        with alert_recipients("staff@x.com, boss@x.com"):
            _clear_cooldowns()
            sent = alerts.notify_low_stock(
                [{"id": 1, "name": "Rose Aurum", "stock": 2}]
            )
        self.assertTrue(sent)
        self.assertEqual(
            [m.to for m in mail.outbox], [["staff@x.com"], ["boss@x.com"]]
        )
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Low stock alert")
        self.assertIn("Rose Aurum (id 1): 2 in stock", message.body)
        self.assertEqual(message.from_email, settings.DEFAULT_FROM_EMAIL)

    def test_no_breach_no_alert(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            self.assertFalse(alerts.notify_low_stock([]))
        self.assertEqual(len(mail.outbox), 0)

    def test_out_of_stock_breach_sends(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            sent = alerts.notify_out_of_stock(
                [{"id": 3, "name": "Oud Royale", "stock": 0}]
            )
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Out of stock alert")
        self.assertIn("Oud Royale (id 3): 0 in stock", mail.outbox[0].body)

    def test_check_stock_alerts_fires_both_breaches(self):
        self.make_product(name="Low One", stock=2)
        self.make_product(name="Gone One", stock=0)
        self.make_product(name="Fine One", stock=99)
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            check_stock_alerts()
        subjects = sorted(m.subject for m in mail.outbox)
        self.assertEqual(subjects, ["Low stock alert", "Out of stock alert"])
        self.assertIn("Low One (id", mail.outbox[0].body)

    def test_check_stock_alerts_silent_when_no_breach(self):
        self.make_product(name="Fine One", stock=99)
        with alert_recipients("staff@x.com"):
            check_stock_alerts()
        self.assertEqual(len(mail.outbox), 0)


@tag("alerts")
class CooldownTests(ApiTestCase):
    """Per-alert-type cooldown: a spike cannot email-bomb the staff."""

    def test_second_alert_inside_window_is_suppressed(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            first = alerts.notify_low_stock(
                [{"id": 1, "name": "Rose Aurum", "stock": 2}]
            )
            second = alerts.notify_low_stock(
                [{"id": 1, "name": "Rose Aurum", "stock": 2}]
            )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(mail.outbox), 1)

    def test_cooldown_is_per_alert_type(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            alerts.notify_low_stock(
                [{"id": 1, "name": "Rose Aurum", "stock": 2}]
            )
            sent = alerts.notify_out_of_stock(
                [{"id": 2, "name": "Gone One", "stock": 0}]
            )
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 2)

    def test_cooldown_expires_after_window(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            self.assertTrue(
                alerts.notify_low_stock(
                    [{"id": 1, "name": "Rose Aurum", "stock": 2}]
                )
            )
            # Simulate the window passing by expiring the marker directly
            # (deterministic — no sleeps, no time mocking).
            cache.delete("alerts:cooldown:low_stock")
            self.assertTrue(
                alerts.notify_low_stock(
                    [{"id": 1, "name": "Rose Aurum", "stock": 2}]
                )
            )
        self.assertEqual(len(mail.outbox), 2)

    def test_suppressed_alert_is_logged_not_silent(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            alerts.notify_low_stock(
                [{"id": 1, "name": "Rose Aurum", "stock": 2}]
            )
            with self.assertLogs("ops.alerts", level="INFO") as logs:
                alerts.notify_low_stock(
                    [{"id": 1, "name": "Rose Aurum", "stock": 2}]
                )
        self.assertIn("suppressed", "\n".join(logs.output))


@tag("alerts")
class PaymentFailureSpikeTests(ApiTestCase):
    """[R-19.21] alerting half: >=3 failed payment attempts in 300s."""

    def test_below_threshold_no_alert(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            _seed_failures(2)
            self.assertFalse(alerts.check_payment_failure_spike())
        self.assertEqual(len(mail.outbox), 0)

    def test_at_threshold_fires_spike_alert(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            _seed_failures(3)
            self.assertTrue(alerts.check_payment_failure_spike())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].subject, "Payment failure spike alert"
        )
        body = mail.outbox[0].body
        self.assertIn("3 payment attempts failed", body)
        self.assertIn("payment.signature_rejected", body)

    def test_failures_outside_window_do_not_count(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            _seed_failures(2, minutes_ago=10)
            _seed_failures(1, minutes_ago=1)
            self.assertFalse(alerts.check_payment_failure_spike())
        self.assertEqual(len(mail.outbox), 0)

    def test_spike_alert_is_cooldown_deduped(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            _seed_failures(3)
            self.assertTrue(alerts.check_payment_failure_spike())
            _seed_failures(3)
            self.assertFalse(alerts.check_payment_failure_spike())
        self.assertEqual(len(mail.outbox), 1)

    def test_no_recipients_skips_the_spike_query_entirely(self):
        # Disabled alerts must not even hit the audit trail for the spike
        # check — the trigger site is hot (every verify failure).
        _clear_cooldowns()
        with mock.patch(
            "common.models.AuditEvent.objects.filter"
        ) as filter_mock:
            self.assertFalse(alerts.check_payment_failure_spike())
        filter_mock.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)


@tag("alerts")
class SpikeDashboardWiringTests(ApiTestCase):
    """[R-19.20] audit c1 BUG-1: the spike detector had no production
    call site. The dashboard load is the wired seam (beside the stock
    poll), matching the check_stock_alerts export symmetry; a hot trigger
    site would have needed an orders/views.py edit (frozen file)."""

    def setUp(self):
        User.objects.create_superuser("boss", "boss@example.com", "boss-pass-123")
        self.assertTrue(self.client.login(username="boss", password="boss-pass-123"))

    def test_dashboard_load_fires_spike_alert_from_audit_trail(self):
        _seed_failures(3)
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            res = self.client.get("/admin/dashboard/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Payment failure spike alert")
        self.assertIn("payment.signature_rejected", mail.outbox[0].body)

    def test_dashboard_spike_alert_is_cooldown_deduped(self):
        _seed_failures(3)
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            self.client.get("/admin/dashboard/")
            self.client.get("/admin/dashboard/")
        self.assertEqual(len(mail.outbox), 1)

    def test_dashboard_with_no_recipients_skips_the_spike_query(self):
        # The detector's cheap-when-disabled property must survive the new
        # seam: with ALERT_RECIPIENTS empty the dashboard load must not
        # touch the audit trail at all (cheap page even when alerts are off).
        _clear_cooldowns()
        with mock.patch(
            "common.models.AuditEvent.objects.filter"
        ) as filter_mock:
            res = self.client.get("/admin/dashboard/")
        self.assertEqual(res.status_code, 200)
        filter_mock.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)


@tag("alerts")
class SecurityChangeAlertTests(ApiTestCase):
    """[R-19.27] alerting half: password reset triggers the staff alert."""

    def _reset_password(self, username):
        res = self.client.post(
            "/api/accounts/password-reset/",
            {"email": f"{username}@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        uid, token = extract_link_params(mail.outbox[-1].body, "reset-password")
        return self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "N3w-Passphrase!"},
            format="json",
        )

    def test_password_reset_end_to_end_sends_security_alert(self):
        self.register_and_verify(username="resetalert")
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            res = self._reset_password("resetalert")
            self.assertEqual(res.status_code, 200, res.data)
        staff_alerts = [
            m
            for m in mail.outbox
            if m.subject == "Security-sensitive account change"
        ]
        self.assertEqual(len(staff_alerts), 1)
        self.assertEqual(staff_alerts[0].to, ["staff@x.com"])
        self.assertIn("resetalert", staff_alerts[0].body)
        self.assertIn("Password reset completed", staff_alerts[0].body)

    def test_no_recipients_password_reset_still_succeeds(self):
        # Best-effort alerting: the customer flow is identical when the
        # alert mailbox is unconfigured.
        self.register_and_verify(username="resetalert2")
        res = self._reset_password("resetalert2")
        self.assertEqual(res.status_code, 200, res.data)


@tag("alerts")
class IntegrationOutageAlertTests(ApiTestCase):
    """[R-19.20] alerting half: degraded /health/ fires the outage alert."""

    def test_degraded_health_sends_outage_alert(self):
        degraded = {
            "status": "degraded",
            "checks": {"database": False},
            "pending_orders": None,
            "carts": None,
            "low_stock": 0,
            "out_of_stock": 0,
            "low_stock_threshold": 5,
            "stock_alert_checked": False,
        }
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            with mock.patch("ops.views.get_health", return_value=degraded):
                res = self.client.get("/health/")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Integration outage alert")
        self.assertIn("degraded", mail.outbox[0].body)

    def test_healthy_health_sends_nothing(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            res = self.client.get("/health/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)


@tag("alerts")
class SendFailureContractTests(ApiTestCase):
    """Log-only failure contract: an alert can never break its trigger."""

    def test_send_failure_is_logged_and_swallowed(self):
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            with mock.patch(
                "common.notifications.send_email",
                side_effect=Exception("smtp down"),
            ):
                with self.assertLogs("ops.alerts", level="ERROR") as logs:
                    sent = alerts.notify_low_stock(
                        [{"id": 1, "name": "Rose Aurum", "stock": 2}]
                    )
        self.assertFalse(sent)
        self.assertIn("smtp down", "\n".join(logs.output))

    def test_cache_failure_fails_open_and_still_sends(self):
        """A raisable cache backend must not kill the alert: the cooldown
        check runs inside _send's try, so a cache outage degrades to an
        attempted send (worst case one un-deduped mail), never a 500 in
        the dashboard/health flow that tripped the alert."""
        with alert_recipients("staff@x.com"):
            _clear_cooldowns()
            with mock.patch.object(
                cache, "get", side_effect=Exception("cache down")
            ):
                sent = alerts.notify_low_stock(
                    [{"id": 1, "name": "Rose Aurum", "stock": 2}]
                )
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)

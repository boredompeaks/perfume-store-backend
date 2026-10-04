"""SPEC-22-10 [R-22.18]: the send-probe's safety pins.

The command exists to prove mail leaves the deployment, so the properties
worth pinning are the ones that keep it from harming anyone: it is dry-run by
default, it can only ever address the deployment's own operator mailboxes,
it never reads or prints a customer's address, it refuses a backend that
cannot actually deliver, and a refused probe is loud.

Hermetic by construction: every send is attempted through a **faked**
``smtplib.SMTP``, so no test opens a socket, and no test can reach a real
mail server even if the backend override below were wrong. The suite's own
backend is the locmem one, which the command refuses outright - so a test that
forgot the override cannot send mail, it gets a refusal.
"""

import io
import smtplib
from unittest.mock import patch

from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

# A deployment with the SMTP keys an operator would really set, plus an
# operator mailbox allowlist. The credential is a recognisably fake fixture
# string (conventions.md: no credential-shaped value that looks like one).
PROBE_ENV = {
    "ALERT_RECIPIENTS": "ops@example.test, oncall@example.test",
    "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
    "EMAIL_HOST": "smtp.example.test",
    "EMAIL_PORT": 587,
    "EMAIL_USE_TLS": True,
    "EMAIL_HOST_USER": "probe@example.test",
    "EMAIL_HOST_PASSWORD": "TESTINGONLYPROBE-NOT-A-CREDENTIAL",
    "DEFAULT_FROM_EMAIL": "probe@example.test",
    "DJANGO_ENV": "staging",
}

SMTP_BACKEND = override_settings(**PROBE_ENV)


def run_probe(*args):
    """Call the command with the SMTP connection faked; return (out, smtp)."""
    out = io.StringIO()
    with patch("smtplib.SMTP") as smtp:
        call_command("email_send_probe", *args, stdout=out)
    return out.getvalue(), smtp


@SMTP_BACKEND
class EmailSendProbeSafetyTests(TestCase):
    """The gates, in the order the command applies them."""

    def test_dry_run_by_default_delivers_nothing(self):
        out, smtp = run_probe()
        self.assertIn("dry run: nothing was sent", out)
        smtp.assert_not_called()
        self.assertEqual(mail.outbox, [])

    def test_dry_run_names_the_plan_without_a_password(self):
        out, smtp = run_probe()
        self.assertIn("environment: staging", out)
        self.assertIn("smtp.example.test:587", out)
        self.assertIn("ops@example.test", out)
        self.assertNotIn(PROBE_ENV["EMAIL_HOST_PASSWORD"], out)
        smtp.assert_not_called()

    @SMTP_BACKEND
    def test_send_delivers_one_message_per_allowlisted_recipient(self):
        out, smtp = run_probe("--send")
        connection = smtp.return_value
        # One SMTP conversation, two messages - one per allowlisted mailbox.
        self.assertEqual(connection.sendmail.call_count, 2)
        recipients = [call.args[1] for call in connection.sendmail.call_args_list]
        self.assertEqual(
            sorted(recipients),
            [["oncall@example.test"], ["ops@example.test"]],
        )
        for call in connection.sendmail.call_args_list:
            self.assertEqual(call.args[0], PROBE_ENV["DEFAULT_FROM_EMAIL"])
        self.assertIn("accepted by the server for 2 recipient(s)", out)

    @SMTP_BACKEND
    def test_send_authenticates_and_upgrades_over_tls(self):
        out, smtp = run_probe("--send", "--recipient", "oncall@example.test")
        smtp.assert_called_once()
        # Django's SMTP backend opens the connection positionally.
        self.assertEqual(smtp.call_args.args[0], "smtp.example.test")
        self.assertEqual(smtp.call_args.args[1], 587)
        smtp.return_value.starttls.assert_called_once()
        smtp.return_value.login.assert_called_once_with(
            PROBE_ENV["EMAIL_HOST_USER"], PROBE_ENV["EMAIL_HOST_PASSWORD"]
        )
        self.assertEqual(smtp.return_value.sendmail.call_count, 1)
        self.assertIn("accepted by the server for 1 recipient(s)", out)

    @SMTP_BACKEND
    def test_recipient_matching_is_case_and_whitespace_insensitive(self):
        out, smtp = run_probe("--send", "--recipient", "  OnCall@Example.Test ")
        self.assertEqual(smtp.return_value.sendmail.call_count, 1)
        self.assertEqual(
            smtp.return_value.sendmail.call_args.args[1], ["oncall@example.test"]
        )

    @SMTP_BACKEND
    def test_refuses_a_recipient_outside_the_allowlist(self):
        out = io.StringIO()
        with patch("smtplib.SMTP") as smtp:
            with self.assertRaises(CommandError) as caught:
                call_command(
                    "email_send_probe",
                    "--send",
                    "--recipient",
                    "someone-else@example.test",
                    stdout=out,
                )
        self.assertIn("is not in ALERT_RECIPIENTS", str(caught.exception))
        # ...and nothing was sent while refusing: no connection, no message.
        smtp.assert_not_called()
        self.assertEqual(mail.outbox, [])

    @SMTP_BACKEND
    def test_refuses_when_nothing_is_allowlisted(self):
        with override_settings(ALERT_RECIPIENTS=""):
            with self.assertRaises(CommandError) as caught:
                run_probe()
        self.assertIn("no probe recipient is allowlisted", str(caught.exception))

    @SMTP_BACKEND
    def test_duplicate_and_blank_allowlist_entries_collapse(self):
        with override_settings(
            ALERT_RECIPIENTS="ops@example.test, , ops@example.test, oncall@"
        ):
            out, smtp = run_probe()
        # Two real addresses, de-duplicated; the blank and the truncated entry
        # are reported as given rather than silently repaired.
        self.assertIn("recipients:  ops@example.test, oncall@", out)
        smtp.assert_not_called()

    def test_refuses_a_backend_that_never_speaks_smtp(self):
        # The suite's own backend: locmem accepts and reports success, so a
        # probe against it would be a false green. It must be refused, and it
        # must be refused even with --send.
        for backend in (
            "django.core.mail.backends.locmem.EmailBackend",
            "django.core.mail.backends.console.EmailBackend",
            "django.core.mail.backends.dummy.EmailBackend",
        ):
            with (
                self.subTest(backend=backend),
                override_settings(EMAIL_BACKEND=backend),
            ):
                with self.assertRaises(CommandError) as caught:
                    run_probe("--send")
                self.assertIn("never speaks SMTP", str(caught.exception))
        self.assertEqual(mail.outbox, [])

    @SMTP_BACKEND
    def test_a_refused_delivery_is_loud_and_echoes_no_credential(self):
        with patch(
            "common.notifications.send_email",
            side_effect=smtplib.SMTPAuthenticationError(
                535, b"535 5.7.8 Authentication credentials invalid"
            ),
        ):
            with self.assertRaises(CommandError) as caught:
                run_probe("--send")
        message = str(caught.exception)
        self.assertIn("SMTPAuthenticationError", message)
        self.assertIn("Authentication credentials invalid", message)
        self.assertNotIn(PROBE_ENV["EMAIL_HOST_PASSWORD"], message)


@SMTP_BACKEND
class EmailSendProbePrivacyTests(TestCase):
    """The command cannot become a way to reach a customer."""

    def test_it_refuses_a_customers_address_even_when_it_exists(self):
        from django.contrib.auth.models import User

        customer = User.objects.create_user(
            username="real-buyer",
            email="real-buyer@example.test",
            password="S3cure-Passphrase!",
        )
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "email_send_probe", "--send", "--recipient", customer.email, stdout=out
            )
        self.assertNotIn(customer.email, out.getvalue())
        self.assertNotIn(customer.username, out.getvalue())

    @SMTP_BACKEND
    def test_the_probe_message_carries_deployment_facts_only(self):
        from django.contrib.auth.models import User

        User.objects.create_user(
            username="real-buyer",
            email="real-buyer@example.test",
            password="S3cure-Passphrase!",
        )
        _, smtp = run_probe("--send", "--recipient", "ops@example.test")
        _, to_addrs, message = smtp.return_value.sendmail.call_args.args
        self.assertEqual(to_addrs, ["ops@example.test"])
        body = message.decode() if isinstance(message, bytes) else message
        for leak in ("real-buyer", "real-buyer@example.test", "S3cure"):
            self.assertNotIn(leak, body)
        self.assertIn("staging", body)
        self.assertIn("smtp.example.test:587", body)

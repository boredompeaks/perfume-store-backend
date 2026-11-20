"""Delivery-probe contract: one event reaches the injected sink, a disabled
channel and a raising transport are clean refusals, and no DSN or recipient
address appears in the output, the event or the health surface.

Nothing here opens a network socket: the transport is a recorder, as in
test_sentry.py. The command module is never imported here, so the same tests
also run against the base tree where the command does not exist yet; the
transport is injected through `common.sentry.init_sentry`. T-20261118-010.
"""

import io
import json
from unittest.mock import patch

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from common import sentry as sentry_support
from ops.services import get_health

FAKE_DSN = "https://not-a-real-key@o0.ingest.us.example.invalid/0"
FAKE_RECIPIENTS = "ops@example.test, oncall@example.test"

PROBE_SETTINGS = override_settings(
    SENTRY_DSN=FAKE_DSN,
    SENTRY_ENVIRONMENT="staging",
    SENTRY_RELEASE="probe-release",
    SENTRY_SAMPLE_RATE=1.0,
    SENTRY_TRACES_SAMPLE_RATE=0.0,
    DJANGO_ENV="staging",
    ALERT_RECIPIENTS=FAKE_RECIPIENTS,
    ERROR_TRACKING_ENABLED=True,
)


def recording_transport():
    from sentry_sdk.transport import Transport

    class RecordingTransport(Transport):
        def __init__(self, options=None):
            super().__init__(options)
            self.events = []

        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.type == "event":
                    self.events.append(item.payload.json)

    return RecordingTransport()


def raising_transport():
    from sentry_sdk.transport import Transport

    class RaisingTransport(Transport):
        def __init__(self, options=None):
            super().__init__(options)
            self.calls = 0

        def capture_envelope(self, envelope):
            self.calls += 1
            raise RuntimeError("ingest host refused the connection")

    return RaisingTransport()


def injecting(transport):
    """`common.sentry.init_sentry` with the probe's transport swapped in."""
    real_init = sentry_support.init_sentry

    def _init(**kwargs):
        kwargs["transport"] = transport
        return real_init(**kwargs)

    return _init


def reset_sdk():
    import sentry_sdk

    try:
        sentry_sdk.get_client().close()
    finally:
        sentry_sdk.init(dsn="")


@PROBE_SETTINGS
class SentrySendProbeTests(TestCase):
    def setUp(self):
        self.transport = recording_transport()
        self.addCleanup(reset_sdk)

    def test_send_delivers_exactly_one_event_to_the_injected_transport(self):
        out = io.StringIO()
        with patch("common.sentry.init_sentry", new=injecting(self.transport)):
            try:
                call_command("sentry_send_probe", "--send", stdout=out)
            except CommandError as exc:
                self.fail(f"probe must deliver when a sink is configured: {exc}")
        self.assertEqual(len(self.transport.events), 1)
        self.assertEqual(self.transport.events[0]["environment"], settings.DJANGO_ENV)
        self.assertIn("one probe event accepted", out.getvalue())

    def test_refuses_without_a_dsn_and_sends_nothing(self):
        with override_settings(SENTRY_DSN=""):
            with patch("common.sentry.init_sentry", new=injecting(self.transport)):
                with self.assertRaises(CommandError) as caught:
                    call_command("sentry_send_probe", "--send")
        self.assertIn("SENTRY_DSN", str(caught.exception))
        self.assertEqual(self.transport.events, [])

    def test_dry_run_reports_an_unset_release_without_a_value(self):
        out = io.StringIO()
        with override_settings(SENTRY_RELEASE=None):
            with patch("common.sentry.init_sentry", new=injecting(self.transport)):
                try:
                    call_command("sentry_send_probe", stdout=out)
                except CommandError as exc:
                    self.fail(f"probe must dry-run with a configured DSN: {exc}")
        output = out.getvalue()
        self.assertIn("release:     <unset>", output)
        self.assertNotIn("None", output)

    def test_a_failing_transport_fails_the_probe_loudly_without_echoing_the_dsn(
        self,
    ):
        failing = raising_transport()
        out = io.StringIO()
        with patch("common.sentry.init_sentry", new=injecting(failing)):
            try:
                call_command("sentry_send_probe", "--send", stdout=out)
            except CommandError as exc:
                message = str(exc)
            else:
                self.fail("a failing transport must fail the probe")
        self.assertIn("transport", message.lower())
        self.assertEqual(failing.calls, 1)
        self.assertNotIn(FAKE_DSN, message)
        self.assertNotIn("Traceback", message)


@PROBE_SETTINGS
class SentrySendProbePrivacyTests(TestCase):
    def setUp(self):
        self.transport = recording_transport()
        self.addCleanup(reset_sdk)

    def test_neither_output_nor_event_nor_health_carries_a_dsn_or_address(self):
        outputs = []
        for args in ((), ("--send",)):
            out = io.StringIO()
            with patch("common.sentry.init_sentry", new=injecting(self.transport)):
                try:
                    call_command("sentry_send_probe", *args, stdout=out)
                except CommandError as exc:
                    self.fail(f"probe must run with a configured DSN: {exc}")
            outputs.append(out.getvalue())

        for output in outputs:
            self.assertNotIn(FAKE_DSN, output)
            self.assertNotIn("ops@example.test", output)

        self.assertEqual(len(self.transport.events), 1)
        event_json = json.dumps(self.transport.events[0])
        self.assertNotIn(FAKE_DSN, event_json)
        self.assertNotIn("ops@example.test", event_json)

        health = get_health()
        self.assertIsInstance(health["checks"]["alert_recipients_configured"], bool)
        self.assertIsInstance(health["checks"]["error_tracking_enabled"], bool)
        self.assertNotIn(FAKE_DSN, str(health))
        self.assertNotIn("ops@example.test", str(health))

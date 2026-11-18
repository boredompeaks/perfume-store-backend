import logging
import os
import types
import uuid
from unittest.mock import patch

from django.core.exceptions import DisallowedHost, PermissionDenied
from django.db import OperationalError
from django.http import Http404
from django.test import SimpleTestCase

from common.sentry import build_before_send, init_sentry

# RFC 2606 guarantees .invalid never resolves, so nothing can reach a real
# ingest host even if a test escaped its fake transport.
FAKE_DSN = "https://not-a-real-key@o0.ingest.us.example.invalid/0"


class RecordingLog:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        try:
            self.warnings.append(message % args if args else message)
        except (TypeError, ValueError):
            self.warnings.append(message)


def exception_event(exc_type=ValueError, message="boom"):
    return {"exception": {"values": [{"type": exc_type.__name__, "value": message}]}}


def exception_hint(exc_type=ValueError, message="boom"):
    return {"exc_info": (exc_type, exc_type(message), None)}


def stub_sdk(record):
    integrations = types.ModuleType("sentry_sdk.integrations")
    django_integration = types.ModuleType("sentry_sdk.integrations.django")
    logging_integration = types.ModuleType("sentry_sdk.integrations.logging")

    class DjangoIntegration:
        identifier = "django"

    class LoggingIntegration:
        identifier = "logging"

        def __init__(self, level=None, event_level=None):
            self.level = level
            self.event_level = event_level

    django_integration.DjangoIntegration = DjangoIntegration
    logging_integration.LoggingIntegration = LoggingIntegration

    sdk = types.ModuleType("sentry_sdk")

    def init(**kwargs):
        record.append(kwargs)

    sdk.init = init
    sdk.integrations = integrations
    integrations.django = django_integration
    integrations.logging = logging_integration
    return {
        "sentry_sdk": sdk,
        "sentry_sdk.integrations": integrations,
        "sentry_sdk.integrations.django": django_integration,
        "sentry_sdk.integrations.logging": logging_integration,
    }


class InitWithoutDsnTests(SimpleTestCase):
    def test_no_dsn_is_inert_and_never_reaches_the_sdk(self):
        calls = []
        with patch.dict("sys.modules", stub_sdk(calls)):
            self.assertFalse(init_sentry(dsn="", log=RecordingLog()))
        self.assertEqual(calls, [])

    def test_an_absent_dsn_env_is_inert(self):
        with patch.dict(os.environ, {"SENTRY_DSN": ""}):
            self.assertFalse(init_sentry(log=RecordingLog()))


class InitWithDsnTests(SimpleTestCase):
    def test_the_reporting_shape_is_pinned(self):
        calls = []
        log = RecordingLog()
        with patch.dict("sys.modules", stub_sdk(calls)):
            live = init_sentry(
                dsn=FAKE_DSN,
                environment="staging",
                release="abc123",
                sample_rate=0.5,
                log=log,
            )
        self.assertTrue(live)
        self.assertEqual(len(calls), 1)
        options = calls[0]
        self.assertEqual(options["dsn"], FAKE_DSN)
        self.assertEqual(options["environment"], "staging")
        self.assertEqual(options["release"], "abc123")
        self.assertEqual(options["sample_rate"], 0.5)
        self.assertEqual(options["traces_sample_rate"], 0.0)
        self.assertIs(options["send_default_pii"], False)
        self.assertEqual(options["max_breadcrumbs"], 20)
        self.assertIs(options["attach_stacktrace"], False)
        self.assertTrue(callable(options["before_send"]))
        self.assertNotIn("transport", options)
        identifiers = [
            getattr(integration, "identifier", None)
            for integration in options["integrations"]
        ]
        self.assertEqual(identifiers, ["django", "logging"])

    def test_env_supplies_the_dsn_label_release_and_environment(self):
        calls = []
        env = {
            "SENTRY_DSN": FAKE_DSN,
            "SENTRY_ENVIRONMENT": "",
            "RELEASE_SHA": "",
            "GIT_SHA": "deploy-sha-1",
        }
        with patch.dict(os.environ, env):
            with patch.dict("sys.modules", stub_sdk(calls)):
                self.assertTrue(init_sentry(log=RecordingLog()))
        options = calls[0]
        self.assertEqual(options["dsn"], FAKE_DSN)
        self.assertEqual(options["environment"], "production")
        self.assertEqual(options["release"], "deploy-sha-1")

    def test_an_explicit_release_wins_over_the_env(self):
        calls = []
        env = {"SENTRY_DSN": FAKE_DSN, "RELEASE_SHA": "env-release"}
        with patch.dict(os.environ, env):
            with patch.dict("sys.modules", stub_sdk(calls)):
                self.assertTrue(
                    init_sentry(release="explicit-release", log=RecordingLog())
                )
        self.assertEqual(calls[0]["release"], "explicit-release")

    def test_an_injected_before_send_is_passed_through_unchanged(self):
        calls = []
        sentinel = lambda event, hint=None: event  # noqa: E731 - identity pin
        with patch.dict("sys.modules", stub_sdk(calls)):
            self.assertTrue(
                init_sentry(
                    dsn=FAKE_DSN,
                    before_send=sentinel,
                    log=RecordingLog(),
                )
            )
        self.assertIs(calls[0]["before_send"], sentinel)

    def test_an_injected_transport_is_passed_through_unchanged(self):
        calls = []
        transport = object()
        with patch.dict("sys.modules", stub_sdk(calls)):
            self.assertTrue(
                init_sentry(
                    dsn=FAKE_DSN,
                    transport=transport,
                    log=RecordingLog(),
                )
            )
        self.assertIs(calls[0]["transport"], transport)

    def test_a_missing_sdk_warns_and_stays_off(self):
        log = RecordingLog()
        with patch.dict(
            "sys.modules",
            {"sentry_sdk": None, "sentry_sdk.integrations": None},
        ):
            self.assertFalse(init_sentry(dsn=FAKE_DSN, log=log))
        self.assertEqual(len(log.warnings), 1)
        self.assertIn("sentry-sdk", log.warnings[0])

    def test_a_refused_dsn_warns_and_stays_off(self):
        log = RecordingLog()

        def refuse(**kwargs):
            raise ValueError("bad dsn")

        modules = stub_sdk([])
        modules["sentry_sdk"].init = refuse
        with patch.dict("sys.modules", modules):
            self.assertFalse(init_sentry(dsn=FAKE_DSN, log=log))
        self.assertEqual(len(log.warnings), 1)
        self.assertIn("rejected by the sentry-sdk", log.warnings[0])


class DenylistTests(SimpleTestCase):
    def build(self):
        return build_before_send(log=RecordingLog())

    def test_denylisted_exception_classes_are_dropped(self):
        guard = self.build()
        for exc_type in (
            Http404,
            PermissionDenied,
            DisallowedHost,
            BrokenPipeError,
            OperationalError,
        ):
            with self.subTest(exc_type=exc_type.__name__):
                self.assertIsNone(
                    guard(exception_event(exc_type), exception_hint(exc_type))
                )

    def test_a_subclass_of_a_denylisted_error_is_dropped_by_class(self):
        class CustomNotFound(Http404):
            pass

        guard = self.build()
        self.assertIsNone(
            guard(exception_event(CustomNotFound), exception_hint(CustomNotFound))
        )

    def test_a_serialised_event_is_denied_by_type_name(self):
        guard = self.build()
        self.assertIsNone(guard({"exception": {"values": [{"type": "Http404"}]}}))

    def test_a_serialised_event_without_a_type_is_not_denied(self):
        guard = self.build()
        event = {"exception": {"values": [{"type": "", "value": "plain"}]}}
        self.assertIs(guard(event), event)

    def test_health_check_paths_are_dropped(self):
        guard = self.build()
        event = {
            "request": {"url": "https://shop.example.test/health/"},
            "exception": {"values": [{"type": "ValueError", "value": "x"}]},
        }
        self.assertIsNone(guard(event, exception_hint(ValueError, "x")))

    def test_a_health_check_transaction_is_dropped(self):
        guard = self.build()
        event = {
            "transaction": "/healthz",
            "exception": {"values": [{"type": "ValueError", "value": "x"}]},
        }
        self.assertIsNone(guard(event, exception_hint(ValueError, "x")))

    def test_a_relative_health_path_is_dropped(self):
        guard = self.build()
        event = {
            "request": {"path": "/ping"},
            "exception": {"values": [{"type": "ValueError", "value": "x"}]},
        }
        self.assertIsNone(guard(event, exception_hint(ValueError, "x")))

    def test_a_normal_path_and_exception_pass(self):
        guard = self.build()
        event = {
            "request": {"url": "https://shop.example.test/api/products/"},
            "exception": {"values": [{"type": "ValueError", "value": "x"}]},
        }
        self.assertIs(guard(event, exception_hint(ValueError, "x")), event)


class NoLocalCeilingTests(SimpleTestCase):
    def test_repeated_identical_events_are_all_reported(self):
        guard = build_before_send(log=RecordingLog())
        for _ in range(60):
            event = exception_event(ValueError, "same")
            self.assertIs(guard(event, exception_hint(ValueError, "same")), event)
        self.assertIsNotNone(guard(exception_event(ValueError, "same")))


class GuardRobustnessTests(SimpleTestCase):
    def test_a_malformed_event_is_dropped_not_raised(self):
        log = RecordingLog()
        guard = build_before_send(log=log)
        self.assertIsNone(
            guard({"request": "not-a-mapping"}, exception_hint(ValueError, "x"))
        )
        self.assertEqual(len(log.warnings), 1)
        self.assertIn("denylist failed", log.warnings[0])

    def test_a_log_record_with_exc_info_is_handled(self):
        guard = build_before_send(log=RecordingLog())
        record = logging.LogRecord(
            "django.request", logging.ERROR, __file__, 1, "server error", None, None
        )
        record.exc_info = (Http404, Http404("gone"), None)
        self.assertIsNone(guard({"message": "server error"}, {"log_record": record}))

    def test_a_log_record_without_exc_info_is_handled(self):
        guard = build_before_send(log=RecordingLog())
        record = logging.LogRecord(
            "django.request", logging.ERROR, __file__, 1, "plain", None, None
        )
        event = {"message": "plain"}
        self.assertIs(guard(event, {"log_record": record}), event)

    def test_a_bare_message_event_is_handled(self):
        guard = build_before_send(log=RecordingLog())
        event = {"message": "bare"}
        self.assertIs(guard(event), event)

    def test_an_event_with_no_identity_is_kept(self):
        guard = build_before_send(log=RecordingLog())
        event = {"level": "error"}
        self.assertIs(guard(event), event)

    def test_an_exc_info_without_a_class_is_not_denied(self):
        guard = build_before_send(log=RecordingLog())
        event = {"message": "non-class"}
        self.assertIs(guard(event, {"exc_info": (42, "boom", None)}), event)

    def test_a_non_string_transaction_is_ignored(self):
        guard = build_before_send(log=RecordingLog())
        event = {"transaction": 123, "message": "kept"}
        self.assertIs(guard(event), event)

    def test_a_non_string_request_url_is_treated_as_no_path(self):
        guard = build_before_send(log=RecordingLog())
        event = {
            "request": {"url": 123},
            "exception": {"values": [{"type": "ValueError", "value": "x"}]},
        }
        self.assertIs(guard(event, exception_hint(ValueError, "x")), event)


class SampleRateValidationTests(SimpleTestCase):
    def init_with_rate(self, rate):
        calls = []
        log = RecordingLog()
        with patch.dict("sys.modules", stub_sdk(calls)):
            self.assertTrue(init_sentry(dsn=FAKE_DSN, sample_rate=rate, log=log))
        return calls[0]["sample_rate"], log

    def test_out_of_range_and_non_finite_rates_fall_back_to_one(self):
        for rate in (-0.5, 1.5, float("nan"), float("inf"), "nonsense", None):
            with self.subTest(rate=rate):
                effective, log = self.init_with_rate(rate)
                self.assertEqual(effective, 1.0)
                self.assertEqual(len(log.warnings), 1)
                self.assertIn("SENTRY_SAMPLE_RATE", log.warnings[0])

    def test_zero_is_preserved_as_an_explicit_disable(self):
        effective, log = self.init_with_rate(0.0)
        self.assertEqual(effective, 0.0)
        self.assertEqual(log.warnings, [])


class RealSdkTransportTests(SimpleTestCase):
    def setUp(self):
        import sentry_sdk
        from sentry_sdk.transport import Transport

        self.sentry_sdk = sentry_sdk

        class RecordingTransport(Transport):
            def __init__(self, options=None):
                super().__init__(options)
                self.envelopes = []

            def capture_envelope(self, envelope):
                self.envelopes.append(envelope)

        self.transport = RecordingTransport()
        self.addCleanup(self._reset)

    def _reset(self):
        try:
            self.sentry_sdk.get_client().close()
        finally:
            self.sentry_sdk.init(dsn="")

    def test_events_reach_the_fake_transport_and_duplicates_are_kept(self):
        live = init_sentry(
            dsn=FAKE_DSN,
            environment="test",
            release="p1-2",
            transport=self.transport,
            log=RecordingLog(),
        )
        self.assertTrue(live)
        self.assertIs(self.sentry_sdk.get_client().transport, self.transport)
        message = f"unique-{uuid.uuid4().hex}"
        for _ in range(2):
            try:
                raise ValueError(message)
            except ValueError:
                self.sentry_sdk.capture_exception()
            self.sentry_sdk.flush(timeout=5)
        self.assertEqual(len(self.transport.envelopes), 2)

    def test_a_denylisted_exception_does_not_reach_the_transport(self):
        live = init_sentry(
            dsn=FAKE_DSN,
            environment="test",
            release="p1-2",
            transport=self.transport,
            log=RecordingLog(),
        )
        self.assertTrue(live)
        self.sentry_sdk.capture_exception(Http404("gone"))
        self.sentry_sdk.flush(timeout=5)
        self.assertEqual(self.transport.envelopes, [])

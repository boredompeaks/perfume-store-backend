"""P1-2: error reporting with a hard, free-tier quota guard.

The guard is the contract, not the SDK. These pins drive it directly, with a
fake clock and an injected store, so the budgets and windows are provable
without waiting minutes or needing a transport. Two tests use the REAL SDK but
an injected ``Transport`` instance, which is the other half of the promise:
a configured tracker sends nothing over a socket in this suite.

The guard must also be boring under failure. A Redis outage, a malformed
event, or a broken store degrades to a local warning and drops the event; it
never raises inside ``before_send`` and never reports its own failure through
the SDK, because that would be the recursion the cap exists to prevent.
"""

import logging
import os
import time
import types
import uuid
from unittest.mock import patch

from django.core.exceptions import DisallowedHost, PermissionDenied
from django.db import OperationalError
from django.http import Http404
from django.test import SimpleTestCase

import common.sentry as sentry_module
from common.sentry import build_before_send, init_sentry

# RFC 2606 guarantees .invalid never resolves, so nothing can reach a real
# ingest host even if a test escaped its fake transport.
FAKE_DSN = "https://not-a-real-key@o0.ingest.us.example.invalid/0"


class RecordingLog:
    """Minimal logger stand-in: the guard only ever calls ``warning``."""

    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        try:
            self.warnings.append(message % args if args else message)
        except (TypeError, ValueError):
            self.warnings.append(message)


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def exception_event(exc_type=ValueError, message="boom"):
    return {"exception": {"values": [{"type": exc_type.__name__, "value": message}]}}


def exception_hint(exc_type=ValueError, message="boom"):
    return {"exc_info": (exc_type, exc_type(message), None)}


def stub_sdk(record):
    """A ``sentry_sdk`` package tree whose ``init`` only records its kwargs."""
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


class FakeRedis:
    """In-process stand-in for the two Redis primitives the store uses.

    It keeps the counters and the NX/EX marks on a fake clock, so two guard
    instances handed the SAME client share one budget exactly as two workers
    sharing one Redis server do.
    """

    def __init__(self, clock):
        self.clock = clock
        self.counts = {}
        self.marks = {}

    def pipeline(self):
        return FakePipeline(self)

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, ttl):
        return True

    def set(self, key, value, nx=False, ex=None):
        if nx and self.marks.get(key, 0) > self.clock():
            return None
        self.marks[key] = self.clock() + (ex or 0)
        return True


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.ops = []

    def incr(self, key):
        self.ops.append(("incr", key))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def execute(self):
        results = []
        for op in self.ops:
            if op[0] == "incr":
                results.append(self.client.incr(op[1]))
            else:
                results.append(self.client.expire(op[1], op[2]))
        return results


class FailingRedis:
    def pipeline(self):
        raise ConnectionError("redis is down")


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
                max_per_minute=3,
                max_per_hour=40,
                redis_url="",
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
        identifiers = [
            getattr(integration, "identifier", None)
            for integration in options["integrations"]
        ]
        self.assertEqual(identifiers, ["django", "logging"])

    def test_env_supplies_the_label_release_and_caps_when_not_passed(self):
        calls = []
        env = {
            "SENTRY_DSN": FAKE_DSN,
            "SENTRY_ENVIRONMENT": "",
            "RELEASE_SHA": "",
            "GIT_SHA": "deploy-sha-1",
            "SENTRY_MAX_EVENTS_PER_MINUTE": "7",
            "SENTRY_MAX_EVENTS_PER_HOUR": "70",
            "REDIS_URL": "",
        }
        with patch.dict(os.environ, env):
            with patch.dict("sys.modules", stub_sdk(calls)):
                self.assertTrue(init_sentry(log=RecordingLog()))
        options = calls[0]
        self.assertEqual(options["environment"], "production")
        self.assertEqual(options["release"], "deploy-sha-1")

    def test_an_injected_before_send_is_passed_through_unchanged(self):
        calls = []
        sentinel = lambda event, hint=None: event  # noqa: E731 - identity pin
        with patch.dict("sys.modules", stub_sdk(calls)):
            self.assertTrue(
                init_sentry(
                    dsn=FAKE_DSN,
                    redis_url="",
                    before_send=sentinel,
                    log=RecordingLog(),
                )
            )
        self.assertIs(calls[0]["before_send"], sentinel)

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

    def test_the_guard_reads_positive_caps_and_ignores_bad_values(self):
        self.assertEqual(
            sentry_module._positive_int_env("P1_2_MISSING", 10),
            10,
        )
        with patch.dict(os.environ, {"P1_2_CAP": "0"}):
            self.assertEqual(sentry_module._positive_int_env("P1_2_CAP", 10), 10)
        with patch.dict(os.environ, {"P1_2_CAP": "nonsense"}):
            self.assertEqual(sentry_module._positive_int_env("P1_2_CAP", 10), 10)
        with patch.dict(os.environ, {"P1_2_CAP": "4"}):
            self.assertEqual(sentry_module._positive_int_env("P1_2_CAP", 10), 4)


class DenylistTests(SimpleTestCase):
    def build(self):
        return build_before_send(redis_url="", clock=Clock())

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

    def test_a_serialised_event_is_denied_by_type_name(self):
        # No hint, only the event's own exception value: the name branch is
        # what keeps a denylisted error from slipping through.
        guard = self.build()
        self.assertIsNone(guard({"exception": {"values": [{"type": "Http404"}]}}))

    def test_health_check_paths_are_dropped(self):
        guard = self.build()
        event = {
            "request": {"url": "https://shop.example.test/health/"},
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


class DedupeTests(SimpleTestCase):
    def test_an_identical_event_is_dropped_then_allowed_after_the_window(self):
        clock = Clock()
        log = RecordingLog()
        guard = build_before_send(
            max_per_minute=100, max_per_hour=100, redis_url="", clock=clock, log=log
        )
        first = exception_event(ValueError, "same")
        second = exception_event(ValueError, "same")
        self.assertIs(guard(first, exception_hint(ValueError, "same")), first)
        self.assertIsNone(guard(second, exception_hint(ValueError, "same")))
        clock.advance(61)
        third = exception_event(ValueError, "same")
        self.assertIs(guard(third, exception_hint(ValueError, "same")), third)

    def test_a_different_message_is_not_a_duplicate(self):
        guard = build_before_send(
            max_per_minute=100, max_per_hour=100, redis_url="", clock=Clock()
        )
        first = exception_event(ValueError, "one")
        second = exception_event(ValueError, "two")
        self.assertIs(guard(first, exception_hint(ValueError, "one")), first)
        self.assertIs(guard(second, exception_hint(ValueError, "two")), second)


class CapTests(SimpleTestCase):
    def test_the_eleventh_event_in_a_minute_is_dropped(self):
        clock = Clock()
        log = RecordingLog()
        guard = build_before_send(
            max_per_minute=10, max_per_hour=1000, redis_url="", clock=clock, log=log
        )
        allowed = 0
        for index in range(10):
            message = f"minute-{index}"
            event = exception_event(ValueError, message)
            allowed += guard(event, exception_hint(ValueError, message)) is event
        self.assertEqual(allowed, 10)
        eleventh = exception_event(ValueError, "minute-11")
        self.assertIsNone(guard(eleventh, exception_hint(ValueError, "minute-11")))
        # A cap hit is one local warning per window, not one per dropped event.
        self.assertIsNone(
            guard(exception_event(ValueError, "minute-12"), exception_hint(ValueError))
        )
        self.assertEqual(len(log.warnings), 1)
        self.assertIn("10/minute", log.warnings[0])

    def test_the_hour_cap_is_independent_of_the_minute_cap(self):
        clock = Clock()
        log = RecordingLog()
        guard = build_before_send(
            max_per_minute=1000, max_per_hour=5, redis_url="", clock=clock, log=log
        )
        for index in range(5):
            message = f"hour-{index}"
            event = exception_event(ValueError, message)
            self.assertIs(guard(event, exception_hint(ValueError, message)), event)
        sixth = exception_event(ValueError, "hour-5")
        self.assertIsNone(guard(sixth, exception_hint(ValueError, "hour-5")))
        self.assertEqual(len(log.warnings), 1)
        self.assertIn("5/hour", log.warnings[0])
        # The next minute opens a fresh minute window, but the hour is still
        # spent: the tighter of the two bounds is what admits traffic.
        clock.advance(61)
        seventh = exception_event(ValueError, "hour-6")
        self.assertIsNone(guard(seventh, exception_hint(ValueError, "hour-6")))


class RedisSharedCounterTests(SimpleTestCase):
    def test_two_workers_spend_one_budget_through_a_shared_store(self):
        clock = Clock()
        client = FakeRedis(clock)
        guard_a = build_before_send(
            max_per_minute=3,
            max_per_hour=100,
            redis_client=client,
            key_prefix="p1-2-shared",
            clock=clock,
            log=RecordingLog(),
        )
        guard_b = build_before_send(
            max_per_minute=3,
            max_per_hour=100,
            redis_client=client,
            key_prefix="p1-2-shared",
            clock=clock,
            log=RecordingLog(),
        )
        for index in range(3):
            message = f"worker-a-{index}"
            event = exception_event(ValueError, message)
            self.assertIs(guard_a(event, exception_hint(ValueError, message)), event)
        # Worker B has its own guard object but the same Redis counter, so its
        # fourth distinct event is over the shared 3/minute budget.
        fourth = exception_event(ValueError, "worker-b-3")
        self.assertIsNone(guard_b(fourth, exception_hint(ValueError, "worker-b-3")))

    def test_dedupe_is_shared_between_workers_too(self):
        clock = Clock()
        client = FakeRedis(clock)
        prefix = "p1-2-shared-dedupe"
        guard_a = build_before_send(
            redis_client=client,
            key_prefix=prefix,
            clock=clock,
            log=RecordingLog(),
        )
        guard_b = build_before_send(
            redis_client=client,
            key_prefix=prefix,
            clock=clock,
            log=RecordingLog(),
        )
        first = exception_event(ValueError, "shared-dup")
        second = exception_event(ValueError, "shared-dup")
        self.assertIs(guard_a(first, exception_hint(ValueError, "shared-dup")), first)
        self.assertIsNone(guard_b(second, exception_hint(ValueError, "shared-dup")))

    def test_a_dead_redis_degrades_to_the_process_counter_without_raising(self):
        log = RecordingLog()
        guard = build_before_send(
            max_per_minute=100,
            max_per_hour=100,
            redis_client=FailingRedis(),
            clock=Clock(),
            log=log,
        )
        for index in range(2):
            message = f"degraded-{index}"
            event = exception_event(ValueError, message)
            self.assertIs(guard(event, exception_hint(ValueError, message)), event)
        self.assertEqual(len(log.warnings), 1)
        self.assertIn("Redis unavailable", log.warnings[0])

    def test_an_unusable_redis_url_falls_back_before_any_connection(self):
        log = RecordingLog()
        guard = build_before_send(redis_url="not-a-url", clock=Clock(), log=log)
        event = exception_event(ValueError, "bad-url")
        self.assertIs(guard(event, exception_hint(ValueError, "bad-url")), event)
        self.assertEqual(len(log.warnings), 1)
        self.assertIn("cannot create a Redis client", log.warnings[0])


class RealRedisSharedCounterTests(SimpleTestCase):
    """The fake above proves the contract; this proves the real server agrees."""

    def setUp(self):
        url = (os.environ.get("REDIS_URL") or "").strip()
        if not url:
            self.skipTest("REDIS_URL is not declared; shared-counter pin not armed")
        import redis

        try:
            self.client = redis.Redis.from_url(
                url, socket_connect_timeout=3, socket_timeout=3
            )
            self.client.ping()
        except Exception as error:
            self.skipTest(f"REDIS_URL declared but no Redis answered ({error})")
        self.prefix = f"p1-2-test:{os.getpid()}:{uuid.uuid4().hex}:"
        self.addCleanup(self._cleanup, url)

    def _cleanup(self, url):
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
        for key in client.scan_iter(f"{self.prefix}*"):
            client.delete(key)
        client.close()

    def test_zero_events_are_lost_and_the_cap_is_one_budget(self):
        clock = Clock(time.time())
        guard_a = build_before_send(
            max_per_minute=2,
            max_per_hour=100,
            redis_url=os.environ["REDIS_URL"],
            key_prefix=self.prefix,
            clock=clock,
            log=RecordingLog(),
        )
        guard_b = build_before_send(
            max_per_minute=2,
            max_per_hour=100,
            redis_url=os.environ["REDIS_URL"],
            key_prefix=self.prefix,
            clock=clock,
            log=RecordingLog(),
        )
        for index in range(2):
            message = f"real-a-{uuid.uuid4().hex}"
            event = exception_event(ValueError, message)
            self.assertIs(guard_a(event, exception_hint(ValueError, message)), event)
        third_message = f"real-b-{uuid.uuid4().hex}"
        third = exception_event(ValueError, third_message)
        self.assertIsNone(guard_b(third, exception_hint(ValueError, third_message)))


class GuardRobustnessTests(SimpleTestCase):
    def test_a_malformed_event_is_dropped_not_raised(self):
        log = RecordingLog()
        guard = build_before_send(redis_url="", clock=Clock(), log=log)
        self.assertIsNone(
            guard({"request": "not-a-mapping"}, exception_hint(ValueError, "x"))
        )
        self.assertEqual(len(log.warnings), 1)
        self.assertIn("quota guard failed", log.warnings[0])

    def test_a_log_record_with_exc_info_is_handled(self):
        guard = build_before_send(
            max_per_minute=100, max_per_hour=100, redis_url="", clock=Clock()
        )
        record = logging.LogRecord(
            "django.request", logging.ERROR, __file__, 1, "server error", None, None
        )
        record.exc_info = (Http404, Http404("gone"), None)
        self.assertIsNone(guard({"message": "server error"}, {"log_record": record}))

    def test_a_log_record_without_exc_info_is_handled(self):
        guard = build_before_send(
            max_per_minute=100, max_per_hour=100, redis_url="", clock=Clock()
        )
        record = logging.LogRecord(
            "django.request", logging.ERROR, __file__, 1, "plain", None, None
        )
        event = {"message": "plain"}
        self.assertIs(guard(event, {"log_record": record}), event)

    def test_a_bare_message_event_is_handled(self):
        guard = build_before_send(
            max_per_minute=100, max_per_hour=100, redis_url="", clock=Clock()
        )
        event = {"message": "bare"}
        self.assertIs(guard(event), event)

    def test_a_non_string_request_url_is_treated_as_no_path(self):
        guard = build_before_send(
            max_per_minute=100, max_per_hour=100, redis_url="", clock=Clock()
        )
        event = {
            "request": {"url": 123},
            "exception": {"values": [{"type": "ValueError", "value": "x"}]},
        }
        self.assertIs(guard(event, exception_hint(ValueError, "x")), event)


class StorePruningTests(SimpleTestCase):
    def test_minute_buckets_are_bounded(self):
        store = sentry_module._ProcessStore(Clock())
        for bucket in range(65):
            store.hit("minute", bucket, 120)
        self.assertLessEqual(len(store._counts), 64)

    def test_expired_marks_are_pruned(self):
        clock = Clock()
        store = sentry_module._ProcessStore(clock)
        for index in range(1025):
            store.mark(f"mark-{index}", 60)
        clock.advance(61)
        store.mark("fresh", 60)
        self.assertEqual(len(store._marks), 1)


class RealSdkTransportTests(SimpleTestCase):
    """With the real SDK, an injected transport is the only sink."""

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

    def test_events_reach_the_fake_transport_and_duplicates_do_not(self):
        live = init_sentry(
            dsn=FAKE_DSN,
            environment="test",
            release="p1-2",
            max_per_minute=100,
            max_per_hour=100,
            redis_url="",
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
        # One accepted event, one identical event dropped by the 60s window.
        self.assertEqual(len(self.transport.envelopes), 1)

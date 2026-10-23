"""Error reporting with a hard, free-tier quota guard.

The Sentry SDK is a liability without a ceiling. Its free plan bills in
events, and the failure mode that costs the most is not a normal bug: it is
one noisy exception inside a hot loop, which can spend a month's quota in
minutes while the app is otherwise healthy. This module therefore separates
two decisions that the SDK usually fuses:

* ``init_sentry`` is the ONLY place the SDK is initialised. It is inert with
  no DSN, imports the SDK inside the DSN gate, and pins the reporting shape
  (no PII, no traces, no stack traces, a small breadcrumb buffer).
* ``build_before_send`` is the ceiling. It runs in-process before an event is
  handed to a transport and enforces, in order: a denylist, a 60-second
  duplicate window, and a per-minute AND per-hour cap. The caps live in a
  shared Redis counter when one is configured, because a per-worker cap is
  not a cap: N workers would each spend the full budget. Without Redis the
  counter is per-process, which is the safe local/CI default.

Nothing here imports the SDK, opens a socket, or reads Django settings at
module import time. ``config.settings`` imports this module while it is still
being executed, so a module-scope dependency on either would break the boot
in exactly the deployment that is supposed to be observable.

The guard never raises and never reports its own failure through the SDK
(that would be the recursion it exists to prevent). A quota hit is a local
warning, once per window; an internal guard failure drops the event and says
so once.
"""

import hashlib
import logging
import os
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_MAX_EVENTS_PER_MINUTE = 10
DEFAULT_MAX_EVENTS_PER_HOUR = 200
DEDUPE_WINDOW_SECONDS = 60

# Paths whose errors are noise, not signal: orchestrator probes hit /health/
# on a timer, and one unhealthy minute would otherwise be hundreds of events.
DENIED_PATH_PREFIXES = (
    "/health",
    "/healthz",
    "/livez",
    "/readyz",
    "/readiness",
    "/liveness",
    "/ping",
)

# Matched both by class (an exception object is available) and by name (a
# serialised event only carries the type as a string). Keeping both means a
# denylisted error cannot slip through the hint-less path.
DENIED_TYPE_NAMES = frozenset(
    {
        "Http404",
        "PermissionDenied",
        "DisallowedHost",
        "BrokenPipeError",
        "OperationalError",
    }
)

_DENIED_EXCEPTIONS = None


def _denied_exception_types():
    """Return the exception classes the guard drops, resolved on first use.

    Imported lazily: this module is imported by settings while settings is
    still being executed, and touching ``django.db`` then is a needless
    boot-time dependency.
    """
    global _DENIED_EXCEPTIONS
    if _DENIED_EXCEPTIONS is None:
        from django.core.exceptions import DisallowedHost, PermissionDenied
        from django.db import OperationalError
        from django.http import Http404

        _DENIED_EXCEPTIONS = (
            Http404,
            PermissionDenied,
            DisallowedHost,
            BrokenPipeError,
            OperationalError,
        )
    return _DENIED_EXCEPTIONS


class _ProcessStore:
    """Per-process fallback: enough for one worker, honest about its scope."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._counts = {}
        self._marks = {}

    def hit(self, kind, bucket, ttl):
        key = (kind, bucket)
        self._counts[key] = self._counts.get(key, 0) + 1
        if len(self._counts) > 64:
            self._counts = {
                stored: count
                for stored, count in self._counts.items()
                if stored[0] != kind or stored[1] > bucket - 1
            }
        return self._counts[key]

    def mark(self, key, ttl):
        now = self._clock()
        seen = self._marks.get(key)
        if seen is not None and now - seen < ttl:
            return False
        self._marks[key] = now
        if len(self._marks) > 1024:
            self._marks = {
                stored: stamp
                for stored, stamp in self._marks.items()
                if now - stamp < ttl
            }
        return True


class _RedisStore:
    """Shared counters, so N workers spend one budget rather than N budgets.

    Bucket keys are fixed windows (``minute:<bucket>``, ``hour:<bucket>``) and
    carry their own expiry, so there is no cleanup job and a worker that dies
    cannot strand a counter. Dedupe is ``SET NX EX`` for the same reason.
    """

    def __init__(self, client, clock=time.time, key_prefix="sentry:quota:"):
        self._client = client
        self._clock = clock
        self._prefix = key_prefix if key_prefix.endswith(":") else f"{key_prefix}:"

    def hit(self, kind, bucket, ttl):
        key = f"{self._prefix}{kind}:{bucket}"
        pipeline = self._client.pipeline()
        pipeline.incr(key)
        pipeline.expire(key, ttl)
        return int(pipeline.execute()[0])

    def mark(self, key, ttl):
        return bool(self._client.set(f"{self._prefix}{key}", "1", nx=True, ex=ttl))


class _FallbackStore:
    """Use Redis while it answers, then spend the rest of the process locally.

    A Redis outage must not stop error reporting (that would hide the very
    outage) and must not raise inside ``before_send``. It also must not log on
    every event: the first failure names the problem, and the process degrades
    to the per-process counter until it restarts.
    """

    def __init__(self, primary, fallback, log):
        self._primary = primary
        self._fallback = fallback
        self._log = log
        self._degraded = False

    def _delegate(self, method, *args):
        if not self._degraded:
            try:
                return getattr(self._primary, method)(*args)
            except Exception as error:
                self._degraded = True
                self._log.warning(
                    "Sentry quota counter: Redis unavailable (%s: %s); "
                    "using per-process counters for this process.",
                    type(error).__name__,
                    error,
                )
        return getattr(self._fallback, method)(*args)

    def hit(self, kind, bucket, ttl):
        return self._delegate("hit", kind, bucket, ttl)

    def mark(self, key, ttl):
        return self._delegate("mark", key, ttl)


def _make_store(redis_url, redis_client, clock, log, key_prefix):
    process = _ProcessStore(clock)
    if redis_client is not None:
        return _FallbackStore(
            _RedisStore(redis_client, clock, key_prefix), process, log
        )
    if redis_url:
        try:
            import redis

            client = redis.Redis.from_url(
                redis_url, socket_connect_timeout=2, socket_timeout=2
            )
        except Exception as error:
            log.warning(
                "Sentry quota counter: cannot create a Redis client (%s: %s); "
                "using per-process counters.",
                type(error).__name__,
                error,
            )
            return process
        return _FallbackStore(_RedisStore(client, clock, key_prefix), process, log)
    return process


def _exception_identity(event, hint):
    """Return ``(class_or_None, type_name, message)`` for an event.

    The hint is the authoritative source when the SDK has a live exception.
    A serialised event and a log record are the two fallbacks; every branch
    keeps a type NAME so the denylist and the dedupe key stay populated even
    when only a string survives.
    """
    hint = hint or {}
    exc_info = hint.get("exc_info")
    if exc_info and exc_info[0] is not None:
        exc_type, exc_value = exc_info[0], exc_info[1]
        return (
            exc_type,
            getattr(exc_type, "__name__", str(exc_type)),
            str(exc_value or ""),
        )
    values = (event.get("exception") or {}).get("values") or []
    if values:
        last = values[-1] or {}
        return None, last.get("type") or "Exception", last.get("value") or ""
    record = hint.get("log_record")
    if record is not None:
        record_exc = getattr(record, "exc_info", None)
        if record_exc and record_exc[0] is not None:
            return (
                record_exc[0],
                getattr(record_exc[0], "__name__", str(record_exc[0])),
                str(record_exc[1] or ""),
            )
        message = record.getMessage() if hasattr(record, "getMessage") else str(record)
        return None, "log", message
    return None, "event", event.get("message") or ""


def _is_denied_exception(exc_type, type_name):
    if type_name in DENIED_TYPE_NAMES:
        return True
    if exc_type is None or not isinstance(exc_type, type):
        return False
    return issubclass(exc_type, _denied_exception_types())


def _request_path(event):
    request = event.get("request") or {}
    url = request.get("url") or request.get("path") or ""
    if not isinstance(url, str):
        return ""
    parsed = urlparse(url)
    return parsed.path if parsed.scheme or parsed.netloc else url


def _is_denied_path(event):
    candidates = [event.get("transaction") or "", _request_path(event)]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith(DENIED_PATH_PREFIXES):
            return True
    return False


def _signature(type_name, message):
    raw = f"{type_name}\0{message}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


def build_before_send(
    *,
    max_per_minute=DEFAULT_MAX_EVENTS_PER_MINUTE,
    max_per_hour=DEFAULT_MAX_EVENTS_PER_HOUR,
    dedupe_seconds=DEDUPE_WINDOW_SECONDS,
    redis_url=None,
    redis_client=None,
    key_prefix="sentry:quota:",
    clock=time.time,
    log=None,
):
    """Return a ``before_send`` hook that is a ceiling, not a filter.

    Order is deliberate: a denylisted event must not spend quota, and a
    duplicate must not either. The minute cap is checked before the hour cap
    so a burst is stopped at the tighter bound.
    """
    log = log or logger
    store = _make_store(redis_url, redis_client, clock, log, key_prefix)
    warned = set()

    def warn_once(window, message, *args):
        if window in warned:
            return
        warned.add(window)
        log.warning(message, *args)

    def before_send(event, hint=None):
        try:
            event = event or {}
            exc_type, type_name, message = _exception_identity(event, hint)
            if _is_denied_exception(exc_type, type_name):
                return None
            if _is_denied_path(event):
                return None

            now = clock()
            minute_bucket = int(now // 60)
            hour_bucket = int(now // 3600)
            if not store.mark(
                f"dedupe:{_signature(type_name, message)}", dedupe_seconds
            ):
                return None
            if store.hit("minute", minute_bucket, 120) > max_per_minute:
                warn_once(
                    ("minute", minute_bucket),
                    "Sentry quota: dropped events beyond %d/minute.",
                    max_per_minute,
                )
                return None
            if store.hit("hour", hour_bucket, 7200) > max_per_hour:
                warn_once(
                    ("hour", hour_bucket),
                    "Sentry quota: dropped events beyond %d/hour.",
                    max_per_hour,
                )
                return None
            return event
        except Exception as error:
            log.warning(
                "Sentry quota guard failed (%s: %s); dropping the event.",
                type(error).__name__,
                error,
            )
            return None

    return before_send


def _positive_int_env(name, default):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def init_sentry(
    *,
    dsn=None,
    environment=None,
    release=None,
    sample_rate=1.0,
    traces_sample_rate=0.0,
    max_per_minute=None,
    max_per_hour=None,
    redis_url=None,
    before_send=None,
    transport=None,
    log=None,
):
    """Initialise the error tracker when a DSN is configured; inert otherwise.

    Returns True when the tracker is live. The SDK is imported INSIDE the DSN
    gate, so an unconfigured process (local development, CI, the test suite)
    never loads it and never touches a transport. Both ways of failing to
    report degrade to a warning rather than a dead boot: an SDK that is not
    installed, and a DSN its own parser refuses. ``init_sentry`` is called
    while ``config.settings`` is executing, so a raise here is a dead
    deployment, not one broken worker.

    A ``transport`` may be injected (tests use it to prove nothing is sent
    over a socket); it is passed straight to the SDK and takes no other
    behaviour with it.
    """
    log = log or logger
    if dsn is None:
        dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    if environment is None:
        environment = (os.getenv("SENTRY_ENVIRONMENT") or "").strip() or "production"
    if release is None:
        release = (
            (os.getenv("RELEASE_SHA") or "").strip()
            or (os.getenv("GIT_SHA") or "").strip()
            or None
        )
    if max_per_minute is None:
        max_per_minute = _positive_int_env(
            "SENTRY_MAX_EVENTS_PER_MINUTE", DEFAULT_MAX_EVENTS_PER_MINUTE
        )
    if max_per_hour is None:
        max_per_hour = _positive_int_env(
            "SENTRY_MAX_EVENTS_PER_HOUR", DEFAULT_MAX_EVENTS_PER_HOUR
        )
    if redis_url is None:
        redis_url = (os.getenv("REDIS_URL") or "").strip() or None
    if before_send is None:
        before_send = build_before_send(
            max_per_minute=max_per_minute,
            max_per_hour=max_per_hour,
            redis_url=redis_url,
            log=log,
        )

    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        options = {
            "dsn": dsn,
            "environment": environment,
            "release": release,
            "sample_rate": sample_rate,
            # Errors only: this app instruments no transactions, and a
            # non-zero trace rate would bill for telemetry nothing consumes.
            "traces_sample_rate": traces_sample_rate,
            # Request bodies, cookies, headers and user identifiers stay in
            # the process unless an operator deliberately opts in.
            "send_default_pii": False,
            # A breadcrumb buffer is bounded on purpose: 100 default crumbs
            # times a noisy event is most of a free-tier payload.
            "max_breadcrumbs": 20,
            # The stack trace is already in the exception; attaching a second
            # copy inflates every event for no diagnostic gain.
            "attach_stacktrace": False,
            "before_send": before_send,
            "integrations": [
                DjangoIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
        }
        if transport is not None:
            options["transport"] = transport
        sentry_sdk.init(**options)
    except ImportError:
        log.warning(
            "SENTRY_DSN is set but the sentry-sdk package is not installed; "
            "error tracking stays off for this process."
        )
        return False
    except Exception as error:
        log.warning(
            "SENTRY_DSN was rejected by the sentry-sdk (%s: %s); error "
            "tracking stays off for this process. Check the value in .env.",
            type(error).__name__,
            error,
        )
        return False
    return True

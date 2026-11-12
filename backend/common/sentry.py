"""Error reporting with a minimal denylist and Sentry-side spend controls.

The Sentry free plan bills in events, and one noisy exception inside a hot
loop can spend a month's quota in minutes. The tempting local answer - a
client-side cap that stops sending after N events - is the wrong one: it goes
quiet exactly when errors spike, which is when telemetry matters most. Spend
is controlled where it can be enforced without hiding an incident:

* the project's own rate limits (per DSN/client key) and inbound filters are
  the ceiling, configured in Sentry rather than in this process;
* ``SENTRY_SAMPLE_RATE`` keeps a fixed fraction of events, which bounds cost
  without ever turning reporting off completely;
* ``build_before_send`` is only a small denylist of known noise (exception
  types and health-check paths), so noise does not spend quota in the first
  place. It never drops an event because a local counter says so.

``init_sentry`` is the ONLY place the SDK is initialised. It is inert with no
DSN, imports the SDK inside the DSN gate, and pins the reporting shape (no
PII, no traces, no stack traces, a small breadcrumb buffer).

Nothing here imports the SDK, opens a socket, or reads Django settings at
module import time. ``config.settings`` imports this module while it is still
being executed, so a module-scope dependency on either would break the boot
in exactly the deployment that is supposed to be observable.

The denylist never raises and never reports its own failure through the SDK
(that would be the recursion it exists to prevent). An internal failure drops
the event and logs a warning rather than raising.
"""

import logging
import math
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

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


def _exception_identity(event, hint):
    """Return ``(class_or_None, type_name, message)`` for an event.

    The hint is the authoritative source when the SDK has a live exception.
    A serialised event and a log record are the two fallbacks; every branch
    keeps a type NAME so the denylist stays populated even when only a string
    survives.
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


def build_before_send(*, log=None):
    """Return a ``before_send`` hook that drops known noise and nothing else.

    There is deliberately no counter here. A local cap cannot distinguish a
    bug from an incident, and the events it drops are invisible to Sentry, so
    an attack that produces errors would also silence its own report. The
    project's rate limits and ``SENTRY_SAMPLE_RATE`` bound spend instead.
    """
    log = log or logger

    def before_send(event, hint=None):
        try:
            event = event or {}
            exc_type, type_name, _ = _exception_identity(event, hint)
            if _is_denied_exception(exc_type, type_name):
                return None
            if _is_denied_path(event):
                return None
            return event
        except Exception as error:
            log.warning(
                "Sentry denylist failed (%s: %s); dropping the event.",
                type(error).__name__,
                error,
            )
            return None

    return before_send


def _validated_sample_rate(sample_rate, log):
    """Return a usable error sample rate, warning on one Sentry cannot use.

    The SDK keeps every event for a rate above 1, ``NaN`` or infinity, and
    drops every event for a negative rate. With the local cap gone, either
    mistake would go unnoticed in the only remaining spend control, so both
    fall back to the documented default. ``0.0`` stays valid as an explicit
    "send no errors" setting.
    """
    try:
        value = float(sample_rate)
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        log.warning(
            "Ignoring invalid Sentry sample rate %r; using 1.0. "
            "Check SENTRY_SAMPLE_RATE.",
            sample_rate,
        )
        return 1.0
    return value


def init_sentry(
    *,
    dsn=None,
    environment=None,
    release=None,
    sample_rate=1.0,
    traces_sample_rate=0.0,
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

    ``sample_rate`` is the spend knob: it is Sentry's own client-side sampling,
    so a fraction of events is kept instead of reporting going dark after an
    arbitrary count. An invalid value falls back to 1.0 with a warning rather
    than reaching the SDK, because the SDK treats a negative rate as "drop
    every event" and a rate above 1 or NaN as "keep every event". A
    ``transport`` may be injected (tests use it to prove nothing is sent over
    a socket); it is passed straight to the SDK and takes no other behaviour
    with it.
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
    if before_send is None:
        before_send = build_before_send(log=log)
    sample_rate = _validated_sample_rate(sample_rate, log)

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

"""Error reporting with a small noise denylist and Sentry-side spend controls.

There is no local event cap on purpose: it would go quiet exactly during an
incident, so Sentry's rate limits and sampling bound spend instead.
"""

import logging
import math
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Probe paths: one unhealthy minute would otherwise be hundreds of events.
DENIED_PATH_PREFIXES = (
    "/health",
    "/healthz",
    "/livez",
    "/readyz",
    "/readiness",
    "/liveness",
    "/ping",
)

# Matched by class and by name, because a serialised event carries only the
# type string.
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
    """Return the dropped exception classes, resolved lazily: settings imports
    this module mid-execution, so touching ``django.db`` at module scope is out."""
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
    """Return ``(class_or_None, type_name, message)``; the hint is
    authoritative, with serialised events and log records as fallbacks."""
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

    Deliberately no local counter: it would silence its own incident, so rate
    limits and ``SENTRY_SAMPLE_RATE`` bound spend.
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
    """Return a usable sample rate; invalid, non-finite or out-of-range values
    fall back to 1.0 with a warning (the SDK would silently mishandle them)."""
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

    The SDK is imported inside the DSN gate. An SDK that is missing or a DSN its
    parser refuses warns instead of killing the boot, because settings calls this
    mid-import. A ``transport`` may be injected for tests.
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
            # Errors only: no transactions are instrumented, so tracing would
            # bill for telemetry nothing consumes.
            "traces_sample_rate": traces_sample_rate,
            # PII stays in-process unless an operator opts in.
            "send_default_pii": False,
            # 100 default crumbs times a noisy event is most of a free-tier
            # payload.
            "max_breadcrumbs": 20,
            # The exception already carries the stack trace.
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

"""Per-request correlation IDs (SPEC-20-3 [R-20.26]).

Before this module a staff action, its log lines and its audit row could only
be tied together by timestamp and luck. Every request now carries an id that

- is read from an inbound ``X-Request-ID`` when the caller already has one
  (an edge/load balancer that traces its own requests keeps ITS id), or
  minted here otherwise;
- is exposed on ``request.request_id`` and echoed back on the response, so a
  user can quote the id from a failed request and an operator can find the
  matching lines;
- rides the logging formatter (:class:`RequestIDFormatter`), so every log
  line emitted while serving the request is attributable without touching
  each call site's format string;
- is stored on the audit row, so the trail can be joined to the logs of the
  request that produced it.

The inbound value is attacker-controlled: it is untrusted input that ends up
in a header, in every log line and in the database, so it is accepted only
when it matches :data:`_VALID_REQUEST_ID` — a bounded, injection-free
charset. Anything else (too long, a space, a newline, angle brackets for a
crafted header split) is discarded in favour of a freshly generated id
rather than echoed.
"""

import logging
import re
import uuid
from contextvars import ContextVar

from django.core.handlers.exception import response_for_exception

# The header name used in both directions: read inbound, written outbound.
REQUEST_ID_HEADER = "X-Request-ID"
# META key Django derives from the header above (X-Request-ID -> HTTP_X_REQUEST_ID).
REQUEST_ID_META_KEY = "HTTP_X_REQUEST_ID"

# A trace id is an opaque token, not prose: 64 characters is far beyond any
# real trace id (a W3C trace-id is 32 hex chars) and caps what a caller can
# push into every log line, the response header and the audit column.
REQUEST_ID_MAX_LENGTH = 64

# Anchored on both ends (\A/\Z, not ^/$) and restricted to an identifier
# charset, so a value carrying CR/LF, a space or any header-splitting
# character is rejected outright instead of sanitised into something that
# merely looks safe.
_VALID_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._:-]{1,%d}\Z" % REQUEST_ID_MAX_LENGTH)

# Rendered in place of the id for records emitted outside a request (startup
# warnings, management commands) so the format string never fails on a
# missing attribute.
NO_REQUEST_ID = "-"

# A ContextVar rather than threading.local: the audit writer and the log
# formatter are called from ordinary code that never sees the request
# object, and ContextVar also carries the id correctly across
# async/thread boundaries the framework may use.
_request_id = ContextVar("request_id", default="")


def current_request_id(default=""):
    """The correlation id of the request being served, or ``default``."""
    return _request_id.get() or default


def sanitize_request_id(value):
    """Return ``value`` when it is safe to propagate, else an empty string.

    Callers fall back to a generated id on an empty result, so an invalid
    inbound header degrades to "no correlation from the caller" instead of
    failing the request.
    """
    if not value:
        return ""
    candidate = value.strip()
    if not _VALID_REQUEST_ID.match(candidate):
        return ""
    return candidate


def new_request_id():
    """A fresh opaque correlation id (32 hex chars, no dashes)."""
    return uuid.uuid4().hex


class RequestIDFormatter(logging.Formatter):
    """Formatter that stamps the current correlation id on every record.

    Injected via ``()`` in ``LOGGING`` rather than hand-written into each
    call site: a per-call-site format change is exactly how correlation
    silently stops covering new lines. Records emitted outside a request get
    :data:`NO_REQUEST_ID`, so the field is always present.
    """

    def format(self, record):
        record.request_id = current_request_id(default=NO_REQUEST_ID)
        return super().format(record)


class RequestIDMiddleware:
    """Attach a correlation id to every request and response.

    Registered first in ``MIDDLEWARE`` so the id is on responses produced
    *above* the view as well — the security redirect, the CORS preflight,
    the admin login bounce — not only on view output.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = (
            sanitize_request_id(request.META.get(REQUEST_ID_META_KEY))
            or new_request_id()
        )
        # Set on the request for views/loggers that want it, and in a
        # ContextVar for the writers (audit) and the formatter, which have no
        # access to the request object.
        request.request_id = request_id
        token = _request_id.set(request_id)
        try:
            try:
                response = self.get_response(request)
            except Exception as exc:
                # An exception that reaches the OUTERMOST middleware has not
                # been converted to a response yet (Django converts it only
                # above us). Building the same response here — via
                # ``response_for_exception``, which also fires
                # ``got_request_exception`` and therefore keeps the 500 log
                # line — is what makes "every response carries the id" true
                # on the unhandled-exception path too.
                response = response_for_exception(request, exc)
        finally:
            # Reset before returning: the id must not leak into the next
            # request handled by this worker thread.
            _request_id.reset(token)
        response[REQUEST_ID_HEADER] = request_id
        return response

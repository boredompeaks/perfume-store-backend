"""Per-request correlation IDs.

The inbound ``X-Request-ID`` is attacker-controlled, so only a bounded,
injection-free value is accepted; anything else is replaced with a fresh id.
"""

import logging
import re
import uuid
from contextvars import ContextVar

from django.core.handlers.exception import response_for_exception

# Header name read inbound and echoed outbound.
REQUEST_ID_HEADER = "X-Request-ID"
# META key Django derives from the header.
REQUEST_ID_META_KEY = "HTTP_X_REQUEST_ID"

# Far beyond any real trace id (a W3C trace-id is 32 hex chars), and caps what a
# caller can push into every log line and audit column.
REQUEST_ID_MAX_LENGTH = 64

# Anchored with an identifier charset, so CR/LF or header-splitting characters
# are rejected rather than sanitized into something that looks safe.
_VALID_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._:-]{1,%d}\Z" % REQUEST_ID_MAX_LENGTH)

# Stand-in for records emitted outside a request, so the format never fails.
NO_REQUEST_ID = "-"

# ContextVar rather than threading.local: the formatter and audit writer never
# see the request, and it crosses async/thread boundaries.
_request_id = ContextVar("request_id", default="")


def current_request_id(default=""):
    """The correlation id of the request being served, or ``default``."""
    return _request_id.get() or default


def sanitize_request_id(value):
    """Return ``value`` when safe to propagate, else an empty string; callers
    fall back to a generated id."""
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
    """Stamp the current correlation id on every record.

    Injected via ``LOGGING`` so new call sites cannot forget it; records outside
    a request get :data:`NO_REQUEST_ID`.
    """

    def format(self, record):
        record.request_id = current_request_id(default=NO_REQUEST_ID)
        return super().format(record)


class RequestIDMiddleware:
    """Attach a correlation id to every request and response.

    Registered first so it covers responses produced above the view (redirects,
    preflight, admin login bounce), not only view output.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = (
            sanitize_request_id(request.META.get(REQUEST_ID_META_KEY))
            or new_request_id()
        )
        # On the request for views and in a ContextVar for writers that never
        # see the request.
        request.request_id = request_id
        token = _request_id.set(request_id)
        try:
            try:
                response = self.get_response(request)
            except Exception as exc:
                # The outermost middleware sees an exception before Django
                # converts it; building the response here keeps the 500 path
                # (and its log line) carrying the id too.
                response = response_for_exception(request, exc)
        finally:
            # Reset so the id cannot leak into the next request on this thread.
            _request_id.reset(token)
        response[REQUEST_ID_HEADER] = request_id
        return response

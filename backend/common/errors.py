"""The one JSON error envelope.

A DRF exception handler envelops exception responses at the source, and middleware
rewrites the ad-hoc ``Response({"error": ...})`` returns no handler sees.
"""

import json

from rest_framework.utils.encoders import JSONEncoder
from rest_framework.views import exception_handler as drf_exception_handler

# Status-family labels, not causes: clients can branch without parsing text.
_STATUS_CODE_LABELS = {
    400: "validation_error",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    429: "throttled",
}


def _is_error_payload(body):
    """True when the dict body identifies itself as an error payload."""
    if isinstance(body.get("error"), str) or isinstance(body.get("detail"), str):
        return True
    # Serializer field errors map names to message lists; scalar or plain-dict
    # context (the health probe's checks) is not an error payload.
    return any(isinstance(value, list) for value in body.values())


def _is_envelope(body):
    """True when the body is already the envelope, so neither producer wraps it
    twice."""
    return (
        isinstance(body.get("error"), str)
        and isinstance(body.get("code"), str)
        and isinstance(body.get("details"), dict)
    )


def error_envelope_exception_handler(exc, context):
    """``REST_FRAMEWORK["EXCEPTION_HANDLER"]``: envelop DRF errors before
    render. Only DRF-produced responses are reshaped; others stay 500s."""
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    body = response.data
    if isinstance(body, dict) and _is_error_payload(body):
        response.data = _envelope(body, response.status_code)
    return response


def _envelope(body, status_code):
    """Wrap one recognised error payload; non-message context moves into
    ``details``."""
    if isinstance(body.get("error"), str):
        message = body["error"]
    elif isinstance(body.get("detail"), str):
        message = body["detail"]
    else:
        message = "Validation failed."
    details = {
        key: value for key, value in body.items() if key not in ("error", "detail")
    }
    return {
        "error": message,
        "code": _STATUS_CODE_LABELS.get(
            status_code, "server_error" if status_code >= 500 else "error"
        ),
        "details": details,
    }


def _json_dict_body(response):
    """The response's JSON-object payload, or None when the body is not one."""
    data = getattr(response, "data", None)
    if data is None:
        if "application/json" not in (response.get("Content-Type") or ""):
            return None
        try:
            data = json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            return None
    return data if isinstance(data, dict) else None


class ErrorEnvelopeMiddleware:
    """Rewrite ad-hoc JSON error bodies the handler cannot see; an existing
    envelope passes through untouched."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self._envelop(self.get_response(request))

    def _envelop(self, response):
        if response.status_code < 400:
            return response
        body = _json_dict_body(response)
        if body is None or not _is_error_payload(body) or _is_envelope(body):
            return response
        enveloped = _envelope(body, response.status_code)
        # Django rendered before this chain ran, so the wire is
        # ``response.content``; rewriting .data alone would leave it raw.
        # Compact separators match DRF's JSONRenderer.
        response.content = json.dumps(
            enveloped, cls=JSONEncoder, separators=(",", ":")
        ).encode(response.charset or "utf-8")
        if getattr(response, "data", None) is not None:
            # Keep the DRF payload in step for in-process readers of .data.
            response.data = enveloped
        return response

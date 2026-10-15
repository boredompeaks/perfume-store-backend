"""Uniform error envelope for every JSON error response (SPEC-9-03).

Spec §9.2 (line 3002) closes the account-API contract with "Use appropriate
HTTP status codes, validation errors and consistent error responses" — it
prescribes consistency, not a literal shape, so this module defines THE one
shape and converts every error response to it:

    {"error": <human-readable message>,   # always a string
     "code":  <status-family label>,      # machine-readable, from the map
     "details": {<field errors / other context>}}  # {} when there is none

Two producers, ONE shape: ``_envelope`` below is the only place the shape is
built, and each error response is converted exactly once.

- DRF exceptions (401 authentication, 403 permission, 400 serializer
  validation, 404/405, 429 throttling, ...) are enveloped by
  ``error_envelope_exception_handler``, wired as ``REST_FRAMEWORK[
  "EXCEPTION_HANDLER"]``. The handler runs before the response is rendered,
  so the envelope becomes the bytes on the wire.
- Ad-hoc ``Response({"error": ...})`` view returns raise no exception, so no
  handler sees them; ``ErrorEnvelopeMiddleware`` envelops those. Django's
  base handler renders the response BEFORE the middleware chain runs
  (``django.core.handlers.base``), so the middleware rewrites
  ``response.content`` — the bytes actually emitted — rather than only
  ``response.data``, which would leave the client with the raw body (that
  mismatch is exactly the wire bug this split fixed). The middleware is
  idempotent: a body that is already the envelope passes through untouched,
  so a handler-produced envelope is never double-wrapped.

Recognition is deliberately conservative: a dict body counts as an error
payload only when it carries a string ``error``/``detail`` key (the two
shapes views and DRF emit today) or list values (serializer field errors —
mappings of field name to message list). Plain JSON 4xx/5xx bodies with
scalar or nested-dict context — the health probe's 503 checks body, for
example — pass through byte-identical.
"""

import json

from rest_framework.utils.encoders import JSONEncoder
from rest_framework.views import exception_handler as drf_exception_handler

# Status-family labels, not cause labels: the cause lives in the message,
# the code lets clients branch on the status class without parsing text.
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
    """True when the dict body identifies itself as an error payload (see
    module docstring for the conservative recognition rule)."""
    if isinstance(body.get("error"), str) or isinstance(body.get("detail"), str):
        return True
    # Serializer field errors are mappings of field name to message LIST; a
    # body whose non-message values are all scalars or plain dicts (the
    # health probe's checks block) is not an error payload.
    return any(isinstance(value, list) for value in body.values())


def _is_envelope(body):
    """True when the dict body is already the uniform envelope.

    The handler and the middleware both produce envelopes; each one must
    recognise the other's output and leave it untouched, or a response
    would be wrapped twice (the message ending up inside ``details``)."""
    return (
        isinstance(body.get("error"), str)
        and isinstance(body.get("code"), str)
        and isinstance(body.get("details"), dict)
    )


def error_envelope_exception_handler(exc, context):
    """``REST_FRAMEWORK["EXCEPTION_HANDLER"]``: envelop DRF exception
    responses at the source.

    Runs inside the view's dispatch, BEFORE Django renders the response, so
    the envelope is what the renderer emits and the client's bytes carry it
    — a middleware rewrite after the fact is already too late for the wire.
    Only responses DRF's own handler produced are reshaped; non-API
    exceptions still propagate as 500s. The status-family label comes from
    the shared ``_envelope`` helper, so the shape cannot drift from the
    middleware's."""
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    body = response.data
    if isinstance(body, dict) and _is_error_payload(body):
        response.data = _envelope(body, response.status_code)
    return response


def _envelope(body, status_code):
    """Wrap one recognised error payload in the uniform shape. Everything
    that is not the message itself (field errors, scalar context such as
    ``minimum_order_amount``) moves into ``details``."""
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
    """The response's JSON-object payload, or None when the body is not one
    (HTML error pages, rendered responses without a dict payload)."""
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
    """Rewrite every recognised JSON error body into the uniform envelope.

    Owns only what the exception handler cannot see: the ad-hoc
    ``Response({"error": ...})`` returns that raise no exception. For every
    other body it is a no-op — in particular it recognises an envelope the
    handler already produced and leaves it byte-identical."""

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
        # Django's base handler rendered the response before this chain ran,
        # so the client's bytes are response.content: rewriting .data alone
        # would leave the wire with the raw body. Compact separators match
        # DRF's JSONRenderer so the wire shape is uniform across both
        # producers (handler-rendered and middleware-rewritten).
        response.content = json.dumps(
            enveloped, cls=JSONEncoder, separators=(",", ":")
        ).encode(response.charset or "utf-8")
        if getattr(response, "data", None) is not None:
            # Keep the DRF payload in step with the wire for in-process
            # readers of .data; the bytes above remain the client's truth.
            response.data = enveloped
        return response

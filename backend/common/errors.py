"""Uniform error envelope for every JSON error response (SPEC-9-03).

Spec §9.2 (line 3002) closes the account-API contract with "Use appropriate
HTTP status codes, validation errors and consistent error responses" — it
prescribes consistency, not a literal shape, so this module defines THE one
shape and converts every error response to it in a single place:

    {"error": <human-readable message>,   # always a string
     "code":  <status-family label>,      # machine-readable, from the map
     "details": {<field errors / other context>}}  # {} when there is none

Conversion is middleware-level rather than a ``REST_FRAMEWORK``
``EXCEPTION_HANDLER`` because most error bodies here are ad-hoc
``Response({"error": ...})`` returns that raise no exception — a handler
would never see them. Middleware sees every response (DRF responses are
still unrendered here, so rewriting ``response.data`` rewrites the body).

Recognition is deliberately conservative: a dict body counts as an error
payload only when it carries a string ``error``/``detail`` key (the two
shapes views and DRF emit today) or list values (serializer field errors —
mappings of field name to message list). Plain JSON 4xx/5xx bodies with
scalar or nested-dict context — the health probe's 503 checks body, for
example — pass through byte-identical.
"""
import json

from rest_framework.utils.encoders import JSONEncoder

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
    """Rewrite every recognised JSON error body into the uniform envelope."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self._envelop(self.get_response(request))

    def _envelop(self, response):
        if response.status_code < 400:
            return response
        body = _json_dict_body(response)
        if body is None or not _is_error_payload(body):
            return response
        enveloped = _envelope(body, response.status_code)
        if getattr(response, "data", None) is not None:
            # DRF Response: still unrendered, so reassigning the payload
            # rewrites what the renderer will emit.
            response.data = enveloped
        else:
            response.content = json.dumps(
                enveloped, cls=JSONEncoder
            ).encode(response.charset or "utf-8")
        return response

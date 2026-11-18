"""Privileged-action audit trail and the single sanitizing path into it.

API writes bypass the admin's ``LogEntry``, and structured values must never carry
credential material, so every detail passes through :func:`clean_audit_payload`.
"""

from django.contrib.admin.models import LogEntry

# Credential-bearing field names, normalized (lowercase alphanumeric) for the
# substring match in ``is_excluded_field``; the only guard between a model field
# and the trail.
EXCLUDED_AUDIT_FIELDS = frozenset(
    {
        "password",
        "password1",
        "password2",
        "passwordconfirmation",
        "newpassword",
        "oldpassword",
        "otp",
        "totp",
        "totpsecret",
        "mfasecret",
        "otpsecret",
        "token",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "idtoken",
        "csrftoken",
        "resettoken",
        "verificationtoken",
        "secret",
        "keysecret",
        "apikey",
        "apisecret",
        "clientsecret",
        "secretkey",
        "webhooksecret",
        "signature",
        "hmackey",
        "card",
        "cardnumber",
        "cvv",
        "cvc",
        "iban",
        "accountnumber",
    }
)

# A detail records what changed, not a copy of the row; over-long values are
# truncated with a marked cut.
AUDIT_VALUE_MAX_LENGTH = 200


def is_excluded_field(name):
    """True when a field name says it holds credential material (normalized
    substring match, so ``razorpay_order_id`` is not caught)."""
    normalized = "".join(char for char in str(name).lower() if char.isalnum())
    return any(marker in normalized for marker in EXCLUDED_AUDIT_FIELDS)


def clean_audit_value(value):
    """One audit value: JSON-safe, bounded, and exact for money (Decimals become
    strings, never floats)."""
    if value is None or isinstance(value, (bool, int)):
        return value
    text = str(value)
    if len(text) > AUDIT_VALUE_MAX_LENGTH:
        return text[:AUDIT_VALUE_MAX_LENGTH] + "…"
    return text


def clean_audit_payload(payload):
    """A detail mapping with credential-bearing fields removed.

    Applied by ``AuditEvent.record`` itself, so it gates the whole trail; a
    non-mapping yields ``{}``.
    """
    if not isinstance(payload, dict):
        return {}
    return {
        key: _clean_audit_nested(value)
        for key, value in payload.items()
        if not is_excluded_field(key)
    }


def _clean_audit_nested(value):
    """Recurse through mappings/lists, then clean the leaf value."""
    if isinstance(value, dict):
        return clean_audit_payload(value)
    if isinstance(value, (list, tuple)):
        return [_clean_audit_nested(item) for item in value]
    return clean_audit_value(value)


def field_changes(changes):
    """Turn ``(field, before, after)`` triples into the stored shape.

    An excluded field lands no entry at all, rather than a redaction that still
    discloses that it moved.
    """
    return [
        clean_audit_payload({"field": name, "before": before, "after": after})
        for name, before, after in changes
        if not is_excluded_field(name)
    ]


def model_field_changes(obj, field_names, before=None):
    """The triples for the fields that moved, read off ``obj`` after the write;
    callers snapshot first, save, then log."""
    snapshot = before or {}
    changes = []
    for name in field_names:
        new = getattr(obj, name, None)
        # Absent from the snapshot means not part of the write (a creation);
        # present means record only if it moved.
        if name not in snapshot:
            changes.append((name, None, new))
        elif snapshot[name] != new:
            changes.append((name, snapshot[name], new))
    return changes


def log_mutation(request, obj, event_type, action, source, changes=()):
    """Record one guarded mutation with its structured before -> after values.

    ``source`` is required so no call site can mislabel the surface. Call inside
    the same transaction as the write, so trail and effect commit together.
    """
    # Import here, not at module scope: common.models imports this module's
    # sanitizer, so a top-level import would cycle.
    from common.models import AuditEvent

    detail = {
        "action": action,
        "model": obj._meta.label_lower,
        "object_id": str(obj.pk),
        "object_repr": clean_audit_value(obj),
    }
    structured = field_changes(changes)
    if structured:
        detail["changes"] = structured
    return AuditEvent.record(
        event_type,
        actor=request.user,
        detail=detail,
        source=source,
    )


def log_api_action(request, obj, action_flag, change_message):
    """Record one privileged staff write performed through the API.

    For a deletion, call before the delete: the collector clears the instance pk
    afterwards. ``request.user`` is always authenticated staff here.
    """
    return LogEntry.objects.log_actions(
        request.user.pk,
        [obj],
        action_flag,
        change_message=change_message,
        single_object=True,
    )

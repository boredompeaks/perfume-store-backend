"""Privileged-action audit trail + the one sanitizing path into it.

Spec 6.12, [6.12.6] "Log privileged actions": the Django admin already
records every change-form save/delete (and ``RoleAwareModelAdmin`` covers
queryset bulk actions) as a ``django.contrib.admin.models.LogEntry`` — but
writes made through the DRF API bypass the admin entirely and were
unlogged. ``log_api_action`` emits the same record type, so the audit-log
route reads one source for both surfaces. The ledger's SPEC-6-05 split
decision keeps the business-event audit model with SPEC-7-01; this is the
privileged-action trail, not an event store.

SPEC-20-4 [R-20.27]/[R-20.29] adds two things on top of that trail, and both
belong here rather than at the call sites:

* **Structured before -> after values.** A change message that only says
  "Updated via API." cannot answer what the row looked like afterwards.
  :func:`log_mutation` records the real old/new values per field on an
  ``AuditEvent``, which — unlike ``LogEntry`` — is this project's own model
  and can therefore carry a queryable ``source`` field.
* **A source discriminator.** ``admin`` vs ``api`` is a field on the record,
  not prose inside a message. Every caller states its surface explicitly;
  the parameter has no default so a new call site cannot silently
  mislabel itself.

The security contract is the part that must not drift. Structured values
mean real model data now flows into the trail, so **every** value reaching
an audit detail — from this module's helpers and from the plain
``AuditEvent.record`` calls made by the business-event surfaces alike —
passes through :func:`clean_audit_payload`, which drops any field whose name
says it holds credential material (:data:`EXCLUDED_AUDIT_FIELDS`) and bounds
what is left. There is deliberately no way to write a raw detail: the
sanitizing path is the only path.
"""

from django.contrib.admin.models import LogEntry

# Field names that must NEVER reach an audit detail, in comparison-normalized
# form (lowercase, alphanumeric only) — see ``is_excluded_field`` for why the
# match is a normalized substring test.
#
# This is a denylist of credential-bearing names, and it is the only thing
# standing between a model field and the audit trail once structured values
# exist. It covers the real material in this project (Django's password
# fields, the MFA secret/one-time codes, DRF/JWT tokens, gateway key material
# and card data) plus the conventional names, because a future model field is
# far more likely to reuse a conventional name than to invent a new one.
EXCLUDED_AUDIT_FIELDS = frozenset(
    {
        # Passwords and passphrases (Django + DRF serializer spellings).
        "password",
        "password1",
        "password2",
        "passwordconfirmation",
        "newpassword",
        "oldpassword",
        # One-time codes and the MFA seed they are derived from.
        "otp",
        "totp",
        "totpsecret",
        "mfasecret",
        "otpsecret",
        # Session / reset / verification material.
        "token",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "idtoken",
        "csrftoken",
        "resettoken",
        "verificationtoken",
        # Provider credentials (Razorpay keys, webhook secrets, HMAC keys).
        "secret",
        "keysecret",
        "apikey",
        "apisecret",
        "clientsecret",
        "secretkey",
        "webhooksecret",
        "signature",
        "hmackey",
        # Payment card data.
        "card",
        "cardnumber",
        "cvv",
        "cvc",
        "iban",
        "accountnumber",
    }
)

# An audit detail records what changed, not a copy of the row: a free-text
# value longer than this is prose (a pasted description, a note) and would
# bloat the trail and every log line that renders it. Truncation is marked so
# a reader can see the value was cut.
AUDIT_VALUE_MAX_LENGTH = 200


def is_excluded_field(name):
    """True when a field name says it holds credential material.

    Matched as a normalized substring, so ``TOTPDevice.secret``,
    ``razorpay_key_secret`` and ``apiKey`` are all caught while
    ``razorpay_order_id`` — an order reference, pinned by the SPEC-7-01
    payment pins — is not.
    """
    normalized = "".join(char for char in str(name).lower() if char.isalnum())
    return any(marker in normalized for marker in EXCLUDED_AUDIT_FIELDS)


def clean_audit_value(value):
    """One audit value: JSON-safe, bounded, exact for money.

    Decimals become their string form (never a float — conventions.md:15),
    ints and bools pass through, and anything else is rendered and truncated.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    text = str(value)
    if len(text) > AUDIT_VALUE_MAX_LENGTH:
        return text[:AUDIT_VALUE_MAX_LENGTH] + "…"
    return text


def clean_audit_payload(payload):
    """A detail mapping with every credential-bearing field removed.

    Applied by ``AuditEvent.record`` itself, so this is the single gate for
    the whole trail rather than a rule each call site has to remember.
    Non-mapping input yields ``{}``: a detail is always a mapping.
    """
    if not isinstance(payload, dict):
        return {}
    return {
        key: _clean_audit_nested(value)
        for key, value in payload.items()
        if not is_excluded_field(key)
    }


def _clean_audit_nested(value):
    """Recurse through nested mappings/lists, then clean the leaf value."""
    if isinstance(value, dict):
        return clean_audit_payload(value)
    if isinstance(value, (list, tuple)):
        return [_clean_audit_nested(item) for item in value]
    return clean_audit_value(value)


def field_changes(changes):
    """Turn ``(field, before, after)`` triples into the stored shape.

    The change set itself is filtered by field name: a mutation of an
    excluded field lands no ``before``/``after`` at all, rather than a
    redacted value that still discloses that the field moved.
    """
    return [
        clean_audit_payload({"field": name, "before": before, "after": after})
        for name, before, after in changes
        if not is_excluded_field(name)
    ]


def model_field_changes(obj, field_names, before=None):
    """The ``(field, before, after)`` triples for the fields that moved.

    ``before`` is the pre-mutation snapshot mapping; omitted (a creation),
    every recorded ``before`` is ``None``. Values are read off ``obj`` AFTER
    the write, so the caller snapshots first, saves, then logs.
    """
    snapshot = before or {}
    changes = []
    for name in field_names:
        new = getattr(obj, name, None)
        # A field absent from the snapshot was not part of the write (a
        # creation, where the recorded ``before`` is None); one present in
        # it is recorded only when its value actually moved.
        if name not in snapshot:
            changes.append((name, None, new))
        elif snapshot[name] != new:
            changes.append((name, snapshot[name], new))
    return changes


def log_mutation(request, obj, event_type, action, source, changes=()):
    """Record one guarded mutation with its structured before -> after values.

    The single writer for structured mutation audit: the actor comes from the
    request, the target is identified by label/pk/repr (so the row outlives a
    deletion), and the values are stored through the sanitizing path above.

    ``source`` is required, not defaulted: an audit row that cannot say
    whether staff did this in the admin or through the API ([R-20.29]) is
    worse than no row, and a required argument is what stops the next call
    site from guessing. Call inside the same transaction as the write, so
    trail and effect commit or roll back together.
    """
    # Imported here, not at module scope: common.models imports the sanitizer
    # above from this module, so a top-level import back would be a cycle.
    # By the time a mutation is logged the app registry is loaded.
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

    A LogEntry never foreign-keys the object — it stores the content type,
    primary key and last-known repr, so the record outlives the row. For
    DELETION, call this BEFORE the delete: Django's collector clears the
    instance pk afterwards (the admin's own ``log_deletion`` logs first
    for the same reason). Returns the LogEntry for callers that want to
    assert on it.

    Only capability-gated views call this, so ``request.user`` is always an
    authenticated staff member; the admin's own log methods make the same
    flow trust assumption.
    """
    return LogEntry.objects.log_actions(
        request.user.pk,
        [obj],
        action_flag,
        change_message=change_message,
        single_object=True,
    )

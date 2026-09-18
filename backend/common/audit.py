"""Privileged-action audit trail for API-side staff operations.

Spec 6.12, [6.12.6] "Log privileged actions": the Django admin already
records every change-form save/delete (and ``RoleAwareModelAdmin`` covers
queryset bulk actions) as a ``django.contrib.admin.models.LogEntry`` — but
writes made through the DRF API bypass the admin entirely and were
unlogged. This helper emits the same record type, so the audit-log route
reads one source for both surfaces. The ledger's SPEC-6-05 split decision
keeps the business-event audit model with SPEC-7-01; this is the
privileged-action trail, not an event store.
"""

from django.contrib.admin.models import LogEntry


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

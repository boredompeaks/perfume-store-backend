"""Role-aware Django admin base (spec 6.12, permission enforcement).

The Django admin surface previously relied on Django's own model
permissions, which none of the six staff roles carry — in practice only
the superuser could work the admin. This base routes the four model
permissions (view/add/change/delete) through ``CAPABILITY_ROLES`` instead,
so each staff role sees exactly the models its function needs
(least privilege, [6.12.7]) and the ``admin`` role — which the roles map
grants every capability — keeps full function.

Two conventions guardrails are honoured deliberately:

- Authorization data comes solely from the roles map; the only user-flag
  shortcut here is Django's own superuser bypass, which is preserved so
  the trust anchor keeps working exactly as before.
- Bulk/export actions are gated through ``get_actions`` (the same seam
  Django uses for ``allowed_permissions``), so a gated action disappears
  from the dropdown *and* is rejected if POSTed directly.

A surface with a second door (``scoped_view_capability`` — spec 1.1 line
110's packing operator reaches the orders grid without ``orders.read``)
narrows READING and WRITING from two deny-by-default declarations:
``scoped_writable_fields`` decides which fields such a caller may edit at
all (every other field its fieldsets expose renders read-only), and
``scoped_value_capabilities`` decides which *values* of those fields need a
further capability. The second one is enforced at the input, in
``formfield_for_dbfield``, which both admin write paths (the change form
and the ``list_editable`` formset) are built through — so a value the
caller may not write is neither offered nor accepted.

Destructive bulk actions ([6.12.4] — explicit confirmation for sensitive
actions) declare themselves in ``confirmation_required_actions``; the base
interposes a confirmation interstitial between the dropdown POST and the
action body. Actions that already render their own deliberate input form
(e.g. adjust-stock) are their own confirmation and stay off the list.

SPEC-20-1 [R-20.20] enriches that one interstitial rather than adding a
second mechanism: a financially significant action supplies a *structured*
detail payload (``confirmation_details`` — amount, currency, affected items,
resulting state) and the shared template renders it above the object list.
SPEC-20-2 [R-20.18] then reuses the very same page as the confirm step for a
mutation that is not a bulk action (a staff-role change on the user change
form): the interstitial re-drives the submission it interrupted, so there is
one confirmation contract, one template and one commit gate. SPEC-20-5
[R-20.28] finally lets an action ask WHY on that page — an optional
reason/note, declared per action (``confirmation_reason_actions``) and read
back off the same POST by the action body.
"""

from dataclasses import replace
from functools import wraps

from django import forms
from django.contrib import admin
from django.contrib.admin.options import ActionLocation
from django.shortcuts import render
from django.urls import reverse

from common.permissions import user_has_capability

# The POST contract of the interstitial. Module constants, not inline
# literals, because four places have to agree on them: the render path (emits
# them), the commit gate (refuses to act without the confirm), the note
# capture, and the re-submission path for a non-bulk confirmation.
CONFIRM_FIELD = "confirm"
CONFIRMATION_YES = "yes"
# Namespaced field name, not a bare "note": the interstitial also re-drives
# other surfaces' POSTs (see ``_repost_fields``), and a short generic key
# could collide with a field those forms own.
CONFIRMATION_NOTE_FIELD = "confirmation_note"
CONFIRMATION_NOTE_MAX_LENGTH = 500

# The prompt the delivered bulk-action interstitial has always shown, kept
# verbatim as the default so rendering one stays byte-identical to before.
DEFAULT_CONFIRM_WARNING = (
    "is a sensitive action and cannot be undone. Confirm to continue."
)


class ConfirmationNoteForm(forms.Form):
    """The optional reason/note a confirmation may ask for (SPEC-20-5).

    Same shape as the inventory path's capture (products.admin
    .AdjustStockForm's optional note): one free-text field, recorded with the
    mutation. Orders carry no reason *vocabulary* — there is no ledger enum
    like ``StockMovement.Reason`` — so free text is the whole capture; minting
    an enum here would mean a model change and a migration for an audit
    string, which the spec does not ask for.
    """

    confirmation_note = forms.CharField(
        label="Reason / note",
        required=False,
        max_length=CONFIRMATION_NOTE_MAX_LENGTH,
        help_text=(
            "Optional. Recorded verbatim in the admin audit trail beside the action."
        ),
        widget=forms.Textarea(attrs={"rows": 2}),
    )


def _repost_fields(post):
    """Flatten the interrupted POST into ``(name, value)`` hidden pairs.

    A non-bulk confirmation must re-drive the *same* submission, so every
    field the user already filled travels through the interstitial and the
    confirmed POST re-runs the ordinary, fully validated save — the
    interstitial adds a step, never a second, looser write path. Multi-valued
    fields contribute one pair per value. The CSRF token (the template
    renders a fresh one) and the confirm marker (the template owns it) are
    dropped, so an interrupted POST can neither forge a token nor pin its own
    outcome. ``request.FILES`` is deliberately not flattened: no file field
    rides this seam, and re-sending bytes through hidden inputs is not a thing
    to invent for it.
    """
    return tuple(
        (name, value)
        for name, values in post.lists()
        if name != CONFIRM_FIELD and not name.startswith("csrf")
        for value in values
    )


class RoleAwareModelAdmin(admin.ModelAdmin):
    """ModelAdmin whose permissions flow from ``CAPABILITY_ROLES``.

    Subclasses declare:

    - ``capability_map``: permission kind -> capability identifier. A kind
      mapped to ``None`` (or absent) is not grantable to any staff role —
      only the superuser bypass can perform it (e.g. orders are created by
      checkout, never from the admin).
    - ``action_capabilities``: bulk action name -> capability identifier.
      Every entry in ``ModelAdmin.actions`` must be mapped here; the test
      suite pins that invariant so a new action cannot ship ungated.
    - ``confirmation_required_actions``: bulk action names that must not
      execute until the user explicitly confirms on the interstitial page.
    - ``confirmation_reason_actions``: names (from the set above) whose
      interstitial also asks for an optional reason/note.
    - ``scoped_view_capability``: an OPTIONAL second door onto the same
      surface for a role the map denies full visibility to (spec 1.1 line
      110's packing/shipping operator holds ``orders.fulfill`` but not
      ``orders.read``). Such a caller still reaches the changelist — it
      already holds the model's ``change`` capability, which is what gates
      the grid — but the ModelAdmin is expected to narrow what that grid
      shows (queryset, columns, searchable fields, change-form fieldsets)
      AND what it may write (``scoped_writable_fields`` /
      ``scoped_value_capabilities``). ``None`` (the default, and every admin
      but the one that declares it) means there is no second door at all.
    """

    capability_map = {}
    action_capabilities = {}
    confirmation_required_actions = frozenset()
    confirmation_reason_actions = frozenset()
    scoped_view_capability = None

    # ——— capability plumbing ———

    def _holds_capability(self, request, capability):
        """Superuser bypass first (Django's own, preserved verbatim),
        then the roles map; unknown capability ids deny by default."""
        user = request.user
        if user.is_superuser:
            return True
        return user_has_capability(user, capability)

    def _map_grants(self, request, kind):
        if request.user.is_superuser:
            return True
        capability = self.capability_map.get(kind)
        return capability is not None and user_has_capability(request.user, capability)

    # ——— the four model permissions, driven by the map ———

    def has_view_permission(self, request, obj=None):
        return self._map_grants(request, "view")

    def has_add_permission(self, request):
        return self._map_grants(request, "add")

    def has_change_permission(self, request, obj=None):
        return self._map_grants(request, "change")

    def has_delete_permission(self, request, obj=None):
        return self._map_grants(request, "delete")

    def is_scoped_viewer(self, request):
        """Whether this caller reaches the surface through
        ``scoped_view_capability`` rather than through ``capability_map``.

        True only for a caller that holds the scoped capability and NOT the
        model's own view capability — a superuser, and any role holding both,
        answer ``False`` and therefore keep the ordinary full surface. The
        check is deny-by-default: no ``scoped_view_capability`` declared means
        no second door, whatever the caller holds.
        """
        capability = self.scoped_view_capability
        if capability is None or not self._holds_capability(request, capability):
            return False
        return not self._map_grants(request, "view")

    def has_module_permission(self, request):
        # The default checks Django model permissions (which no role
        # carries), which would hide every model from the admin index;
        # module visibility must follow the same map as the model itself.
        # A scoped viewer is the one addition: the packing operator holds the
        # model's change capability, so its grid already answers 200, and
        # leaving the module off the index would make that reachable-but-
        # undiscoverable surface (the operator would have to guess the URL).
        return self.has_view_permission(request) or self.is_scoped_viewer(request)

    # ——— the scoped WRITE seam (what a scoped viewer may commit) ———
    #
    # Reading is half the second door; writing is the half that needs its own
    # declarations, because a scoped viewer's change capability is real (it is
    # what Django gates the grid on) while its mandate is one function. Both
    # attributes are deny-by-default: the base declares NOTHING writable and
    # NO gated value, so an admin that opens the door without declaring its
    # writes offers a read, not an editor, and a future capability is never
    # writable until someone maps it.

    # Field names this surface lets a scoped viewer edit. Every other field its
    # fieldsets expose renders read-only (see ``get_readonly_fields``).
    scoped_writable_fields = frozenset()
    # ``field name -> {value: capability required to write that value}``. A
    # value named here belongs to another capability's authority, so it is
    # offered and accepted only for a caller holding that capability; an
    # unmapped value carries no extra requirement beyond the field's own.
    scoped_value_capabilities = {}

    def scoped_value_permitted(self, request, field_name, value):
        """Whether ``value`` may be written into ``field_name`` by this caller.

        The single point the answer is computed at, read from both ends of the
        seam: ``formfield_for_dbfield`` withholds a refused value from the
        widget, and it is the same predicate a ModelAdmin would ask before
        writing a value itself. ``str()`` because a ModelForm hands back a
        string for a char/choice field while a dict key is written literally.
        """
        required = self.scoped_value_capabilities.get(field_name, {}).get(str(value))
        return required is None or self._holds_capability(request, required)

    def get_readonly_fields(self, request, obj=None):
        """A scoped viewer edits only what its ModelAdmin declared writable.

        Deny-by-default over the model: every editable field it carries except
        the declared writes renders read-only, and Django's ``get_form``
        EXCLUDES readonly fields from the ModelForm — so a hand-posted value
        for one is never a form field and is ignored outright rather than
        validated away. That is why widening a scoped fieldset cannot silently
        hand out a write: the declaration, not the fieldset, decides what is
        editable.

        The field list is read from the MODEL, never from ``get_fieldsets()``:
        the default ``get_fieldsets`` reaches back into ``get_form`` and
        ``get_readonly_fields``, so consulting it here would recurse on any
        surface that does not declare its own fieldsets.
        """
        readonly = super().get_readonly_fields(request, obj)
        if not self.is_scoped_viewer(request):
            return readonly
        withheld = (
            field.name
            for field in self.model._meta.get_fields()
            if getattr(field, "editable", False)
            and field.name not in self.scoped_writable_fields
            and field.name not in readonly
        )
        return readonly + tuple(withheld)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        """A value this caller may not write is never OFFERED to it.

        Both admin write paths are built through this callback — ``get_form``
        for the change form and ``get_changelist_form`` for the ``list_editable``
        formset both pass it as ``formfield_callback`` — so narrowing the
        choices here closes them at once, at the layer that owns the input: the
        option is gone from the widget, and a value posted by hand fails the
        field's own validation before ``save_model`` is ever reached. Only a
        field with choices may be named in ``scoped_value_capabilities``; any
        other formfield is returned untouched.
        """
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        gated = (
            self.scoped_value_capabilities.get(db_field.name)
            if self.is_scoped_viewer(request)
            else None
        )
        if not gated:
            return formfield
        formfield.choices = [
            choice
            for choice in formfield.choices
            if self.scoped_value_permitted(request, db_field.name, choice[0])
        ]
        return formfield

    # ——— privileged-action audit trail ([6.12.5]) ———

    def log_bulk_action(self, request, queryset, message):
        # Change-form saves and deletes leave a LogEntry automatically, but
        # queryset bulk actions bypass save_model entirely — without this,
        # the privileged paths gated above would mutate rows unlogged.
        for obj in queryset:
            self.log_change(request, obj, message)

    # ——— bulk / export action gating + confirmation ———

    def get_actions(self, request, action_location=ActionLocation.CHANGE_LIST):
        # Same seam Django uses for ``allowed_permissions``: filtering here
        # hides the action from the dropdown and makes a directly POSTed
        # action name an invalid choice, so the gate holds on both paths.
        actions = super().get_actions(request, action_location)
        gated = {}
        for name, action in actions.items():
            capability = self.action_capabilities.get(name)
            if capability is not None and not self._holds_capability(
                request, capability
            ):
                continue
            if name in self.confirmation_required_actions:
                action = replace(action, func=self._confirm_first(action))
            gated[name] = action
        return gated

    def _confirm_first(self, action):
        """Wrap an unbound action so the first POST renders the
        confirmation interstitial instead of executing."""
        func = action.func

        @wraps(func)
        def confirmed_first(admin_self, request, queryset):
            # The superuser keeps the legacy direct execution: the bypass
            # is Django's own trust anchor and predates RBAC.
            if (
                request.user.is_superuser
                or request.POST.get(CONFIRM_FIELD) == CONFIRMATION_YES
            ):
                return func(admin_self, request, queryset)
            return admin_self.render_action_confirmation(request, action, queryset)

        return confirmed_first

    # ——— the interstitial's structured detail payload (SPEC-20-1) ———

    def confirmation_details(self, request, action_name, objects):
        """The structured payload the interstitial renders above the objects.

        Empty by default: an action with nothing consequential to spell out
        keeps the delivered description/count/object-list render untouched.
        A financially significant action overrides this and supplies the
        amount and its currency, the affected items and the resulting state
        per selected row (SPEC-20-1 [R-20.20]).
        """
        return {}

    def confirmation_reason_form(self, action_name, data=None):
        """The reason/note form this action's interstitial offers, or None.

        None is the gate: a reason is asked for only where the admin
        declared one, so the textarea cannot appear on a confirmation with no
        audit slot to put it in.
        """
        if action_name not in self.confirmation_reason_actions:
            return None
        if data is None:
            return ConfirmationNoteForm()
        return ConfirmationNoteForm(data)

    def confirmation_note(self, request, action_name):
        """The validated optional reason captured on the interstitial.

        Empty when the action asks for no reason, when none was typed, or
        when the submitted value fails the form (an over-long crafted note).
        The mutation is never blocked by an optional field, and a note is
        never truncated into the audit trail: dropping it lands on exactly the
        same outcome as supplying none.
        """
        form = self.confirmation_reason_form(action_name, request.POST)
        if form is None or not form.is_valid():
            return ""
        return form.cleaned_data[CONFIRMATION_NOTE_FIELD].strip()

    def render_confirmation(
        self,
        request,
        *,
        action_name,
        description,
        objects,
        details=None,
        warning=DEFAULT_CONFIRM_WARNING,
        re_post=False,
        cancel_url=None,
    ):
        """Render the interstitial — the single confirmation surface.

        Bulk actions and non-bulk mutations (the staff-role change step)
        both come through here, so there is one template, one confirm
        contract and one place the structured detail payload is assembled.
        """
        return render(
            request,
            "admin/action_confirmation.html",
            {
                "title": description,
                "description": description,
                "warning": warning,
                "action_name": action_name,
                "objects": objects,
                "select_across": request.POST.get("select_across") == "1",
                "opts": self.model._meta,
                "details": (
                    details
                    if details is not None
                    else self.confirmation_details(request, action_name, objects)
                ),
                "pending_fields": _repost_fields(request.POST) if re_post else (),
                "note_form": self.confirmation_reason_form(action_name),
                "cancel_url": cancel_url or reverse("admin:index"),
            },
        )

    def render_action_confirmation(self, request, action, queryset):
        return self.render_confirmation(
            request,
            action_name=action.name,
            description=action.description,
            objects=queryset,
        )

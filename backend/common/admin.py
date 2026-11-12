"""Role-aware Django admin base.

Model permissions and bulk actions flow from ``CAPABILITY_ROLES``, and destructive
actions pass through one shared confirmation interstitial.
"""

from dataclasses import replace
from functools import wraps
from typing import Any

from django import forms
from django.contrib import admin
from django.contrib.admin.options import ActionLocation
from django.shortcuts import render
from django.urls import reverse

from common.permissions import user_has_capability

# The interstitial's POST contract, shared by its render, commit and note paths.
CONFIRM_FIELD = "confirm"
CONFIRMATION_YES = "yes"
# Namespaced so it cannot collide with a re-driven form's own field names.
CONFIRMATION_NOTE_FIELD = "confirmation_note"
CONFIRMATION_NOTE_MAX_LENGTH = 500

# Default warning, kept verbatim for the delivered interstitial.
DEFAULT_CONFIRM_WARNING = (
    "is a sensitive action and cannot be undone. Confirm to continue."
)


class ConfirmationNoteForm(forms.Form):
    """Optional free-text reason asked on a confirmation page."""

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

    A non-bulk confirmation re-drives the same validated save, never a looser second
    write path. CSRF and the confirm marker are dropped so it cannot forge either.
    """
    return tuple(
        (name, value)
        for name, values in post.lists()
        if name != CONFIRM_FIELD and not name.startswith("csrf")
        for value in values
    )


class RoleAwareModelAdmin(admin.ModelAdmin):
    """ModelAdmin whose permissions flow from ``CAPABILITY_ROLES``.

    Subclasses declare ``capability_map`` (a kind mapped to ``None`` is not
    grantable), ``action_capabilities``, the confirmation sets, and an optional
    scoped second door (``scoped_view_capability``).
    """

    # Annotated because subclasses override them and mypy cannot infer bare
    # ``{}``/``frozenset()`` defaults.
    capability_map: dict[str, frozenset[str]] = {}
    action_capabilities: dict[str, frozenset[str]] = {}
    confirmation_required_actions: frozenset[str] = frozenset()
    confirmation_reason_actions: frozenset[str] = frozenset()
    scoped_view_capability: str | None = None

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

    def has_view_permission(self, request, obj=None):
        return self._map_grants(request, "view")

    def has_add_permission(self, request):
        return self._map_grants(request, "add")

    def has_change_permission(self, request, obj=None):
        return self._map_grants(request, "change")

    def has_delete_permission(self, request, obj=None):
        return self._map_grants(request, "delete")

    def is_scoped_viewer(self, request):
        """Whether the caller reaches the surface only through the scoped
        second door; deny-by-default when none is declared."""
        capability = self.scoped_view_capability
        if capability is None or not self._holds_capability(request, capability):
            return False
        return not self._map_grants(request, "view")

    def has_module_permission(self, request):
        # Module visibility must follow the same map: the default checks Django
        # permissions no role carries. Scoped viewers are included so their
        # reachable grid stays discoverable.
        return self.has_view_permission(request) or self.is_scoped_viewer(request)

    # The scoped write seam, deny-by-default: an admin that opens the second
    # door without declaring its writes offers a read, not an editor.

    # Fields a scoped viewer may edit; every other editable model field renders
    # read-only.
    scoped_writable_fields: frozenset[str] = frozenset()
    # ``field -> {value: capability required to write it}``; an unmapped value
    # carries no extra requirement. ``Any`` keys because a ModelForm hands back
    # strings while dict keys are written literally.
    scoped_value_capabilities: dict[str, dict[Any, str]] = {}

    def scoped_value_permitted(self, request, field_name, value):
        """Whether this caller may write ``value`` into ``field_name``.

        The one predicate behind both the widget filter and a direct write check;
        ``str()`` normalizes a ModelForm's returned value.
        """
        required = self.scoped_value_capabilities.get(field_name, {}).get(str(value))
        return required is None or self._holds_capability(request, required)

    def get_readonly_fields(self, request, obj=None):
        """A scoped viewer edits only what ``scoped_writable_fields`` declares.

        Readonly fields are excluded from the ModelForm, so a hand-posted value is
        ignored. The field list is read from the model because ``get_fieldsets``
        would recurse back here.
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
        """Withhold from a scoped viewer any value its capability does not permit.

        Both admin write paths use this callback, so a refused option is gone from
        the widget and a hand-posted value fails the field's validation.
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

    def log_bulk_action(self, request, queryset, message):
        # Bulk actions bypass save_model, so log each row to keep the privileged
        # paths audited.
        for obj in queryset:
            self.log_change(request, obj, message)

    def get_actions(self, request, action_location=ActionLocation.CHANGE_LIST):
        # Filtering here also makes a directly POSTed action name invalid, so the
        # gate holds on both paths.
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
        """Wrap an unbound action so the first POST shows the interstitial."""
        func = action.func

        @wraps(func)
        def confirmed_first(admin_self, request, queryset):
            # The superuser bypass predates RBAC and is preserved.
            if (
                request.user.is_superuser
                or request.POST.get(CONFIRM_FIELD) == CONFIRMATION_YES
            ):
                return func(admin_self, request, queryset)
            return admin_self.render_action_confirmation(request, action, queryset)

        return confirmed_first

    def confirmation_details(self, request, action_name, objects):
        """Structured payload rendered above the objects; empty unless a
        consequential action overrides it."""
        return {}

    def confirmation_reason_form(self, action_name, data=None):
        """The reason/note form this action's interstitial offers, or ``None``."""
        if action_name not in self.confirmation_reason_actions:
            return None
        if data is None:
            return ConfirmationNoteForm()
        return ConfirmationNoteForm(data)

    def confirmation_note(self, request, action_name):
        """The validated optional note, or ``""`` when none was asked, typed or
        valid; an optional field never blocks the mutation."""
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
        """Render the interstitial shared by bulk and non-bulk confirmations."""
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

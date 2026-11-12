"""Per-user saved changelist filters.

A saved filter is a shortcut to a URL, never a second path to rows: the marker is
folded into ``request.GET`` and Django's changelist does the filtering.
"""

from django import forms
from django.apps import apps
from django.contrib import admin, messages
from django.contrib.admin.exceptions import (
    DisallowedModelAdminLookup,
    DisallowedModelAdminToField,
)
from django.contrib.admin.options import IncorrectLookupParameters
from django.contrib.admin.views.main import (
    ERROR_FLAG,
    IGNORED_PARAMS,
    PAGE_VAR,
    SEARCH_VAR,
)
from django.contrib.auth.views import redirect_to_login
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import SuspiciousOperation
from django.db import IntegrityError, transaction
from django.http import Http404, QueryDict
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from common.models import SavedFilter

# Shared spellings: ``APPLY_PARAM`` is the only query parameter this feature
# owns; the selection travels in the POST body as the form field ``SPEC_FIELD``.
APPLY_PARAM = "_saved_filter"
SPEC_FIELD = "filter_spec"
# Bounds the changelist-authority probes a crafted POST can ask for.
SPEC_FIELD_MAX_LENGTH = 2000
# Changelist plumbing, not a filter selection, so it is never stored; ``q`` is
# the exception. Names are imported from Django so the set cannot drift.
NON_SPEC_PARAMS = (frozenset(IGNORED_PARAMS) - {SEARCH_VAR}) | {
    PAGE_VAR,
    ERROR_FLAG,
    APPLY_PARAM,
    SPEC_FIELD,
}


class SavedFilterForm(forms.Form):
    """The save form: a name plus the current selection.

    ``filter_spec`` is untrusted regardless; :func:`clean_spec` drops anything the
    changelist would not accept.
    """

    name = forms.CharField(
        max_length=SavedFilter.NAME_MAX_LENGTH,
        label="Save this view as",
        help_text="Your private name for the current filter selection.",
    )
    filter_spec = forms.CharField(
        required=False, max_length=SPEC_FIELD_MAX_LENGTH, widget=forms.HiddenInput
    )


def changelist_url(model_admin):
    """The changelist this model admin lists on."""
    opts = model_admin.model._meta
    return reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")


def content_type_for(model_admin):
    return ContentType.objects.get_for_model(model_admin.model)


def registered_model_admin(app_label, model_name):
    """The registered ModelAdmin for this model, or ``None``.

    Resolved by name so this shared module never imports an app's models.
    """
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return None
    return admin.site._registry.get(model)


def viewable_model_admin(request, app_label, model_name):
    """The ModelAdmin for this model, or 404 — and only to a viewer, via its
    own ``has_view_permission``."""
    model_admin = registered_model_admin(app_label, model_name)
    if model_admin is None or not model_admin.has_view_permission(request):
        raise Http404
    return model_admin


def require_admin_session(request):
    """Anonymous callers go to the admin login; staff sessions carry on."""
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), reverse("admin:login"))
    return None


def current_spec(request):
    """The selection on screen as a query string, with changelist plumbing
    dropped (and dropped again in :func:`clean_spec`)."""
    params = request.GET.copy()
    for key in list(params.keys()):
        if key in NON_SPEC_PARAMS:
            del params[key]
    return params.urlencode()


def apply_saved_filter(model_admin, request):
    """Fold the requested saved filter into ``request.GET``, returning it or None.

    A foreign or malformed pk is simply no filter, and a parameter already on the
    URL wins so re-applying the same marker is idempotent.
    """
    raw = request.GET.get(APPLY_PARAM, "")
    # Stripped unconditionally: the changelist reads every unrecognised query
    # key as a lookup and would bounce the operator to ``?e=1``.
    params = request.GET.copy()
    params.pop(APPLY_PARAM, None)
    saved = _owned_filter(model_admin, request, raw) if raw else None
    if saved is not None:
        for key, value in saved.params.items():
            # A hand-edited row may hold a non-string; skip it because
            # QueryDict takes strings.
            if isinstance(value, str) and key not in params:
                params[key] = value
    request.GET = params
    return saved


def _owned_filter(model_admin, request, raw):
    """The caller's own saved filter named by ``raw``, or ``None``."""
    if not raw.isdigit():
        return None
    return SavedFilter.objects.filter(
        pk=int(raw),
        user=request.user,
        content_type=content_type_for(model_admin),
    ).first()


def saved_filter_context(model_admin, request):
    """Apply the requested filter and build the bar's context; called before
    delegating so the merge reaches ``request.GET``."""
    active = apply_saved_filter(model_admin, request)
    return {
        "saved_filters": SavedFilter.objects.filter(
            user=request.user, content_type=content_type_for(model_admin)
        ),
        "active_saved_filter": active,
        "saved_filter_form": SavedFilterForm(
            initial={SPEC_FIELD: current_spec(request)}
        ),
        "saved_filter_changelist_url": changelist_url(model_admin),
        "saved_filter_apply_param": APPLY_PARAM,
        "saved_filter_meta": model_admin.model._meta,
    }


def _changelist_accepts(model_admin, request, key, value):
    """Ask this admin's own changelist whether ``key=value`` is playable.

    A stored spec must never be one that would 400, and Django decides. The probe
    restores the query string in a ``finally``.
    """
    original = request.GET
    request.GET = QueryDict(urlencode({key: value}), mutable=True)
    try:
        model_admin.get_changelist_instance(request)
    except (
        DisallowedModelAdminLookup,
        DisallowedModelAdminToField,
        IncorrectLookupParameters,
        SuspiciousOperation,
        ValueError,
    ):
        return False
    finally:
        request.GET = original
    return True


def clean_spec(model_admin, request, raw_spec):
    """The saveable selection: no plumbing, single values only, each accepted by
    the changelist."""
    if not raw_spec:
        return {}
    candidate = QueryDict(raw_spec)
    spec = {}
    for key in candidate:
        values = candidate.getlist(key)
        if key in NON_SPEC_PARAMS or len(values) != 1:
            continue
        if _changelist_accepts(model_admin, request, key, values[0]):
            spec[key] = values[0]
    return spec


def _refuse(model_admin, request, reason):
    """Tell the operator why nothing was saved, on the page they were on."""
    messages.error(request, reason)
    return redirect(changelist_url(model_admin))


def _error_text(form):
    return " ".join(
        message for field_errors in form.errors.values() for message in field_errors
    )


@require_POST
def save_saved_filter(request, app_label, model_name):
    """Store the current filter selection under a name, for this user."""
    login = require_admin_session(request)
    if login is not None:
        return login
    model_admin = viewable_model_admin(request, app_label, model_name)
    form = SavedFilterForm(request.POST)
    if not form.is_valid():
        return _refuse(model_admin, request, _error_text(form))
    spec = clean_spec(model_admin, request, form.cleaned_data.get(SPEC_FIELD, ""))
    if not spec:
        return _refuse(
            model_admin,
            request,
            "Nothing to save: this listing has no filter or search selection.",
        )
    name = form.cleaned_data["name"].strip()
    try:
        with transaction.atomic():
            # update_or_create makes re-saving an update; the unique constraint
            # stays the authority, so a lost race is told, not 500'd.
            SavedFilter.objects.update_or_create(
                user=request.user,
                content_type=content_type_for(model_admin),
                name=name,
                defaults={"params": spec},
            )
    except IntegrityError:
        return _refuse(
            model_admin, request, f'A saved view named "{name}" already exists.'
        )
    messages.success(request, f'Saved the view "{name}".')
    return redirect(changelist_url(model_admin))


@require_POST
def delete_saved_filter(request, pk):
    """Drop one of the caller's own saved views; another user's pk is a 404,
    same as one that does not exist."""
    login = require_admin_session(request)
    if login is not None:
        return login
    saved = get_object_or_404(SavedFilter, pk=pk, user=request.user)
    # No view-permission gate here: a preference row outlives a lost capability,
    # and the redirect target is gated by the changelist anyway.
    model_admin = registered_model_admin(
        saved.content_type.app_label, saved.content_type.model
    )
    target = changelist_url(model_admin) if model_admin else reverse("admin:index")
    saved.delete()
    messages.success(request, f'Deleted the saved view "{saved.name}".')
    return redirect(target)


class SavedFilterMixin:
    """Adds the saved-view bar to a changelist.

    Sits FIRST in the bases so this ``changelist_view`` runs before the admin's
    own override, and the merge happens before anything builds a ChangeList.
    """

    def changelist_view(self, request, extra_context=None):
        context = dict(extra_context or {})
        # Gated on the changelist's own permission, which Django re-checks below.
        if self.has_view_or_change_permission(request):
            context.update(saved_filter_context(self, request))
        return super().changelist_view(request, context)

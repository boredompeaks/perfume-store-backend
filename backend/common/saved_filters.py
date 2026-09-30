"""Per-user saved changelist filters (SPEC-20-6, spec 20.1 [R-20.11]).

Spec 20.1 asks every major admin listing to support "Saved filters/views
where useful" and Django ships no such mechanism, so the two
highest-traffic changelists (orders, products) get one. It is deliberately
thin, and thin in a specific way: a saved filter is a *shortcut to a URL*,
never a second path to the rows.

- ``SavedFilterMixin.changelist_view`` folds a ``?_saved_filter=<pk>``
  marker into the same ``request.GET`` the changelist already reads, and
  then lets Django do the rest — filtering, validation, ordering, the
  queryset. A saved filter therefore cannot surface a row the changelist
  would not have shown anyway, because it never touches the queryset
  itself.
- The pk lookup is scoped to the requesting user *in the query*, so another
  user's saved view is indistinguishable from one that does not exist: the
  apply link can neither apply nor confirm the existence of a foreign view.
- Both write endpoints hang off the ModelAdmin's own
  ``has_view_permission`` (spec 6.12 least privilege), so the surface is
  reachable for exactly the users who could list those rows to begin with.
- A stored spec is validated with ``get_changelist_instance`` before it is
  written. Django's ``ChangeList`` is the authority on which query
  parameters a changelist accepts, and it turns the rest into a 400 — which
  a spec replayed from the database on a later request must never be able
  to produce.

Ordering (``o``) is deliberately NOT part of a saved spec: it is a list of
``list_display`` column *indexes*, so a saved copy silently re-sorts a
different column the moment a column is inserted, and Django re-derives a
sane ordering per request anyway. A saved filter is a filter.
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

# The query parameter that names the saved filter to apply, and the save
# form's hidden field carrying the selection being saved. Module constants,
# not inline literals, because the changelist, the bar template, both write
# endpoints and the stored-spec filter all have to agree on the spelling.
# ``APPLY_PARAM`` is the only query parameter this feature owns — the
# selection itself travels in the POST body as ``SPEC_FIELD``, which is a
# form field name (hence no leading underscore: a template cannot resolve
# one).
APPLY_PARAM = "_saved_filter"
SPEC_FIELD = "filter_spec"
# A changelist URL is short; this bounds the work a crafted POST can ask of
# the changelist-authority probe below (one probe per parameter) and makes
# an over-long selection an honest form error rather than a silent success.
SPEC_FIELD_MAX_LENGTH = 2000
# Everything Django itself ignores on a changelist (show-all, ordering, the
# search term, the popup/source/to_field/facets verbs) plus the two it
# deletes separately (page, error flag) is plumbing, not a filter selection,
# so none of it may be stored. `q` is the one member that IS a real
# selection — the search term — so it is kept. Both names are imported from
# Django rather than retyped, so this set cannot drift from the changelist's
# own idea of "not a filter".
NON_SPEC_PARAMS = (frozenset(IGNORED_PARAMS) - {SEARCH_VAR}) | {
    PAGE_VAR,
    ERROR_FLAG,
    APPLY_PARAM,
    SPEC_FIELD,
}


class SavedFilterForm(forms.Form):
    """The saved-view bar's save form: a name plus the current selection.

    ``filter_spec`` rides as a hidden field because the POST target is the
    save endpoint, not the changelist the bar was rendered on. It is
    treated as untrusted input regardless: :func:`clean_spec` asks the
    changelist which of the submitted parameters it accepts, so a crafted
    value is dropped rather than stored.
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

    Resolved from the registry by name rather than imported, so this shared
    module never imports an app's models (the dependency direction in §14
    runs common -> apps) and an app without a changelist simply does not
    participate.
    """
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return None
    return admin.site._registry.get(model)


def viewable_model_admin(request, app_label, model_name):
    """The ModelAdmin for this model, or 404 — and only to a viewer.

    The gate is the ModelAdmin's own ``has_view_permission``, i.e. the same
    capability-driven method the changelist is gated by, so a saved filter
    exists for exactly the users who can list the rows it filters. An
    unregistered model is a 404 too: there is no changelist to save a
    filter for.
    """
    model_admin = registered_model_admin(app_label, model_name)
    if model_admin is None or not model_admin.has_view_permission(request):
        raise Http404
    return model_admin


def require_admin_session(request):
    """Anonymous callers go to the admin login; staff sessions carry on.

    The same contract the ``capability_required`` chrome routes use
    (conventions.md:26 — a new surface is not open by default, and an
    expired session should land on the login rather than a 404).
    """
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), reverse("admin:login"))
    return None


def current_spec(request):
    """The filter selection on screen, as a query string for the form.

    Changelist plumbing is dropped here (and again in :func:`clean_spec`)
    so the form never carries a page number or an apply marker into a
    stored view.
    """
    params = request.GET.copy()
    for key in list(params.keys()):
        if key in NON_SPEC_PARAMS:
            del params[key]
    return params.urlencode()


def apply_saved_filter(model_admin, request):
    """Fold the requested saved filter into ``request.GET``.

    Returns the SavedFilter that was applied, or ``None``. The lookup is
    scoped to the requesting user and the model, so a foreign or
    malformed pk is simply "no saved filter": the apply link can neither
    apply nor confirm the existence of another user's view.

    A parameter already on the URL always wins. A saved view is a starting
    point an operator can always adjust from, and this also keeps the
    marker idempotent — re-applying the same filter twice is the same URL.
    """
    raw = request.GET.get(APPLY_PARAM, "")
    # The marker is stripped UNCONDITIONALLY, whether or not it resolves: it
    # is this feature's own parameter, and the changelist reads every
    # unrecognised query key as a filter lookup — left in place it would be
    # rejected as an unknown lookup and bounce the operator to `?e=1`.
    params = request.GET.copy()
    params.pop(APPLY_PARAM, None)
    saved = _owned_filter(model_admin, request, raw) if raw else None
    if saved is not None:
        for key, value in saved.params.items():
            # A JSON column is not schema-validated, so a hand-edited row
            # can hold a non-string. Such an entry is not a filter
            # selection: skip it rather than let it reach the QueryDict,
            # which takes strings.
            if isinstance(value, str) and key not in params:
                params[key] = value
    request.GET = params
    return saved


def _owned_filter(model_admin, request, raw):
    """The caller's own saved filter named by ``raw``, or ``None``.

    A malformed or foreign pk is "no saved filter" — never an error and
    never a hint that someone else's view exists.
    """
    if not raw.isdigit():
        return None
    return SavedFilter.objects.filter(
        pk=int(raw),
        user=request.user,
        content_type=content_type_for(model_admin),
    ).first()


def saved_filter_context(model_admin, request):
    """Apply the requested saved filter and build the bar's context.

    Called from ``changelist_view`` BEFORE delegating to Django: the merge
    has to reach ``request.GET`` because that is what the changelist reads,
    and the form's selection is read AFTER the merge so "save this view"
    captures what is actually on screen.
    """
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

    ``get_changelist_instance`` raises for a parameter the changelist
    cannot honour, which the changelist renders as a 400. A stored spec is
    replayed from the database on a later request, so anything that would
    400 must never reach the table — and Django, not a re-implementation of
    its filter vocabulary, decides.

    The probe borrows the live request and puts its query string back in a
    ``finally``: a shallow copy would share the session and the message
    store with the real response, which is exactly what a validation probe
    must not touch.
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
    """The saveable filter selection inside a submitted spec field.

    Two filters, both about what may be REPLAYED later:

    - the parameter must not be changelist plumbing and must carry a single
      value — a multi-valued key is a hand-built one-off URL, not a view
      worth keeping;
    - the changelist must accept it (see :func:`_changelist_accepts`).
    """
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
            # Re-saving a name replaces its selection — update_or_create
            # makes that one call, and the (user, model, name) constraint
            # stays the authority on uniqueness: a concurrent save of the
            # same name loses the race and is told so, rather than 500ing.
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
    """Drop one of the caller's own saved views.

    A pk that is not the caller's is a 404, the same answer as a pk that
    does not exist: the apply and delete links are guessable ids, and
    neither may confirm that someone else's view is there.
    """
    login = require_admin_session(request)
    if login is not None:
        return login
    saved = get_object_or_404(SavedFilter, pk=pk, user=request.user)
    # No view-permission gate on the way in: the row is the caller's own
    # preference, not domain data, so a staff member who has since lost the
    # capability can still clear a stale saved view. The redirect target is
    # then refused by the changelist's own gate, which is the honest answer.
    model_admin = registered_model_admin(
        saved.content_type.app_label, saved.content_type.model
    )
    target = changelist_url(model_admin) if model_admin else reverse("admin:index")
    saved.delete()
    messages.success(request, f'Deleted the saved view "{saved.name}".')
    return redirect(target)


class SavedFilterMixin:
    """Adds the saved-view bar to a changelist (SPEC-20-6 [R-20.11]).

    Sits FIRST in the ModelAdmin's bases so this ``changelist_view`` runs
    before the admin's own override — ``ProductAdmin`` narrows
    ``list_editable`` for a role without ``inventory.adjust`` and must
    still route through the saved-filter merge. The merge has to reach
    ``request.GET`` before anything builds a ChangeList from it, and the
    bar's context has to be built after the merge so "save this view"
    captures the selection actually on screen.
    """

    def changelist_view(self, request, extra_context=None):
        context = dict(extra_context or {})
        # Gated on the changelist's own permission, which Django re-checks
        # right below and answers with a 403: a user who cannot list these
        # rows must not reach the saved-filter table at all.
        if self.has_view_or_change_permission(request):
            context.update(saved_filter_context(self, request))
        return super().changelist_view(request, context)

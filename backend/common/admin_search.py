"""Unified global admin search (SPEC-5-10, spec 5.1).

Spec 5.1 draws the admin's one search box as "Search orders, products,
customers…" — one entry point over the models staff actually work in,
instead of a per-model box that only ever searches one changelist. Django
has no such thing, and the honest way to build it is to reuse the pieces
the admin already trusts rather than invent a second search engine:

- a model's results come from its own registered ``ModelAdmin``
  (``get_search_results`` over ``get_queryset``, i.e. the same declared
  ``search_fields``, the same ``lookup_allowed`` vocabulary and the same
  root queryset its changelist uses), so the global search cannot reach a
  field the changelist would not have searched;
- a model's results appear only if that ModelAdmin's own
  ``has_view_permission`` allows the caller. That is the same
  capability-driven method the changelist is gated by (spec 6.12 least
  privilege), so a role without ``orders.read`` gets no order results and
  the order query is never even issued — and the model is not named on the
  page either, so a gated model's existence is not confirmed;
- the route itself is gated by ``capability_required_any`` over the three
  view capabilities: a staff member who may view none of them has no
  business on a page that would only ever be empty (403, not a blank
  page). The superuser bypass every other admin surface keeps is preserved
  by the decorator.

A result row links to the object's own change page, which the admin renders
read-only for a viewer — the same page the changelist's first column links
to — and each group links to that model's changelist with the term applied,
so the global search is a shortcut into the ordinary, capability-gated
surfaces rather than a parallel read path.
"""

from collections import namedtuple

from django.apps import apps
from django.contrib import admin
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import urlencode

from common.permissions import capability_required_any

# One searchable model: the (app_label, model_name) pair resolved through
# the admin registry, plus the heading the spec's own vocabulary gives it
# ("Search orders, products, customers…"). The label is declared rather
# than derived because the catalogue model's name is the legacy plural
# ``products``, whose derived plural reads "productss".
SearchTarget = namedtuple("SearchTarget", "app_label model_name label")

SEARCH_TARGETS = (
    SearchTarget("products", "products", "Products"),
    SearchTarget("orders", "order", "Orders"),
    SearchTarget("auth", "user", "Customers"),
)
# The capabilities that may open the page at all — the three targets' own
# view capabilities. Duplicated as literals here only because a decorator
# argument is evaluated at import time, before the registry can be read;
# the test suite pins this tuple to be exactly the registered ModelAdmins'
# ``capability_map["view"]`` so the two cannot drift.
SEARCH_CAPABILITIES = ("products.read", "orders.read", "customers.read")
# Results per model on the page, and a bound on the term itself: a staff
# search is an icontains scan, so an unbounded string is a needless table
# scan. The bound is a presentation limit, not an authorization one.
RESULTS_PER_MODEL = 10
MAX_TERM_LENGTH = 200


def registered_model_admin(app_label, model_name):
    """The registered ModelAdmin for this model, or ``None``."""
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return None
    return admin.site._registry.get(model)


def _change_url(model_admin, obj):
    opts = model_admin.model._meta
    return reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk])


def _search_group(request, target, term):
    """One model's results, or ``None`` when the caller may not view it.

    ``None`` means the model is not part of this page for this caller: the
    capability check comes first, so a gated model's query is never issued
    and its heading never renders.
    """
    model_admin = registered_model_admin(target.app_label, target.model_name)
    if model_admin is None or not model_admin.has_view_permission(request):
        return None
    opts = model_admin.model._meta
    changelist = reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
    group = {
        "title": target.label,
        "total": 0,
        "rows": [],
        "changelist_url": changelist,
    }
    if not term:
        return group
    queryset, _may_have_duplicates = model_admin.get_search_results(
        request, model_admin.get_queryset(request), term
    )
    group["total"] = queryset.count()
    group["rows"] = [
        {"label": str(obj), "url": _change_url(model_admin, obj)}
        for obj in queryset[:RESULTS_PER_MODEL]
    ]
    # "see all" hands the term back to the changelist, which re-applies its
    # own capability gate — the full result set is never rendered here.
    group["changelist_url"] = f"{changelist}?{urlencode({'q': term})}"
    return group


@capability_required_any(SEARCH_CAPABILITIES)
def global_search(request):
    """Search the three highest-value models at once, capability-gated."""
    term = (request.GET.get("q") or "").strip()[:MAX_TERM_LENGTH]
    groups = [
        group
        for group in (_search_group(request, target, term) for target in SEARCH_TARGETS)
        if group is not None
    ]
    return render(
        request,
        "admin/global_search.html",
        {
            "title": "Search",
            "term": term,
            "groups": groups,
            "total": sum(group["total"] for group in groups),
        },
    )

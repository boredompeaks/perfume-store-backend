"""Unified global admin search over the three highest-value models.

Results come from each model's own ModelAdmin and view permission, so the search is
only a shortcut into the ordinary capability-gated surfaces.
"""

from collections import namedtuple

from django.apps import apps
from django.contrib import admin
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import urlencode

from common.permissions import capability_required_any

# The heading is declared because the ``products`` model's derived plural reads
# "productss".
SearchTarget = namedtuple("SearchTarget", "app_label model_name label")

SEARCH_TARGETS = (
    SearchTarget("products", "products", "Products"),
    SearchTarget("orders", "order", "Orders"),
    SearchTarget("auth", "user", "Customers"),
)
# Import-time literals because a decorator argument is evaluated before the
# registry can be read; a test pins them to the admins' view capabilities.
SEARCH_CAPABILITIES = ("products.read", "orders.read", "customers.read")
# Presentation bounds, not authorization: the term bound limits an icontains
# scan.
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

    The capability check comes first, so a gated model's query is never issued.
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
    # "see all" hands the term to the changelist, which re-applies its own gate.
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

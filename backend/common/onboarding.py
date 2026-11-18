"""Staff onboarding: what each role can do, derived from the live registry.

Deriving from ``CAPABILITY_ROLES`` and the admin registry means a newly registered
admin or a moved capability is reported without editing this module.
"""

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse

from .permissions import get_user_roles, user_has_capability
from .roles import CAPABILITY_ROLES, STAFF_ROLES


def staff_only(view):
    """Staff gate with the admin's answers: anonymous goes to login, an
    authenticated non-staff caller gets 403."""

    @login_required(login_url="admin:login")
    def guarded(request, *args, **kwargs):
        if not (request.user.is_active and request.user.is_staff):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return guarded


def _surface_url(model):
    """The surface's changelist URL, or the admin index when it has none."""
    opts = model._meta
    try:
        return reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
    except NoReverseMatch:
        # A registry shape the page did not anticipate must not 500 the page.
        return reverse("admin:index")


def admin_surfaces():
    """Every registered surface that declares RBAC metadata; admins with none
    are omitted rather than shown as if ungated."""
    surfaces = []
    for model, model_admin in admin.site._registry.items():
        capability_map = {
            kind: capability
            for kind, capability in (
                getattr(model_admin, "capability_map", {}) or {}
            ).items()
            if capability
        }
        action_capabilities = {
            name: capability
            for name, capability in (
                getattr(model_admin, "action_capabilities", {}) or {}
            ).items()
            if capability
        }
        scoped = getattr(model_admin, "scoped_view_capability", None)
        if not capability_map and not action_capabilities and not scoped:
            continue
        surfaces.append(
            {
                "label": str(model._meta.verbose_name_plural),
                "url": _surface_url(model),
                "capability_map": capability_map,
                "action_capabilities": action_capabilities,
                "scoped_view_capability": scoped,
            }
        )
    return surfaces


def _capability_access(surfaces, capability):
    """The surfaces and actions ``capability`` unlocks, in registry order."""
    access = []
    for surface in surfaces:
        kinds = sorted(
            kind
            for kind, mapped in surface["capability_map"].items()
            if mapped == capability
        )
        actions = sorted(
            name
            for name, mapped in surface["action_capabilities"].items()
            if mapped == capability
        )
        scoped = surface["scoped_view_capability"] == capability
        if not kinds and not actions and not scoped:
            continue
        access.append(
            {
                "label": surface["label"],
                "url": surface["url"],
                "kinds": kinds,
                "actions": actions,
                "scoped_view": scoped,
            }
        )
    return access


def _role_capabilities(role, surfaces, user):
    """Capabilities held by ``role`` and what each unlocks.

    ``held_by_user`` is computed against the requesting user, including the
    superuser bypass, so the page highlights what the caller already has.
    """
    entries = []
    for capability in sorted(CAPABILITY_ROLES):
        if role not in CAPABILITY_ROLES[capability]:
            continue
        entries.append(
            {
                "name": capability,
                "held_by_user": user.is_superuser
                or user_has_capability(user, capability),
                "access": _capability_access(surfaces, capability),
            }
        )
    return entries


@staff_only
def onboarding(request):
    """Render the role-by-role onboarding page.

    ``staff_only`` is its only gate: the page contains no customer data or secrets.
    """
    surfaces = admin_surfaces()
    user_roles = get_user_roles(request.user)
    roles = [
        {
            "name": role,
            "label": role.replace("_", " ").capitalize(),
            "is_current": role in user_roles,
            "capabilities": _role_capabilities(role, surfaces, request.user),
        }
        for role in STAFF_ROLES
    ]
    return render(
        request,
        "admin/onboarding.html",
        {
            "title": "Staff onboarding",
            "roles": roles,
            "surface_count": len(surfaces),
            "is_superuser": request.user.is_superuser,
        },
    )

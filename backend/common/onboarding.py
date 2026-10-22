"""Staff onboarding: what each role can do, derived from the live registry.

SPEC-D-06 asks for an admin page that explains a staff role in terms of the
capabilities it holds and the admin surfaces those capabilities unlock. The
content is DERIVED, never hand-maintained:

* ``common.roles.CAPABILITY_ROLES`` says which capabilities a role holds;
* ``django.contrib.admin.site._registry`` says which registered ModelAdmin
  declares each capability (``capability_map``), which bulk action needs it
  (``action_capabilities``), and which surface has a second door
  (``scoped_view_capability``).

A hand-written table beside those three sources would be a fourth authority
that drifts silently. Deriving instead means a newly registered admin appears
here on the next request, and a capability moved from one surface to another is
reported correctly with no edit to this module.
"""

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse

from .permissions import get_user_roles, user_has_capability
from .roles import CAPABILITY_ROLES, STAFF_ROLES


def staff_only(view):
    """Staff gate with the admin's exact answers.

    ``staff_member_required`` sends EVERY failing caller to the login page,
    including an authenticated customer, while the admin itself answers such a
    caller with 403. This route lives in the admin's namespace and follows the
    admin contract: anonymous -> login, authenticated non-staff -> 403. The
    predicate is the same one ``staff_member_required`` uses (active AND
    staff); only the failure response differs.
    """

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
        # A ModelAdmin can be registered without a changelist (Django still
        # builds one for every model in practice); the page must not 500 on
        # a registry shape it did not anticipate.
        return reverse("admin:index")


def admin_surfaces():
    """Every registered surface that declares RBAC metadata.

    Django registers a handful of models that are not part of this store's
    role model (and third-party apps may add more). A ModelAdmin with no
    ``capability_map``, no ``action_capabilities`` and no
    ``scoped_view_capability`` is not described by ``CAPABILITY_ROLES`` at all,
    so it is omitted rather than presented as if it were ungated.
    """
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
    """Capabilities held by ``role`` and what each one unlocks.

    ``held_by_user`` is computed against the REQUESTING user, not the role
    being described, so the page can highlight the capabilities the caller
    already has. A superuser bypasses the map exactly as ``RoleAwareModelAdmin``
    does, which is why the flag is not just a role comparison.
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

    ``staff_only`` is the route's only gate: the page names capabilities and
    surfaces but contains no customer rows, no secrets and no per-order data,
    so reading it needs staff membership rather than any one operational
    capability.
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

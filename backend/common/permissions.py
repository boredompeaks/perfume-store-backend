"""Shared DRF permission classes.

conventions.md: authorize via ``permission_classes`` — never inline
``request.user.is_staff`` checks.
"""
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission

from common.roles import CAPABILITY_ROLES, STAFF_ROLES


class IsAdminUserOrReadOnly(BasePermission):
    """Read-only for everyone, staff-only for write methods.

    DRF ships no admin-or-read-only permission, and a blanket ``IsAdminUser``
    on the multi-method product views would also block anonymous catalogue
    GETs. Writes therefore mirror ``IsAdminUser`` (``is_staff``) while safe
    methods stay public — the exact legacy behaviour of the inline checks.

    Subclasses may pin ``write_capability`` to source write authority from
    the RBAC roles map instead of the blanket ``is_staff`` flag (see
    ``capability_or_read_only``); the legacy flag remains the default so
    existing deployments keep their contract.
    """

    # Keeps the 403 body byte-identical to the legacy inline gate.
    message = "Administrator access is required."
    write_capability = None

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        if self.write_capability is not None:
            if user_has_capability(request.user, self.write_capability):
                return True
        elif request.user and request.user.is_staff:
            return True
        # Raised directly rather than returning False: with a JWT
        # authenticator attached, DRF turns a plain failed check into
        # 401 NotAuthenticated for guests, while the legacy contract (and
        # the pinned tests) is 403 "Administrator access is required."
        raise PermissionDenied(self.message)


_ROLE_SET = frozenset(STAFF_ROLES)


def get_user_roles(user):
    """Staff roles a user holds, derived from Group membership.

    ``sync_role_groups`` keeps exactly one Group per staff role, so the
    group set is the role set; intersecting with ``STAFF_ROLES`` stops
    unrelated groups from accidentally granting staff authority. Anonymous
    and unauthenticated users hold no roles.
    """
    if not (user and user.is_authenticated):
        return frozenset()
    return frozenset(user.groups.values_list("name", flat=True)) & _ROLE_SET


def user_has_capability(user, capability):
    """True when one of the user's roles grants ``capability``.

    Authority flows solely through ``CAPABILITY_ROLES`` — the map already
    grants ``admin`` every capability, so full staff authority needs no
    separate staff/superuser branch. Unknown capability identifiers deny
    by default (least privilege): adding a capability to the map without
    wiring it must never silently open access.
    """
    return bool(get_user_roles(user) & CAPABILITY_ROLES.get(capability, frozenset()))


class CapabilityPermission(BasePermission):
    """Deny unless the user's staff role grants the pinned ``capability``.

    Subclasses/factory set ``capability`` to an identifier from
    ``CAPABILITY_ROLES``; the unpinned base class therefore denies everyone.

    Denials raise ``PermissionDenied`` instead of returning ``False``: with
    a JWT authenticator attached DRF turns a plain failed check into 401
    for guests, while the staff API contract is a uniform 403 for both
    anonymous and merely unprivileged callers (same reasoning as
    ``IsAdminUserOrReadOnly`` above).
    """

    capability = None
    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        if user_has_capability(request.user, self.capability):
            return True
        raise PermissionDenied(self.message)


def _permission_class_name(capability):
    """``orders.fulfill`` -> ``HasOrdersFulfill`` (stable, importable name)."""
    return "Has" + "".join(part.capitalize() for part in capability.split("."))


def capability_permission(capability):
    """Factory: a ``CapabilityPermission`` subclass pinned to one capability."""
    return type(
        _permission_class_name(capability),
        (CapabilityPermission,),
        {"capability": capability},
    )


def capability_or_read_only(capability):
    """Factory: SAFE_METHODS stay public, writes need the pinned capability.

    The product endpoints mix a public catalogue read with staff writes in
    one view, so a plain ``CapabilityPermission`` (which denies safe methods
    too) cannot express them. Pinning ``write_capability`` on the
    ``IsAdminUserOrReadOnly`` seam reuses its public-read handling and legacy
    403 body while write authority moves from the blanket ``is_staff`` flag
    to ``CAPABILITY_ROLES`` — a tightening, never a downgrade.
    """
    return type(
        _permission_class_name(capability) + "OrReadOnly",
        (IsAdminUserOrReadOnly,),
        {"write_capability": capability},
    )


# One named class per capability so views can declare e.g.
# ``permission_classes = [HasOrdersFulfill]``. The set is pinned against
# ``CAPABILITY_ROLES`` in tests/test_rbac_foundation.py so it cannot drift
# from the roles map.
HasProductsRead = capability_permission("products.read")
HasProductsWrite = capability_permission("products.write")
HasProductsPublish = capability_permission("products.publish")
HasInventoryRead = capability_permission("inventory.read")
HasInventoryAdjust = capability_permission("inventory.adjust")
HasOrdersRead = capability_permission("orders.read")
HasOrdersFulfill = capability_permission("orders.fulfill")
HasOrdersCancel = capability_permission("orders.cancel")
HasRefundsCreate = capability_permission("refunds.create")
HasCustomersRead = capability_permission("customers.read")
HasDiscountsWrite = capability_permission("discounts.write")
HasReportsRead = capability_permission("reports.read")
HasStaffManage = capability_permission("staff.manage")
HasSettingsManage = capability_permission("settings.manage")

# SPEC-6-03c: the product views are the read/write split shape — public
# catalogue reads, writes gated by ``products.write`` (catalogue + admin).
HasProductsWriteOrReadOnly = capability_or_read_only("products.write")

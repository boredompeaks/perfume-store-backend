"""Shared DRF permission classes.

Authorization goes through ``permission_classes``, never inline ``is_staff`` checks;
``capability_required`` is the plain-Django twin for admin-chrome routes.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.urls import reverse
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission

from common.roles import (
    CAPABILITY_ROLES,
    ROLE_GRANT_CAPABILITY,
    STAFF_ROLES,
)


class IsAdminUserOrReadOnly(BasePermission):
    """Read-only for everyone, staff-only for writes.

    Subclasses may pin ``write_capability`` to source writes from the roles map
    instead of the blanket ``is_staff`` flag.
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
        # Raised rather than returning False: with a JWT authenticator DRF would
        # turn a failed check into 401 for guests; the pinned contract is 403.
        raise PermissionDenied(self.message)


_ROLE_SET = frozenset(STAFF_ROLES)


def get_user_roles(user):
    """Staff roles a user holds from Group membership, intersected with
    ``STAFF_ROLES`` so unrelated groups grant nothing."""
    if not (user and user.is_authenticated):
        return frozenset()
    return frozenset(user.groups.values_list("name", flat=True)) & _ROLE_SET


def user_has_capability(user, capability):
    """True when one of the user's roles grants ``capability``; unknown
    identifiers deny by default."""
    return bool(get_user_roles(user) & CAPABILITY_ROLES.get(capability, frozenset()))


class CapabilityPermission(BasePermission):
    """Deny unless the user's staff role grants the pinned ``capability``.

    The unpinned base denies everyone. Denials raise so DRF answers 403, not 401,
    for guests (same reasoning as ``IsAdminUserOrReadOnly``).
    """

    capability = None
    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        if user_has_capability(request.user, self.capability):
            return True
        raise PermissionDenied(self.message)


def capability_required(capability):
    """Decorator: the plain-Django twin of ``CapabilityPermission``.

    Anonymous callers go to the admin login and an authenticated caller without
    the capability gets 403. Staff membership is required too, matching every
    other view inside ``/admin/``; the superuser bypass is preserved.
    """

    def decorator(view):
        @wraps(view)
        def gated(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(
                    request.get_full_path(), reverse("admin:login")
                )
            if user.is_superuser or (
                user.is_staff and user_has_capability(user, capability)
            ):
                return view(request, *args, **kwargs)
            raise DjangoPermissionDenied(
                "You do not have permission to perform this action."
            )

        return gated

    return decorator


def capability_required_any(capabilities):
    """Decorator: like :func:`capability_required` but passes when the caller
    holds any one of ``capabilities``; each surface still gates its own results."""

    def decorator(view):
        @wraps(view)
        def gated(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(
                    request.get_full_path(), reverse("admin:login")
                )
            if user.is_superuser or (
                user.is_staff
                and any(
                    user_has_capability(user, capability) for capability in capabilities
                )
            ):
                return view(request, *args, **kwargs)
            raise DjangoPermissionDenied(
                "You do not have permission to perform this action."
            )

        return gated

    return decorator


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


def user_may_assign_role(user, role):
    """Whether ``user`` may add or remove ``role`` on a staff account.

    The escalation guard: roles in ``ROLE_GRANT_CAPABILITY`` need their own
    capability, so an Admin can never promote above its tier. The superuser
    bypass is preserved.
    """
    required = ROLE_GRANT_CAPABILITY.get(role)
    if required is None:
        return True
    if user is None:
        return False
    return user.is_superuser or user_has_capability(user, required)


def capability_or_read_only(capability):
    """Factory: public reads, writes gated by the pinned capability, reusing
    ``IsAdminUserOrReadOnly``'s 403 body."""
    return type(
        _permission_class_name(capability) + "OrReadOnly",
        (IsAdminUserOrReadOnly,),
        {"write_capability": capability},
    )


# One named class per capability; a test pins the set to ``CAPABILITY_ROLES``.
HasProductsRead = capability_permission("products.read")
HasProductsWrite = capability_permission("products.write")
HasProductsPublish = capability_permission("products.publish")
HasInventoryRead = capability_permission("inventory.read")
HasInventoryAdjust = capability_permission("inventory.adjust")
HasOrdersRead = capability_permission("orders.read")
HasOrdersFulfill = capability_permission("orders.fulfill")
HasOrdersCancel = capability_permission("orders.cancel")
HasRefundsCreate = capability_permission("refunds.create")
# The shipping rate admin gate; required by the one-class-per-capability
# invariant even though the public estimate is read-only.
HasShippingManage = capability_permission("shipping.manage")
# Shipment tracking gates; no customer endpoint uses them, but the
# one-class-per-capability invariant requires them.
HasShipmentsRead = capability_permission("shipments.read")
HasShipmentsWrite = capability_permission("shipments.write")
# Return-request staff gates for the review queue; required by the
# one-class-per-capability invariant.
HasReturnsRead = capability_permission("returns.read")
HasReturnsWrite = capability_permission("returns.write")
HasCustomersRead = capability_permission("customers.read")
HasDiscountsWrite = capability_permission("discounts.write")
HasReportsRead = capability_permission("reports.read")
HasStaffManage = capability_permission("staff.manage")
HasSettingsManage = capability_permission("settings.manage")
# The one capability superadmin holds and admin does not; authorises granting
# that tier itself (see ``user_may_assign_role``).
HasPlatformConfigure = capability_permission("platform.configure")

# Public catalogue reads, writes gated by ``products.write``.
HasProductsWriteOrReadOnly = capability_or_read_only("products.write")


def is_privileged(user):
    """The accounts mandatory-MFA population: ``staff.manage`` holders plus
    Django superusers, so the trust anchor cannot slip past the factor."""
    if not (user and user.is_authenticated):
        return False
    return user.is_superuser or user_has_capability(user, "staff.manage")


class IsPrivilegedRole(BasePermission):
    """Permission twin of :func:`is_privileged` for the MFA endpoints; the
    superuser bypass keeps enrollment from dead-locking the trust anchor."""

    message = "Multi-factor authentication management requires privileged staff access."

    def has_permission(self, request, view):
        if is_privileged(request.user):
            return True
        raise PermissionDenied(self.message)

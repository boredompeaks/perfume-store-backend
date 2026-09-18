"""Shared DRF permission classes.

conventions.md: authorize via ``permission_classes`` — never inline
``request.user.is_staff`` checks.
"""
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsAdminUserOrReadOnly(BasePermission):
    """Read-only for everyone, staff-only for write methods.

    DRF ships no admin-or-read-only permission, and a blanket ``IsAdminUser``
    on the multi-method product views would also block anonymous catalogue
    GETs. Writes therefore mirror ``IsAdminUser`` (``is_staff``) while safe
    methods stay public — the exact legacy behaviour of the inline checks.
    """

    # Keeps the 403 body byte-identical to the legacy inline gate.
    message = "Administrator access is required."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        if request.user and request.user.is_staff:
            return True
        # Raised directly rather than returning False: with a JWT
        # authenticator attached, DRF turns a plain failed check into
        # 401 NotAuthenticated for guests, while the legacy contract (and
        # the pinned tests) is 403 "Administrator access is required."
        raise PermissionDenied(self.message)

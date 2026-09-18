"""RBAC foundation tests (spec 6.12): staff role groups sync idempotently.

``common.roles`` is the source of truth for the six staff roles; the
``common`` bootstrap data migration calls ``sync_role_groups`` during
``migrate``. These tests pin that the sync is safe to re-run: exactly the
six stable-named groups exist, and a deleted group is re-created rather
than silently missing from a half-bootstrapped database.

They also pin the capability permission layer built on the roles map:
allow/deny per role, the full role->capability matrix against
``CAPABILITY_ROLES``, and that the legacy ``IsAdminUserOrReadOnly``
contract is untouched by the group-based layer.
"""
from django.contrib.auth.models import AnonymousUser, Group, User
from django.test import TestCase
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

import common.permissions
from common.permissions import (
    CapabilityPermission,
    HasProductsWriteOrReadOnly,
    HasSettingsManage,
    HasStaffManage,
    IsAdminUserOrReadOnly,
    capability_or_read_only,
    capability_permission,
    get_user_roles,
    user_has_capability,
)
from common.roles import (
    CAPABILITY_ROLES,
    ROLE_ADMIN,
    ROLE_CATALOGUE,
    ROLE_SUPPORT,
    STAFF_ROLES,
    sync_role_groups,
)


def _named_permission(capability):
    """Resolve the named class for a capability, pinning the naming rule."""
    name = "Has" + "".join(part.capitalize() for part in capability.split("."))
    return getattr(common.permissions, name)


class StaffRoleGroupSyncTests(TestCase):
    def test_sync_twice_leaves_exactly_six_groups_with_stable_names(self):
        # The bootstrap migration already ran on the test database; re-running
        # the sync (ops scripts, future migrations) must not duplicate groups.
        sync_role_groups()
        sync_role_groups()

        self.assertEqual(
            set(Group.objects.values_list("name", flat=True)), set(STAFF_ROLES)
        )
        for role in STAFF_ROLES:
            self.assertEqual(Group.objects.filter(name=role).count(), 1)

    def test_sync_recreates_a_missing_role_group(self):
        Group.objects.filter(name=ROLE_CATALOGUE).delete()
        self.assertFalse(Group.objects.filter(name=ROLE_CATALOGUE).exists())

        groups = sync_role_groups()

        self.assertTrue(Group.objects.filter(name=ROLE_CATALOGUE).exists())
        self.assertEqual(groups[ROLE_CATALOGUE].name, ROLE_CATALOGUE)


class CapabilityPermissionMatrixTests(TestCase):
    """Allow/deny for every capability x {anonymous, customer, 6 roles}."""

    def setUp(self):
        groups = sync_role_groups()
        self.role_users = {}
        for role in STAFF_ROLES:
            # No usable password needed: these checks never authenticate.
            user = User.objects.create_user(username=f"role-{role}")
            user.groups.add(groups[role])
            self.role_users[role] = user
        # Authenticated but role-less: a plain customer must gain nothing.
        self.customer = User.objects.create_user(username="rbac-customer")
        self.factory = APIRequestFactory()

    def _request(self, user):
        request = Request(self.factory.generic("GET", "/api/staff/"))
        request.user = user
        return request

    def _assert_denied(self, permission, user):
        with self.assertRaises(PermissionDenied):
            permission.has_permission(self._request(user), None)

    def test_anonymous_is_denied_every_capability(self):
        for capability in CAPABILITY_ROLES:
            with self.subTest(capability=capability):
                self._assert_denied(_named_permission(capability)(), AnonymousUser())

    def test_roleless_customer_is_denied_every_capability(self):
        for capability in CAPABILITY_ROLES:
            with self.subTest(capability=capability):
                self._assert_denied(_named_permission(capability)(), self.customer)

    def test_role_capability_matrix_matches_the_map(self):
        for capability, granted_roles in CAPABILITY_ROLES.items():
            for role, user in self.role_users.items():
                with self.subTest(capability=capability, role=role):
                    permission = _named_permission(capability)()
                    if role in granted_roles:
                        self.assertTrue(
                            permission.has_permission(self._request(user), None)
                        )
                    else:
                        self._assert_denied(permission, user)

    def test_unknown_capability_and_anonymous_roles_deny_by_default(self):
        # An identifier missing from the map must never open access, and an
        # anonymous user resolves to the empty role set.
        self.assertFalse(
            user_has_capability(self.role_users[ROLE_ADMIN], "orders.demolish")
        )
        self.assertEqual(get_user_roles(AnonymousUser()), frozenset())

    def test_unpinned_base_capability_permission_denies_even_admin(self):
        # capability=None base class: denying is the default, so a subclass
        # that forgets to pin a capability can never unlock a view.
        self._assert_denied(CapabilityPermission(), self.role_users[ROLE_ADMIN])


class CapabilityPermissionClassContractTests(TestCase):
    """The named permission classes stay in lockstep with CAPABILITY_ROLES."""

    def test_every_capability_has_exactly_one_named_class(self):
        expected_names = {
            "Has"
            + "".join(part.capitalize() for part in capability.split(".")): capability
            for capability in CAPABILITY_ROLES
        }
        for name, capability in expected_names.items():
            with self.subTest(capability=capability):
                permission_class = getattr(common.permissions, name)
                self.assertTrue(issubclass(permission_class, CapabilityPermission))
                self.assertEqual(permission_class.capability, capability)

        # No capability-pinned classes beyond the map either.
        pinned = {
            cls.capability
            for cls in vars(common.permissions).values()
            if isinstance(cls, type)
            and issubclass(cls, BasePermission)
            and getattr(cls, "capability", None)
        }
        self.assertEqual(pinned, set(CAPABILITY_ROLES))

    def test_factory_builds_a_pinned_subclass(self):
        permission_class = capability_permission("products.read")
        self.assertTrue(issubclass(permission_class, CapabilityPermission))
        self.assertEqual(permission_class.capability, "products.read")
        self.assertEqual(permission_class.__name__, "HasProductsRead")


class IsAdminUserOrReadOnlyRegressionTests(TestCase):
    """The legacy is_staff gate is untouched by the roles-based layer."""

    def setUp(self):
        self.permission = IsAdminUserOrReadOnly()
        self.staff = User.objects.create_user(username="legacy-staff", is_staff=True)
        self.customer = User.objects.create_user(username="legacy-customer")
        self.factory = APIRequestFactory()

    def _request(self, method, user):
        request = Request(self.factory.generic(method, "/api/products/"))
        request.user = user
        return request

    def test_message_keeps_the_legacy_403_body(self):
        self.assertEqual(self.permission.message, "Administrator access is required.")

    def test_safe_methods_stay_public(self):
        for method in ("GET", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                self.assertTrue(
                    self.permission.has_permission(
                        self._request(method, AnonymousUser()), None
                    )
                )

    def test_write_methods_keep_the_legacy_staff_contract(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                for user in (AnonymousUser(), self.customer):
                    with self.assertRaises(PermissionDenied) as ctx:
                        self.permission.has_permission(
                            self._request(method, user), None
                        )
                    self.assertEqual(
                        str(ctx.exception), "Administrator access is required."
                    )
                # The legacy staff flag still grants writes with no role
                # groups at all — the new layer adds to it, not replaces it.
                self.assertTrue(
                    self.permission.has_permission(
                        self._request(method, self.staff), None
                    )
                )


class HasProductsWriteOrReadOnlyUnitTests(TestCase):
    """Unit contract for the method-aware capability gate (SPEC-6-03c).

    The product views mix public catalogue reads with staff writes, so the
    gate rides the ``IsAdminUserOrReadOnly`` seam with a pinned
    ``write_capability`` instead of the blanket ``is_staff`` flag."""

    def setUp(self):
        groups = sync_role_groups()
        self.catalogue = User.objects.create_user(username="gate-catalogue")
        self.catalogue.groups.add(groups[ROLE_CATALOGUE])
        self.support = User.objects.create_user(username="gate-support")
        self.support.groups.add(groups[ROLE_SUPPORT])
        self.factory = APIRequestFactory()

    def _request(self, method, user):
        request = Request(self.factory.generic(method, "/api/products/"))
        request.user = user
        return request

    def test_named_instance_is_pinned_to_products_write(self):
        self.assertEqual(HasProductsWriteOrReadOnly.write_capability, "products.write")

    def test_factory_builds_a_seam_subclass_not_a_capability_permission(self):
        # Subclasses the legacy seam and keys on ``write_capability`` — a
        # different attribute from CapabilityPermission's ``capability`` — so
        # the named-class drift scan in CapabilityPermissionClassContractTests
        # cannot mistake it for a plain capability class.
        factory_class = capability_or_read_only("products.write")
        self.assertTrue(issubclass(factory_class, IsAdminUserOrReadOnly))
        self.assertFalse(issubclass(factory_class, CapabilityPermission))
        self.assertEqual(factory_class.write_capability, "products.write")
        self.assertEqual(factory_class.__name__, "HasProductsWriteOrReadOnly")

    def test_safe_methods_stay_public_for_everyone(self):
        permission = HasProductsWriteOrReadOnly()
        for method in ("GET", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                for user in (AnonymousUser(), self.support, self.catalogue):
                    self.assertTrue(
                        permission.has_permission(self._request(method, user), None)
                    )

    def test_writes_follow_the_capability_map(self):
        permission = HasProductsWriteOrReadOnly()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertTrue(
                    permission.has_permission(
                        self._request(method, self.catalogue), None
                    )
                )
                with self.assertRaises(PermissionDenied) as ctx:
                    permission.has_permission(self._request(method, self.support), None)
                self.assertEqual(
                    str(ctx.exception), "Administrator access is required."
                )


class PrivilegeEscalationGuardTests(TestCase):
    """Spec 6.12 line 2261: only admin-role staff may assign roles/permissions.

    No role-assignment surface exists yet (SPEC-6-05); until it does the
    guard lives at the capability layer. The matrix tests derive their
    expectations FROM the map, so they cannot catch a widened map — these
    pins can: ``staff.manage``/``settings.manage`` are admin-exclusive, so
    every future assignment surface built on ``HasStaffManage`` /
    ``HasSettingsManage`` is admin-only by construction."""

    def test_sensitive_capabilities_are_admin_exclusive_in_the_map(self):
        self.assertEqual(CAPABILITY_ROLES["staff.manage"], frozenset({ROLE_ADMIN}))
        self.assertEqual(CAPABILITY_ROLES["settings.manage"], frozenset({ROLE_ADMIN}))

    def test_role_assignment_capability_denies_every_non_admin_role(self):
        groups = sync_role_groups()
        factory = APIRequestFactory()
        for role in STAFF_ROLES:
            user = User.objects.create_user(username=f"guard-{role}")
            user.groups.add(groups[role])
            request = Request(factory.generic("POST", "/api/staff/roles/"))
            request.user = user
            with self.subTest(role=role):
                for permission_class in (HasStaffManage, HasSettingsManage):
                    if role == ROLE_ADMIN:
                        self.assertTrue(
                            permission_class().has_permission(request, None)
                        )
                    else:
                        with self.assertRaises(PermissionDenied):
                            permission_class().has_permission(request, None)

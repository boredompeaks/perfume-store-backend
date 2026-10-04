"""RBAC foundation tests (spec 6.12): staff role groups sync idempotently.

``common.roles`` is the source of truth for the seven staff roles (the six
operational ones plus the superadmin tier of spec 1.1 lines 142-146); the
``common`` bootstrap data migration calls ``sync_role_groups`` during
``migrate``. These tests pin that the sync is safe to re-run: exactly the
stable-named role groups exist, and a deleted group is re-created rather
than silently missing from a half-bootstrapped database.

They also pin the capability permission layer built on the roles map:
allow/deny per role, the full role->capability matrix against
``CAPABILITY_ROLES``, and that the legacy ``IsAdminUserOrReadOnly``
contract is untouched by the group-based layer.
"""

from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
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
    capability_required,
    capability_required_any,
    get_user_roles,
    user_has_capability,
)
from common.roles import (
    CAPABILITY_ROLES,
    ROLE_ADMIN,
    ROLE_CATALOGUE,
    ROLE_SUPERADMIN,
    ROLE_SUPPORT,
    STAFF_ROLES,
    sync_role_groups,
)


def _named_permission(capability):
    """Resolve the named class for a capability, pinning the naming rule."""
    name = "Has" + "".join(part.capitalize() for part in capability.split("."))
    return getattr(common.permissions, name)


class StaffRoleGroupSyncTests(TestCase):
    def test_sync_twice_leaves_exactly_one_group_per_role_with_stable_names(self):
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


class AdminChromeCapabilityDecoratorTests(TestCase):
    """SPEC-20-6b: the admin-chrome decorators demand staff membership too.

    ``capability_required`` / ``capability_required_any`` are the plain-Django
    twins of the DRF capability classes, applied to the session views in the
    admin area (``/admin/search/``, ``/admin/dashboard/``,
    ``/admin/audit-log/``). Holding a capability used to be sufficient on its
    own, so an authenticated non-staff account carrying a role group reached
    those pages — including a 200 global search that listed orders and linked
    to live change forms — while the very changelists those links pointed at
    bounced it to the login (Django's admin views are ``staff_member_required``).
    The admin area therefore requires ``is_staff`` in addition to the
    capability, mirroring ``staff_member_required``; the capability layer and
    the superuser bypass are unchanged."""

    def setUp(self):
        groups = sync_role_groups()
        # Same role group, opposite staff flag: the only variable between the
        # admitted and the refused caller is ``is_staff``.
        self.staff_capable = User.objects.create_user(
            username="chrome-staff", is_staff=True
        )
        self.staff_capable.groups.add(groups[ROLE_ADMIN])
        self.nonstaff_capable = User.objects.create_user(username="chrome-nonstaff")
        self.nonstaff_capable.groups.add(groups[ROLE_ADMIN])
        self.staff_roleless = User.objects.create_user(
            username="chrome-roleless", is_staff=True
        )
        # None leaves an unusable password: nothing here authenticates, and it
        # keeps the fixture off the slow production hasher.
        self.superuser = User.objects.create_superuser(
            "chrome-root", "chrome-root@example.com", None
        )
        self.factory = RequestFactory()

    def _call(self, decorator, *capabilities, user):
        view = decorator(*capabilities)(lambda request: HttpResponse("chrome ok"))
        request = self.factory.get("/admin/probe/")
        request.user = user
        return view(request)

    # Both decorators, so neither can regress behind the other's pins.
    def _both_decorators(self):
        return (
            ("capability_required", capability_required, ("orders.read",)),
            (
                "capability_required_any",
                lambda *capabilities: capability_required_any(tuple(capabilities)),
                ("products.read", "orders.read"),
            ),
        )

    def test_staff_capability_holder_is_admitted_unchanged(self):
        for name, decorator, capabilities in self._both_decorators():
            with self.subTest(decorator=name):
                res = self._call(decorator, *capabilities, user=self.staff_capable)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.content, b"chrome ok")

    def test_non_staff_capability_holder_is_denied(self):
        # Precondition: the capability is genuinely held, so the refusal can
        # only be the staff gate. (Unreachable through the product UI — a
        # non-staff account cannot be given roles — but the admin area must not
        # depend on that invariant holding at the database level.)
        self.assertTrue(user_has_capability(self.nonstaff_capable, "orders.read"))
        for name, decorator, capabilities in self._both_decorators():
            with self.subTest(decorator=name):
                with self.assertRaises(DjangoPermissionDenied):
                    self._call(decorator, *capabilities, user=self.nonstaff_capable)

    def test_staff_without_the_capability_is_still_denied(self):
        """The capability layer is untouched: staff membership alone opens
        nothing, which is what SPEC-17-10 closed on the dashboard."""
        for name, decorator, capabilities in self._both_decorators():
            with self.subTest(decorator=name):
                with self.assertRaises(DjangoPermissionDenied):
                    self._call(decorator, *capabilities, user=self.staff_roleless)

    def test_anonymous_callers_are_still_sent_to_the_admin_login(self):
        for name, decorator, capabilities in self._both_decorators():
            with self.subTest(decorator=name):
                res = self._call(decorator, *capabilities, user=AnonymousUser())
                self.assertEqual(res.status_code, 302)
                self.assertTrue(res["Location"].startswith("/admin/login/"))

    def test_the_superuser_bypass_is_preserved(self):
        for name, decorator, capabilities in self._both_decorators():
            with self.subTest(decorator=name):
                res = self._call(decorator, *capabilities, user=self.superuser)
                self.assertEqual(res.status_code, 200)


class PrivilegeEscalationGuardTests(TestCase):
    """Spec 6.12 line 2261 + spec 1.1 line 146: who may grant what.

    No role-assignment API surface exists yet (SPEC-6-05); until it does the
    guard lives at the capability layer. The matrix tests derive their
    expectations FROM the map, so they cannot catch a widened map — these
    pins can: the staff/settings capabilities are held by the two privilege
    TIERS and by no operational role, so every future assignment surface
    built on ``HasStaffManage`` / ``HasSettingsManage`` is tier-only by
    construction, and ``platform.configure`` — the one capability ``admin``
    deliberately lacks — is what separates the top tier from Admin."""

    PRIVILEGED_TIERS = frozenset({ROLE_ADMIN, ROLE_SUPERADMIN})

    def test_sensitive_capabilities_are_held_by_the_two_tiers_only(self):
        for capability in ("staff.manage", "settings.manage"):
            with self.subTest(capability=capability):
                self.assertEqual(CAPABILITY_ROLES[capability], self.PRIVILEGED_TIERS)
                # The operational five gain nothing from the tier above them.
                self.assertEqual(
                    CAPABILITY_ROLES[capability] - self.PRIVILEGED_TIERS,
                    frozenset(),
                )

    def test_platform_configuration_is_the_top_tier_only_capability(self):
        self.assertEqual(
            CAPABILITY_ROLES["platform.configure"], frozenset({ROLE_SUPERADMIN})
        )

    def test_role_assignment_capability_denies_every_role_below_the_tiers(self):
        groups = sync_role_groups()
        factory = APIRequestFactory()
        for role in STAFF_ROLES:
            user = User.objects.create_user(username=f"guard-{role}")
            user.groups.add(groups[role])
            request = Request(factory.generic("POST", "/api/staff/roles/"))
            request.user = user
            with self.subTest(role=role):
                for permission_class in (HasStaffManage, HasSettingsManage):
                    if role in self.PRIVILEGED_TIERS:
                        self.assertTrue(
                            permission_class().has_permission(request, None)
                        )
                    else:
                        with self.assertRaises(PermissionDenied):
                            permission_class().has_permission(request, None)

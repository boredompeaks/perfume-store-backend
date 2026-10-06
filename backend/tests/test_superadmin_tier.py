"""SPEC-1-B03: the superadmin tier (spec 1.1, lines 133-150).

Spec 1.1 names seven staff roles and the last two are tiers:

- line 137 — Admin: "Manage users, roles, settings and operational access";
- line 146 — Superadmin: "Manage high-privilege settings, access and
  platform configuration";
- line 150 — "Admin is not one giant permission", so a tier ABOVE admin is
  not the same permission: the top tier exists precisely to hold powers
  ``admin`` must not have.

Two contracts are pinned here, both by behaviour:

1. the tier is real, not a label. ``platform.configure`` is granted to the
   superadmin role only and to no other role in the map, while the two
   capabilities the spec gives both tiers (``staff.manage`` = access,
   ``settings.manage`` = high-privilege settings) are shared. The top tier
   holds NO operational capability — least privilege, and the map stays
   deny-by-default for it exactly as for everyone else.
2. an Admin manages staff roles but can never mint or unmint its own
   superior: granting the ``superadmin`` role group needs
   ``platform.configure``, which the map never grants ``admin``. Django's
   ``is_superuser`` bypass keeps working exactly as before.

The module also pins the line-110 fix: the inventory/fulfilment operator
("Manage stock, packing, shipping and returns") could not pack or ship at
all, because ``orders.fulfill`` sat on support+admin alone. It now holds
that capability — and nothing else beyond it.
"""

import importlib
from decimal import Decimal

from django.apps import apps
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase, tag
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from common.admin import CONFIRM_FIELD, CONFIRMATION_YES, RoleAwareModelAdmin
from common.permissions import (
    CapabilityPermission,
    HasPlatformConfigure,
    HasStaffManage,
    get_user_roles,
    is_privileged,
    user_has_capability,
    user_may_assign_role,
)
from common.roles import (
    CAPABILITY_ROLES,
    ROLE_ADMIN,
    ROLE_GRANT_CAPABILITY,
    ROLE_INVENTORY,
    ROLE_MARKETING,
    ROLE_SUPERADMIN,
    ROLE_SUPPORT,
    STAFF_ROLES,
    sync_role_groups,
)
from common.testing import ApiTestCase
from orders.models import Order, OrderItem, OrderStatusEvent
from orders.state import ADMIN_FULFILMENT_NEXT, ALLOWED_TRANSITIONS
from orders.views import _may_fulfil

from .test_staff_roles_admin import user_change_post

TEST_PASSWORD = "S3cure-Passphrase!"

# The capabilities the spec's superadmin row grants (line 146): "high-privilege
# settings, access and platform configuration". Nothing operational.
TOP_TIER_CAPABILITIES = frozenset(
    {"platform.configure", "settings.manage", "staff.manage"}
)

# The capabilities the inventory/fulfilment operator holds after SPEC-1-B03:
# stock (inventory.*), the catalogue read it needs to pick items, and
# packing/shipping (orders.fulfill) per line 110.
INVENTORY_CAPABILITIES = frozenset(
    {"products.read", "inventory.read", "inventory.adjust", "orders.fulfill"}
)


def make_role_user(role, username):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def request_for(user):
    request = RequestFactory().post("/admin/")
    request.user = user
    return request


def held_capabilities(user):
    """Every capability identifier ``user``'s roles grant."""
    return frozenset(
        capability
        for capability in CAPABILITY_ROLES
        if user_has_capability(user, capability)
    )


class SuperadminTierDeclarationTests(TestCase):
    """The tier is declared, ordered above admin, and synced like the rest."""

    def test_superadmin_is_declared_and_ordered_above_admin(self):
        self.assertIn(ROLE_SUPERADMIN, STAFF_ROLES)
        self.assertLess(
            STAFF_ROLES.index(ROLE_ADMIN),
            STAFF_ROLES.index(ROLE_SUPERADMIN),
        )
        self.assertIs(STAFF_ROLES[-1], ROLE_SUPERADMIN)

    def test_the_six_operational_roles_keep_their_place_below_it(self):
        self.assertEqual(
            STAFF_ROLES[:-1],
            (
                ROLE_SUPPORT,
                "catalogue",
                ROLE_INVENTORY,
                "marketing",
                "finance",
                ROLE_ADMIN,
            ),
        )

    def test_every_capability_names_only_roles_that_exist(self):
        for capability, roles in CAPABILITY_ROLES.items():
            with self.subTest(capability=capability):
                self.assertLessEqual(roles, set(STAFF_ROLES))

    def test_the_group_exists_on_a_bootstrapped_database(self):
        # The migrations ran to build this test database: the group is there
        # without any application code running sync_role_groups().
        self.assertTrue(Group.objects.filter(name=ROLE_SUPERADMIN).exists())
        self.assertEqual(
            set(Group.objects.values_list("name", flat=True)), set(STAFF_ROLES)
        )

    def test_sync_recreates_a_deleted_superadmin_group(self):
        Group.objects.filter(name=ROLE_SUPERADMIN).delete()

        groups = sync_role_groups()

        self.assertTrue(Group.objects.filter(name=ROLE_SUPERADMIN).exists())
        self.assertEqual(groups[ROLE_SUPERADMIN].name, ROLE_SUPERADMIN)

    def test_the_rebootstrap_migration_adds_the_group_to_a_populated_database(self):
        # Existing deployments already ran 0001, so adding a role to the map
        # cannot reach their database: this migration is what puts the new
        # group there, and it must leave the pre-existing groups alone.
        Group.objects.filter(name=ROLE_SUPERADMIN).delete()
        survivor = Group.objects.get(name=ROLE_ADMIN)
        user = make_role_user(ROLE_ADMIN, "survivor")

        self.rebootstrap_migration().sync_staff_role_groups(None, None)

        self.assertTrue(Group.objects.filter(name=ROLE_SUPERADMIN).exists())
        self.assertTrue(Group.objects.filter(pk=survivor.pk).exists())
        self.assertTrue(user.groups.filter(pk=survivor.pk).exists())

    def test_the_rebootstrap_migration_reverse_removes_only_the_added_group(self):
        # 0001's reverse deletes every role group; repeating that here would
        # drop live role assignments that predate this migration.
        module = self.rebootstrap_migration()
        keeper = make_role_user(ROLE_ADMIN, "keeper")

        module.remove_superadmin_role_group(apps, None)

        self.assertFalse(Group.objects.filter(name=ROLE_SUPERADMIN).exists())
        self.assertEqual(
            set(Group.objects.values_list("name", flat=True)),
            set(STAFF_ROLES) - {ROLE_SUPERADMIN},
        )
        self.assertTrue(keeper.groups.filter(name=ROLE_ADMIN).exists())

    @staticmethod
    def rebootstrap_migration():
        return importlib.import_module(
            "common.migrations.0006_sync_staff_role_groups_superadmin"
        )


class SuperadminCapabilityTests(TestCase):
    """The tier's authority, and the map's deny-by-default, per spec line 146."""

    def setUp(self):
        groups = sync_role_groups()
        self.superadmin = User.objects.create_user(username="root-role")
        self.superadmin.groups.add(groups[ROLE_SUPERADMIN])
        self.admin = User.objects.create_user(username="chief-role")
        self.admin.groups.add(groups[ROLE_ADMIN])
        self.support = User.objects.create_user(username="supp-role")
        self.support.groups.add(groups[ROLE_SUPPORT])
        self.customer = User.objects.create_user(username="plain-customer")

    def _request(self, user):
        request = Request(APIRequestFactory().generic("GET", "/api/staff/"))
        request.user = user
        return request

    def test_the_top_tier_holds_exactly_the_capabilities_the_spec_names(self):
        # "high-privilege settings, access and platform configuration" — and
        # nothing operational, because the spec grants the top tier no
        # catalogue, fulfilment, refund or reporting authority.
        self.assertEqual(held_capabilities(self.superadmin), TOP_TIER_CAPABILITIES)

    def test_platform_configuration_is_the_superadmin_only_capability(self):
        self.assertTrue(user_has_capability(self.superadmin, "platform.configure"))
        for user in (self.admin, self.support, self.customer):
            with self.subTest(username=user.username):
                self.assertFalse(user_has_capability(user, "platform.configure"))

    def test_an_admin_cannot_configure_the_platform_through_the_permission_class(self):
        # The API-side twin of the map: an Admin is refused the capability
        # with the same uniform 403 every other capability denial raises.
        with self.assertRaises(DRFPermissionDenied):
            HasPlatformConfigure().has_permission(self._request(self.admin), None)
        self.assertTrue(
            HasPlatformConfigure().has_permission(self._request(self.superadmin), None)
        )

    def test_the_named_class_is_pinned_to_the_new_capability(self):
        self.assertTrue(issubclass(HasPlatformConfigure, CapabilityPermission))
        self.assertEqual(HasPlatformConfigure.capability, "platform.configure")
        self.assertEqual(HasPlatformConfigure.__name__, "HasPlatformConfigure")

    def test_the_top_tier_manages_access_and_high_privilege_settings(self):
        # Spec line 146 gives the tier "access" and "high-privilege
        # settings" — the same two surfaces spec line 137 gives Admin, so
        # both tiers hold them and the tier is strictly above Admin on both.
        for capability in ("staff.manage", "settings.manage"):
            with self.subTest(capability=capability):
                self.assertTrue(user_has_capability(self.superadmin, capability))
                self.assertTrue(user_has_capability(self.admin, capability))

    def test_no_operational_role_reaches_the_privileged_capabilities(self):
        # Spec line 150: admin is not one giant permission, and it is not
        # spread down the tiers either.
        for capability in TOP_TIER_CAPABILITIES:
            with self.subTest(capability=capability):
                self.assertEqual(
                    CAPABILITY_ROLES[capability] - {ROLE_ADMIN, ROLE_SUPERADMIN},
                    frozenset(),
                )

    def test_deny_by_default_holds_for_the_new_tier(self):
        # An identifier nobody wired must not open access, least of all for
        # the highest role in the map.
        self.assertFalse(user_has_capability(self.superadmin, "platform.selfdestruct"))
        self.assertFalse(user_has_capability(self.superadmin, "orders.demolish"))

    def test_anonymous_and_role_less_callers_are_denied_the_tier(self):
        anonymous = AnonymousUser()
        for user in (anonymous, self.customer):
            with self.subTest(username=getattr(user, "username", "anonymous")):
                for capability in TOP_TIER_CAPABILITIES:
                    self.assertFalse(user_has_capability(user, capability))
                with self.assertRaises(DRFPermissionDenied):
                    HasPlatformConfigure().has_permission(self._request(user), None)


@tag("e2e")
class SuperadminEscalationGuardTests(ApiTestCase):
    """An Admin manages staff roles; it can never mint or unmint its superior."""

    def setUp(self):
        self.admin_user = make_role_user(ROLE_ADMIN, "chief")
        self.superadmin_user = make_role_user(ROLE_SUPERADMIN, "tier-root")
        self.target = User.objects.create_user(
            username="temp",
            email="temp@example.com",
            password=TEST_PASSWORD,
            is_staff=True,
        )

    # helpers -----------------------------------------------------------------
    def change_url(self, target=None):
        target = target or self.target
        return f"/admin/auth/user/{target.id}/change/"

    def post_roles(self, roles, *, target=None, confirm=False, **extra):
        target = target or self.target
        payload = user_change_post(
            target,
            staff_roles=[str(Group.objects.get(name=role).pk) for role in roles],
            **extra,
        )
        if confirm:
            payload[CONFIRM_FIELD] = CONFIRMATION_YES
        return self.client.post(self.change_url(target), payload)

    def assert_no_superadmin_grant(self, target=None):
        target = target or self.target
        target.refresh_from_db()
        self.assertFalse(
            target.groups.filter(name=ROLE_SUPERADMIN).exists(),
            "the top tier must never reach an account without platform.configure",
        )
        self.assertFalse(
            LogEntry.objects.filter(change_message__icontains=ROLE_SUPERADMIN).exists(),
            "a refused escalation must not write a role audit entry",
        )

    # the field ---------------------------------------------------------------
    def test_the_roles_field_never_offers_the_top_tier_to_an_admin(self):
        self.client.force_login(self.admin_user)

        res = self.client.get(self.change_url())

        self.assertEqual(res.status_code, 200)
        offered = set(
            res.context["adminform"]
            .form.fields["staff_roles"]
            .queryset.values_list("name", flat=True)
        )
        self.assertNotIn(ROLE_SUPERADMIN, offered)
        self.assertEqual(offered, set(STAFF_ROLES) - {ROLE_SUPERADMIN})

    def test_the_roles_field_offers_every_role_to_the_top_tier(self):
        self.client.force_login(self.superadmin_user)

        res = self.client.get(self.change_url())

        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            set(
                res.context["adminform"]
                .form.fields["staff_roles"]
                .queryset.values_list("name", flat=True)
            ),
            set(STAFF_ROLES),
        )

    # the refusals ------------------------------------------------------------
    def test_an_admin_cannot_grant_the_superadmin_role(self):
        self.client.force_login(self.admin_user)

        res = self.post_roles([ROLE_SUPERADMIN])

        self.assertEqual(res.status_code, 403)
        self.assert_no_superadmin_grant()

    def test_an_admin_cannot_grant_itself_the_superadmin_role(self):
        self.client.force_login(self.admin_user)

        res = self.post_roles([ROLE_ADMIN, ROLE_SUPERADMIN], target=self.admin_user)

        self.assertEqual(res.status_code, 403)
        self.assert_no_superadmin_grant(self.admin_user)
        self.assertTrue(self.admin_user.groups.filter(name=ROLE_ADMIN).exists())

    def test_an_admin_cannot_strip_the_top_tier_from_an_account(self):
        # The escalation guard is symmetric: unminting the tier is as much a
        # high-privilege access change as minting it, and an Admin may not do
        # either — so the top tier's accounts are off-limits to them.
        self.target.groups.add(Group.objects.get(name=ROLE_SUPERADMIN))
        self.client.force_login(self.admin_user)

        res = self.post_roles([])

        self.assertEqual(res.status_code, 403)
        self.target.refresh_from_db()
        self.assertTrue(self.target.groups.filter(name=ROLE_SUPERADMIN).exists())

    def test_a_direct_role_sync_is_refused_for_an_admin(self):
        # Defence in depth, off the view flow (the same shape as the
        # staff.manage guard): even a caller that reaches the sync with a
        # cleaned_data the form would never produce is refused before any
        # group membership is written.
        user_admin = admin.site._registry[User]

        with self.assertRaises(PermissionDenied):
            user_admin._apply_staff_roles(
                request_for(self.admin_user),
                self.target,
                type(
                    "FakeForm",
                    (),
                    {
                        "cleaned_data": {
                            "staff_roles": [Group.objects.get(name=ROLE_SUPERADMIN)]
                        }
                    },
                )(),
            )

        self.assert_no_superadmin_grant()

    # no regression -----------------------------------------------------------
    def test_an_admin_may_still_reassign_the_operational_roles(self):
        self.target.groups.add(Group.objects.get(name=ROLE_SUPPORT))
        self.client.force_login(self.admin_user)

        first = self.post_roles([ROLE_MARKETING])
        self.assertEqual(first.status_code, 200)
        self.assert_no_superadmin_grant()
        committed = self.post_roles([ROLE_MARKETING], confirm=True)

        self.assertEqual(committed.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(
            set(self.target.groups.values_list("name", flat=True)), {ROLE_MARKETING}
        )
        self.assertTrue(
            LogEntry.objects.filter(change_message='Removed role "support".').exists()
        )

    # the tier and the bypass -------------------------------------------------
    def test_the_top_tier_grants_the_superadmin_role_through_the_confirm_step(self):
        self.client.force_login(self.superadmin_user)

        first = self.post_roles([ROLE_SUPERADMIN])
        self.assertEqual(first.status_code, 200)
        self.assert_no_superadmin_grant()

        committed = self.post_roles([ROLE_SUPERADMIN], confirm=True)

        self.assertEqual(committed.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.groups.filter(name=ROLE_SUPERADMIN).exists())
        self.assertTrue(
            LogEntry.objects.filter(
                change_message=f'Added role "{ROLE_SUPERADMIN}".'
            ).exists()
        )

    def test_the_django_superuser_bypass_still_grants_the_top_tier(self):
        root = User.objects.create_superuser("root", "root@example.com", None)
        self.client.force_login(root)
        # A Django superuser editor also OWNS the privilege flags (they are
        # not read-only for them), so the payload states is_staff explicitly.
        flags = {"is_staff": "on"}

        first = self.post_roles([ROLE_SUPERADMIN], **flags)
        self.assertEqual(first.status_code, 200)
        committed = self.post_roles([ROLE_SUPERADMIN], confirm=True, **flags)

        self.assertEqual(committed.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.groups.filter(name=ROLE_SUPERADMIN).exists())

    # the predicate itself ----------------------------------------------------
    def test_the_top_tier_is_the_only_role_whose_grant_needs_platform_configure(self):
        self.assertEqual(ROLE_GRANT_CAPABILITY, {ROLE_SUPERADMIN: "platform.configure"})
        for role in STAFF_ROLES:
            with self.subTest(role=role):
                user = make_role_user(role, f"assign-{role}")
                # Only the tier that already holds platform.configure can
                # grant it — which is why the grant can never climb.
                self.assertEqual(
                    user_may_assign_role(user, ROLE_SUPERADMIN),
                    role == ROLE_SUPERADMIN,
                )
                # Every other role keeps the plain staff.manage contract, so
                # the guard adds no second gate on the operational six.
                self.assertTrue(user_may_assign_role(user, ROLE_ADMIN))

    def test_the_guard_never_opens_a_role_to_an_unauthenticated_caller(self):
        self.assertFalse(user_may_assign_role(None, ROLE_SUPERADMIN))
        self.assertFalse(user_may_assign_role(AnonymousUser(), ROLE_SUPERADMIN))

    def test_the_django_superuser_flag_is_the_only_bypass_on_the_guard(self):
        superuser = User.objects.create_superuser("root", "root@example.com", None)
        self.assertTrue(user_may_assign_role(superuser, ROLE_SUPERADMIN))
        # The flag is not itself a role: it grants the grant, nothing else.
        self.assertEqual(get_user_roles(superuser), frozenset())


class InventoryFulfilmentAuthorityTests(ApiTestCase):
    """Spec line 110: "Manage stock, packing, shipping and returns".

    The operator could not pack or ship at all — ``orders.fulfill`` sat on
    support+admin alone. The fix grants that one capability and nothing
    else: the spec names packing and shipping for this role, not order
    visibility, cancellation, refunds or customer records.
    """

    def setUp(self):
        self.operator = make_role_user(ROLE_INVENTORY, "packer")
        self.support = make_role_user(ROLE_SUPPORT, "supp")
        self.buyer = self.make_user("buyer")

    def _order(self, username="buyer", **overrides):
        fields = dict(
            user=self.buyer,
            full_name="Seam Buyer",
            phone="9999999999",
            address="1 Test Lane",
            city="Indore",
            state="MP",
            pincode="452001",
            total_amount=Decimal("750.00"),
        )
        fields.update(overrides)
        return Order.objects.create(**fields)

    def _walk_ready_order(self, name):
        order = self._order()
        product = self.make_product(name=name, price="750.00", stock=5)
        OrderItem.objects.create(
            order=order,
            product=product,
            product_name=product.name,
            price=product.price,
            quantity=1,
            subtotal=product.price,
        )
        Order.objects.filter(pk=order.pk).update(payment_status="captured")
        return order

    def test_the_operator_can_pack_and_ship(self):
        order = self._walk_ready_order("Walk Rose")
        self.client.force_authenticate(self.operator)

        for expected in ("confirmed", "shipped", "delivered"):
            with self.subTest(status=expected):
                res = self.client.post(f"/api/admin/orders/{order.id}/fulfill/")
                self.assertEqual(res.status_code, 200, res.data)
                self.assertEqual(res.data["status"], expected)
        order.refresh_from_db()
        self.assertEqual(order.status, "delivered")

    def test_the_operator_holds_packing_and_shipping_and_nothing_more(self):
        self.assertEqual(held_capabilities(self.operator), INVENTORY_CAPABILITIES)

    def test_packing_does_not_grant_the_other_order_powers(self):
        order = self._order()
        self.client.force_authenticate(self.operator)

        for path in ("cancel", "refund"):
            with self.subTest(path=path):
                res = self.client.post(f"/api/admin/orders/{order.id}/{path}/")
                self.assertEqual(res.status_code, 403)
                self.assertEqual(res.data["code"], "permission_denied")
        self.assertEqual(self.client.get("/api/admin/orders/").status_code, 403)

    def test_the_operator_keeps_its_stock_authority(self):
        self.assertTrue(user_has_capability(self.operator, "inventory.adjust"))
        self.assertTrue(user_has_capability(self.operator, "inventory.read"))

    def test_packing_does_not_grant_staff_or_platform_management(self):
        request = Request(APIRequestFactory().generic("POST", "/api/staff/"))
        request.user = self.operator
        with self.assertRaises(DRFPermissionDenied):
            HasStaffManage().has_permission(request, None)
        with self.assertRaises(DRFPermissionDenied):
            HasPlatformConfigure().has_permission(request, None)

    def test_support_keeps_fulfilment_authority(self):
        # No regression from the line-110 fix: support's "permitted order
        # issues" (line 92) still walks the fulfilment path.
        order = self._order()
        self.client.force_authenticate(self.support)

        res = self.client.post(f"/api/admin/orders/{order.id}/fulfill/")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], "confirmed")

    def test_the_json_seam_is_scoped_to_the_queue_for_a_fulfil_only_role(self):
        # [R-1-B03] orders.fulfill is not order visibility (spec 1.1 line
        # 110), so the operator may advance the orders its queue lists and no
        # others. The out-of-queue answer is the SAME uniform 404 an unknown
        # id gets: it must not confirm the order exists, nor name its status,
        # to a role that was deliberately denied order visibility.
        order = self._walk_ready_order("Scoped Rose")
        settled = self._order(status="delivered", total_amount="250.00")
        self.client.force_authenticate(self.operator)

        settled_before = settled.status
        res = self.client.post(f"/api/admin/orders/{settled.id}/fulfill/")

        self.assertEqual(res.status_code, 404, res.data)
        settled.refresh_from_db()
        self.assertEqual(settled.status, settled_before)
        # ...and the queued order is still advanceable, so the scope narrows
        # rather than closing the seam.
        queued = self.client.post(f"/api/admin/orders/{order.id}/fulfill/")
        self.assertEqual(queued.status_code, 200, queued.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")

    def test_an_orders_read_holder_keeps_the_unscoped_json_seam(self):
        # Support holds orders.read (order visibility) AND orders.fulfill, so
        # the queue scope does not apply to it: a settled order still answers
        # the machine's own 409 with its allowed set, exactly as before.
        settled = self._order(status="delivered", total_amount="250.00")
        self.client.force_authenticate(self.support)

        res = self.client.post(f"/api/admin/orders/{settled.id}/fulfill/")

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data["details"]["allowed"], "")
        self.assertEqual(res.data["code"], "conflict")

    def test_the_superuser_flag_is_not_narrowed_by_the_queue_scope(self):
        # Django's own bypass is preserved on the scoped seam for the same
        # reason it is preserved on every other surface: the trust anchor must
        # not be narrowed by a least-privilege rule meant for staff roles.
        root = User.objects.create_superuser("queue-root", "root@example.com", None)
        self.assertTrue(_may_fulfil(root, self._order(status="delivered")))
        # ...and it grants nothing on its own to an anonymous caller: no
        # capability, no bypass.
        self.assertFalse(_may_fulfil(AnonymousUser(), self._order()))


@tag("e2e")
class FulfilmentQueueSurfaceTests(ApiTestCase):
    """[R-1-B03] The operator-reachable listing the packing capability needs.

    Holding ``orders.fulfill`` gives the changelist a 200 (Django gates it on
    the model's change permission) while the admin index hid the module, so
    the operator could advance an order only by guessing its pk and had no way
    to discover which orders awaited packing. ``OrderAdmin``'s scoped viewer
    is the fix: a queue listing, discoverable from the index, narrowed to
    packing — and ``orders.read`` itself stays where spec 1.1 puts it.
    """

    CHANGELIST = "/admin/orders/order/"

    def setUp(self):
        self.operator = make_role_user(ROLE_INVENTORY, "queue-packer")
        self.support = make_role_user(ROLE_SUPPORT, "queue-support")
        self.buyer = self.make_user("queue-buyer")
        self.product = self.make_product(name="Queue Rose", price="750.00", stock=9)
        self.queued = self._order("PACK-0001", status="pending", total="750.00")
        OrderItem.objects.create(
            order=self.queued,
            product=self.product,
            product_name=self.product.name,
            price=self.product.price,
            quantity=2,
            subtotal=Decimal("1500.00"),
        )
        Order.objects.filter(pk=self.queued.pk).update(payment_status="captured")
        # Not this operator's work: delivered (no fulfilment edge) and
        # cancelled (same), both out of the queue.
        self.settled = self._order("PACK-0002", status="delivered", total="250.00")
        self.cancelled = self._order("PACK-0003", status="cancelled", total="80.00")

    def _order(self, order_number, status, total):
        return Order.objects.create(
            user=self.buyer,
            order_number=order_number,
            full_name="Queue Buyer",
            phone="9876500011",
            address="1 Queue Lane",
            city="Indore",
            state="MP",
            pincode="452001",
            status=status,
            total_amount=Decimal(total),
        )

    def order_admin(self):
        return admin.site._registry[Order]

    def test_the_index_offers_the_operator_its_queue(self):
        # Discoverability is the whole defect: the module was hidden, so the
        # surface existed but nothing pointed at it.
        self.client.force_login(self.operator)
        res = self.client.get("/admin/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.CHANGELIST)
        self.assertTrue(
            self.order_admin().has_module_permission(request_for(self.operator))
        )

    def test_the_index_hides_the_queue_from_a_role_that_may_neither_read_nor_pack(self):
        # Deny-by-default in the other direction: marketing holds neither
        # capability, so the module stays off their index.
        self.client.force_login(make_role_user(ROLE_MARKETING, "queue-marketing"))
        res = self.client.get("/admin/")
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, self.CHANGELIST)

    def test_the_queue_lists_the_orders_awaiting_a_pack_and_only_those(self):
        self.client.force_login(self.operator)
        res = self.client.get(self.CHANGELIST)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.queued.order_number)
        self.assertNotContains(res, self.settled.order_number)
        self.assertNotContains(res, self.cancelled.order_number)

    def test_the_queue_offers_the_three_fulfilment_actions_and_no_money_ones(self):
        self.client.force_login(self.operator)
        res = self.client.get(self.CHANGELIST)
        self.assertEqual(res.status_code, 200)
        for action in ("mark_confirmed", "mark_shipped", "mark_delivered"):
            self.assertContains(res, action)
        # export_csv rides orders.read, which this role deliberately lacks.
        self.assertNotContains(res, "export_csv")

    def test_the_queue_withholds_the_money_and_the_customer_record(self):
        self.client.force_login(self.operator)
        res = self.client.get(self.CHANGELIST)
        self.assertEqual(res.status_code, 200)
        # The money columns and the customer's own details are orders.read's
        # (and customers.read's), not a packer's.
        self.assertNotContains(res, str(self.queued.total_amount))
        self.assertNotContains(res, self.queued.full_name)
        self.assertNotContains(res, self.queued.phone)
        self.assertNotContains(res, self.buyer.email)

    def test_the_queue_is_searched_by_reference_not_by_customer(self):
        self.client.force_login(self.operator)
        by_reference = self.client.get(self.CHANGELIST, {"q": self.queued.order_number})
        self.assertEqual(by_reference.status_code, 200)
        self.assertContains(by_reference, self.queued.order_number)
        # A customer record is not a lookup key for this role: searching by
        # name or phone finds nothing, because get_search_fields is narrowed.
        for term in (self.queued.full_name, self.queued.phone, self.buyer.email):
            with self.subTest(term=term):
                res = self.client.get(self.CHANGELIST, {"q": term})
                self.assertEqual(res.status_code, 200)
                self.assertNotContains(res, self.queued.order_number)

    def test_the_operator_advances_a_queued_order_from_the_queue(self):
        self.client.force_login(self.operator)
        res = self.client.post(
            self.CHANGELIST,
            {"action": "mark_confirmed", "_selected_action": [str(self.queued.pk)]},
        )
        self.assertEqual(res.status_code, 302)
        self.queued.refresh_from_db()
        self.assertEqual(self.queued.status, "confirmed")
        entry = LogEntry.objects.get(object_id=str(self.queued.pk))
        self.assertEqual(entry.user.username, "queue-packer")

    def test_a_forged_selection_outside_the_queue_is_inert(self):
        # The actions read their queryset from this admin's get_queryset, so
        # a hand-posted pk cannot reach an order the queue does not list.
        self.client.force_login(self.operator)
        res = self.client.post(
            self.CHANGELIST,
            {"action": "mark_confirmed", "_selected_action": [str(self.settled.pk)]},
        )
        self.assertEqual(res.status_code, 302)
        self.settled.refresh_from_db()
        self.assertEqual(self.settled.status, "delivered")
        self.assertFalse(
            LogEntry.objects.filter(
                object_id=str(self.settled.pk), user=self.operator
            ).exists()
        )

    def test_the_change_form_shows_the_packing_list_and_no_money(self):
        self.client.force_login(self.operator)
        res = self.client.get(f"{self.CHANGELIST}{self.queued.pk}/change/")
        self.assertEqual(res.status_code, 200)
        # What to pack: the item snapshot, without its prices.
        self.assertContains(res, self.product.name)
        self.assertNotContains(res, "1500.00")
        self.assertNotContains(res, "750.00")
        # The customer record and the money block stay out.
        self.assertNotContains(res, self.queued.full_name)
        self.assertNotContains(res, self.queued.phone)
        self.assertNotContains(res, self.buyer.email)

    def test_an_order_outside_the_queue_has_no_change_page_for_this_role(self):
        self.client.force_login(self.operator)
        for order in (self.settled, self.cancelled):
            with self.subTest(status=order.status):
                res = self.client.get(f"{self.CHANGELIST}{order.pk}/change/")
                self.assertEqual(res.status_code, 302)

    def test_the_queue_offers_no_saved_view_bar_it_could_never_save(self):
        # The bar's own endpoints gate on has_view_permission, which this role
        # does not hold, so rendering it would offer a form that only 404s.
        self.client.force_login(self.operator)
        res = self.client.get(self.CHANGELIST)
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, 'id="saved-filters"')

    def test_the_scoped_viewer_is_exactly_fulfil_without_orders_read(self):
        model_admin = self.order_admin()
        for user, expected in (
            (self.operator, True),
            (self.support, False),  # holds orders.read as well
            (
                make_role_user(ROLE_ADMIN, "queue-chief"),
                False,
            ),
        ):
            with self.subTest(username=user.username):
                request = RequestFactory().get(self.CHANGELIST)
                request.user = user
                self.assertIs(model_admin.is_scoped_viewer(request), expected)

    def test_the_queue_declaration_reads_from_the_machine_and_the_map(self):
        # The queue can only ever be the statuses the fulfilment walk can
        # advance (single source: orders.state), and the second door is
        # declared as a capability that exists in the roles map — an unknown
        # identifier would deny, which is why the drift pin below matters.
        model_admin = self.order_admin()
        self.assertEqual(
            tuple(model_admin.scoped_queryset), tuple(ADMIN_FULFILMENT_NEXT)
        )
        self.assertIn(model_admin.scoped_view_capability, set(CAPABILITY_ROLES))
        # The withheld columns really are the ones orders.read holds.
        full = set(model_admin.list_display)
        withheld = {
            "user",
            "full_name",
            "total_amount",
            "discount_amount",
            "currency",
            "coupon",
            "payment_ref",
        }
        self.assertLessEqual(withheld, full)
        self.assertFalse(withheld & set(model_admin.scoped_list_display))
        # The customer record is withheld from search as well as from the grid.
        self.assertFalse(
            {"full_name", "phone", "user__email", "user__username"}
            & set(model_admin.scoped_search_fields)
        )
        self.assertTrue(
            {"full_name", "phone", "user__email"} <= set(model_admin.search_fields)
        )

    def test_orders_read_holders_keep_the_full_grid_and_the_saved_bar(self):
        self.client.force_login(self.support)
        res = self.client.get(self.CHANGELIST)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, str(self.queued.total_amount))
        self.assertContains(res, self.queued.full_name)
        self.assertContains(res, "export_csv")
        # The saved-view bar is untouched for a role that may save one.
        self.assertContains(res, 'id="saved-filters"')
        self.assertContains(res, self.settled.order_number)

    def test_the_superuser_keeps_the_full_grid_on_the_orders_module(self):
        User.objects.create_superuser("queue-root", "root@example.com", TEST_PASSWORD)
        root = User.objects.get(username="queue-root")
        self.client.force_login(root)
        res = self.client.get(self.CHANGELIST)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, str(self.queued.total_amount))
        self.assertContains(res, self.settled.order_number)
        self.assertTrue(self.order_admin().has_module_permission(request_for(root)))


@tag("e2e")
class ScopedFulfilWritePathTests(ApiTestCase):
    """[R-1-B03] cycle 3: what the packing door may WRITE, on both admin paths.

    Cycle 2 narrowed this surface for reading and left the write path wide: a
    role holding exactly ``{products.read, inventory.read, inventory.adjust,
    orders.fulfill}`` could cancel a pending order through the change form AND
    through the ``list_editable`` cell (``pending -> cancelled`` is a legal
    machine edge), and could rewrite the delivery address — the one surface
    where the machine gate says nothing, because the value is not the status.

    The two admin write paths are the change form and the changelist formset.
    Django builds both through ``formfield_for_dbfield`` and
    ``get_readonly_fields``, so one declaration in the base closes both; these
    tests drive the real POSTs and then assert on the PERSISTED row, not on the
    status code, because a refusal that still moved the row would be no
    refusal at all.
    """

    CHANGELIST = "/admin/orders/order/"

    def setUp(self):
        self.operator = make_role_user(ROLE_INVENTORY, "write-packer")
        # Support holds orders.cancel (and orders.read, so it is NOT a scoped
        # viewer): the control that proves the seam refuses only the packer.
        self.support = make_role_user(ROLE_SUPPORT, "write-support")
        self.chief = make_role_user(ROLE_ADMIN, "write-chief")
        self.root = User.objects.create_superuser(
            "write-root", "root@example.com", TEST_PASSWORD
        )
        self.buyer = self.make_user("write-buyer")
        self.product = self.make_product(name="Write Rose", price="750.00", stock=9)
        self.queued = self._order("WRITE-0001")

    def _order(self, order_number):
        order = Order.objects.create(
            user=self.buyer,
            order_number=order_number,
            full_name="Write Buyer",
            phone="9876500044",
            address="1 Queue Lane",
            city="Indore",
            state="MP",
            pincode="452001",
            total_amount=Decimal("750.00"),
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name=self.product.name,
            price=self.product.price,
            quantity=1,
            subtotal=Decimal("750.00"),
        )
        # Paid, so the fulfilment preconditions are met for the queue advance.
        Order.objects.filter(pk=order.pk).update(payment_status="captured")
        return order

    # helpers -----------------------------------------------------------------
    def order_admin(self):
        return admin.site._registry[Order]

    def change_url(self):
        return f"{self.CHANGELIST}{self.queued.pk}/change/"

    def change_post(self, **fields):
        """A complete change-form POST, exactly as the grid's form renders it.

        It states the full surface's editable fields (so the same payload is an
        honest submission for a cancel holder and for the superuser) plus the
        item inline's management form, whose fields are read-only on every
        surface. A scoped viewer's extra fields are not form fields there, so
        they are ignored — which is exactly what the address test relies on.
        """
        order = self.queued
        payload = {
            "user": order.user_id,
            "full_name": order.full_name,
            "phone": order.phone,
            "address": order.address,
            "city": order.city,
            "state": order.state,
            "pincode": order.pincode,
            "status": order.status,
            "_save": "Save",
            "items-TOTAL_FORMS": "1",
            "items-INITIAL_FORMS": "1",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-id": str(order.items.get().pk),
        }
        payload.update(fields)
        return self.client.post(self.change_url(), payload)

    def changelist_post(self, status="confirmed"):
        """The ``list_editable`` formset POST, exactly as the grid renders it."""
        order = self.queued
        payload = {
            "_save": "Save",
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-id": str(order.pk),
            "form-0-order_number": order.order_number,
            "form-0-status": status,
        }
        return self.client.post(self.CHANGELIST, payload)

    def assert_row_unchanged(self):
        """The persisted row is the contract: status AND every stamp the
        refused write would have set."""
        order = self.queued
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertIsNone(order.cancelled_at)
        self.assertFalse(
            OrderStatusEvent.objects.filter(order=order).exists(),
            "a refused status write must leave no transition audit row",
        )

    # the packing work that must survive the fix ------------------------------
    def test_a_fulfil_only_role_advances_a_packing_status_on_the_change_form(self):
        self.client.force_login(self.operator)

        res = self.change_post(status="confirmed")

        self.assertEqual(res.status_code, 302)
        self.queued.refresh_from_db()
        self.assertEqual(self.queued.status, "confirmed")
        event = OrderStatusEvent.objects.get(order=self.queued)
        self.assertEqual(event.to_status, "confirmed")
        self.assertEqual(event.actor, self.operator)

    def test_a_fulfil_only_role_advances_a_packing_status_on_the_list_edit_cell(self):
        # The other surviving write path: the grid cell is how a packer walks a
        # queue, so narrowing the cancel target must not close it.
        self.client.force_login(self.operator)

        res = self.changelist_post(status="confirmed")

        self.assertEqual(res.status_code, 302)
        self.queued.refresh_from_db()
        self.assertEqual(self.queued.status, "confirmed")

    # the destructive target, both paths ---------------------------------------
    def test_a_fulfil_only_role_cannot_cancel_through_the_change_form(self):
        self.client.force_login(self.operator)

        res = self.change_post(status="cancelled")

        # Django's own field validation refuses the hand-posted value and
        # re-renders; nothing reaches save_model.
        self.assertEqual(res.status_code, 200)
        self.assertIn("status", res.context["adminform"].form.errors)
        self.assert_row_unchanged()
        self.assertFalse(
            LogEntry.objects.filter(
                object_id=str(self.queued.pk), user=self.operator
            ).exists(),
            "a refused write must leave no change-log entry",
        )

    def test_a_fulfil_only_role_cannot_cancel_through_the_list_edit_cell(self):
        self.client.force_login(self.operator)

        res = self.changelist_post(status="cancelled")

        self.assertIn(res.status_code, (200, 302))
        self.assert_row_unchanged()

    def test_the_change_form_offers_the_packer_no_cancel_option(self):
        self.client.force_login(self.operator)

        res = self.client.get(self.change_url())

        self.assertEqual(res.status_code, 200)
        offered = [
            value
            for value, _label in res.context["adminform"].form.fields["status"].choices
        ]
        self.assertNotIn("cancelled", offered)
        # ...and the queue walk is still offered in full.
        self.assertLessEqual({"confirmed", "shipped", "delivered"}, set(offered))
        self.assertNotContains(res, '<option value="cancelled">')

    def test_the_list_edit_cell_offers_the_packer_no_cancel_option(self):
        self.client.force_login(self.operator)

        res = self.client.get(self.CHANGELIST)

        self.assertEqual(res.status_code, 200)
        # The cell exists (the packing advance still lives there) but its
        # widget carries no cancel target.
        self.assertContains(res, 'name="form-0-status"')
        self.assertNotContains(res, '<option value="cancelled"')

    # the delivery address (BUG-5) --------------------------------------------
    def test_a_fulfil_only_role_cannot_rewrite_the_delivery_address(self):
        self.client.force_login(self.operator)

        res = self.change_post(
            address="1 Attacker Lane",
            city="Mars",
            state="ZZ",
            pincode="000000",
        )

        self.assertEqual(res.status_code, 302)
        self.queued.refresh_from_db()
        self.assertEqual(self.queued.address, "1 Queue Lane")
        self.assertEqual(self.queued.city, "Indore")
        self.assertEqual(self.queued.state, "MP")
        self.assertEqual(self.queued.pincode, "452001")

    def test_the_scoped_change_form_renders_no_input_for_a_withheld_field(self):
        self.client.force_login(self.operator)

        res = self.client.get(self.change_url())

        self.assertEqual(res.status_code, 200)
        for field in ("address", "city", "state", "pincode", "order_number"):
            with self.subTest(field=field):
                self.assertNotContains(res, f'name="{field}"')
        # ...while the one writable field is still an input.
        self.assertContains(res, 'name="status"')

    # no over-blocking ---------------------------------------------------------
    def test_a_cancel_holder_keeps_its_change_form_authority(self):
        # Support holds orders.cancel and orders.read, so it is not a scoped
        # viewer: the pending -> cancelled edge it may commit on the sanctioned
        # API path must stay committable here too.
        self.client.force_login(self.support)

        res = self.change_post(status="cancelled")

        self.assertEqual(res.status_code, 302)
        self.queued.refresh_from_db()
        self.assertEqual(self.queued.status, "cancelled")
        self.assertIsNotNone(self.queued.cancelled_at)

    def test_the_cancel_holder_is_still_offered_the_cancel_option(self):
        self.client.force_login(self.support)

        res = self.client.get(self.change_url())

        offered = [
            value
            for value, _label in res.context["adminform"].form.fields["status"].choices
        ]
        self.assertIn("cancelled", offered)

    def test_the_admin_role_keeps_its_full_change_form_and_cell(self):
        self.client.force_login(self.chief)

        form = self.client.get(self.change_url())
        offered = [
            value
            for value, _label in form.context["adminform"].form.fields["status"].choices
        ]
        self.assertIn("cancelled", offered)
        self.assertContains(form, 'name="address"')
        grid = self.client.get(self.CHANGELIST)
        self.assertContains(grid, 'name="form-0-status"')
        self.assertContains(grid, '<option value="cancelled"')
        committed = self.changelist_post(status="cancelled")
        self.assertEqual(committed.status_code, 302)
        self.queued.refresh_from_db()
        self.assertEqual(self.queued.status, "cancelled")

    def test_the_superuser_bypass_is_untouched_by_the_scoped_seam(self):
        self.client.force_login(self.root)

        form = self.client.get(self.change_url())
        offered = [
            value
            for value, _label in form.context["adminform"].form.fields["status"].choices
        ]
        self.assertIn("cancelled", offered)
        self.assertContains(form, 'name="address"')
        committed = self.change_post(status="cancelled")
        self.assertEqual(committed.status_code, 302)
        self.queued.refresh_from_db()
        self.assertEqual(self.queued.status, "cancelled")
        self.assertIsNotNone(self.queued.cancelled_at)

    # the declaration itself ---------------------------------------------------
    def test_the_write_declaration_names_the_packing_field_and_the_cancel_capability(
        self,
    ):
        model_admin = self.order_admin()
        self.assertEqual(model_admin.scoped_writable_fields, frozenset({"status"}))
        self.assertEqual(
            model_admin.scoped_value_capabilities,
            {"status": {"cancelled": "orders.cancel"}},
        )
        # One authority, not three that can drift: the change form, the cell and
        # the bulk action all answer on orders.cancel.
        self.assertEqual(
            model_admin.scoped_value_capabilities["status"]["cancelled"],
            model_admin.action_capabilities["cancel_pending"],
        )
        # Least privilege: the refusal costs the operator nothing else, and the
        # operator still holds no orders.read (so no export_csv).
        self.assertFalse(user_has_capability(self.operator, "orders.cancel"))
        self.assertFalse(user_has_capability(self.operator, "orders.read"))
        self.assertEqual(held_capabilities(self.operator), INVENTORY_CAPABILITIES)

    def test_every_machine_target_outside_the_fulfilment_walk_is_capability_mapped(
        self,
    ):
        # Deny-by-default drift pin. The machine is the source of every target
        # a scoped viewer could ever reach; any edge that is not a fulfilment
        # step must be mapped to the capability that owns it, or the next edge
        # someone adds would be writable by a packer by omission.
        model_admin = self.order_admin()
        machine_targets = set().union(*ALLOWED_TRANSITIONS.values())
        fulfilment_targets = set(ADMIN_FULFILMENT_NEXT.values())
        mapped = set(model_admin.scoped_value_capabilities["status"])
        self.assertEqual(machine_targets - fulfilment_targets, mapped)
        self.assertEqual(mapped, {"cancelled"})

    def test_the_seam_is_deny_by_default_for_an_admin_that_declares_no_writes(self):
        # The base's own defaults, probed directly: a ModelAdmin that opens the
        # door without declaring its writes must offer a read, not an editor.
        bare = RoleAwareModelAdmin(Order, admin.site)
        bare.scoped_view_capability = "orders.fulfill"
        request = RequestFactory().get(self.change_url())
        request.user = self.operator
        self.assertTrue(bare.is_scoped_viewer(request))
        self.assertEqual(bare.scoped_writable_fields, frozenset())
        self.assertEqual(bare.scoped_value_capabilities, {})
        exposed = {
            field
            for _title, options in bare.get_fieldsets(request, self.queued)
            for field in options["fields"]
        }
        self.assertTrue(exposed <= set(bare.get_readonly_fields(request, self.queued)))
        # ...and with nothing mapped, no value is withheld (the field set is
        # what refuses the write) — which is why the orders admin must map the
        # destructive one explicitly.
        self.assertTrue(bare.scoped_value_permitted(request, "status", "cancelled"))

    def test_the_value_predicate_answers_on_the_capability_not_on_the_role(self):
        # The predicate is the seam's single decision point, so it is pinned on
        # both answers: the packer is refused the cancel target, a cancel holder
        # is allowed it, and an unmapped value needs nothing extra.
        model_admin = self.order_admin()
        for user, cancelled_allowed in ((self.operator, False), (self.support, True)):
            with self.subTest(username=user.username):
                request = RequestFactory().get(self.change_url())
                request.user = user
                self.assertEqual(
                    model_admin.scoped_value_permitted(request, "status", "cancelled"),
                    cancelled_allowed,
                )
                self.assertTrue(
                    model_admin.scoped_value_permitted(request, "status", "confirmed")
                )


class SuperuserBypassTests(TestCase):
    """SPEC-1-B03 must not move Django's own superuser bypass (permissions.py)."""

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            "root-bypass", "root-bypass@example.com", None
        )
        groups = sync_role_groups()
        self.superadmin = User.objects.create_user(username="tier-bypass")
        self.superadmin.groups.add(groups[ROLE_SUPERADMIN])
        self.admin = User.objects.create_user(username="admin-bypass")
        self.admin.groups.add(groups[ROLE_ADMIN])
        self.model_admin = RoleAwareModelAdmin(Order, admin.site)

    def test_is_privileged_still_short_circuits_on_the_superuser_flag(self):
        # The bypass line itself is untouched: a superuser with no role group
        # at all is still privileged, which is what makes the MFA enrollment
        # surface reachable for them.
        self.assertFalse(self.superuser.groups.exists())
        self.assertTrue(is_privileged(self.superuser))

    def test_the_top_tier_is_privileged_for_mandatory_mfa(self):
        # The tier holds ``staff.manage`` (it manages access), so R-17.9
        # reaches it through the existing rule rather than a new one.
        self.assertTrue(is_privileged(self.superadmin))
        self.assertTrue(is_privileged(self.admin))

    def test_the_bypass_is_not_narrowed_to_the_roles_map(self):
        request = request_for(self.superuser)
        # Even an identifier no role grants: the superuser bypass answers
        # before the map is consulted, exactly as before SPEC-1-B03.
        self.assertTrue(
            self.model_admin._holds_capability(request, "platform.selfdestruct")
        )
        self.assertTrue(self.model_admin._map_grants(request, "add"))
        self.assertTrue(self.model_admin._map_grants(request, "change"))

    def test_the_bypass_does_not_leak_into_the_roles_map(self):
        self.assertFalse(
            self.model_admin._holds_capability(
                request_for(self.admin), "platform.configure"
            )
        )

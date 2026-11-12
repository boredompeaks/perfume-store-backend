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

TOP_TIER_CAPABILITIES = frozenset(
    {"platform.configure", "settings.manage", "staff.manage"}
)

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
    return frozenset(
        capability
        for capability in CAPABILITY_ROLES
        if user_has_capability(user, capability)
    )


class SuperadminTierDeclarationTests(TestCase):
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
        # Literal role names: the migration imports the same constants the test would otherwise derive from, so a rename would match itself here.
        self.assertTrue(Group.objects.filter(name="superadmin").exists())
        self.assertEqual(
            set(Group.objects.values_list("name", flat=True)),
            {
                "support",
                "catalogue",
                "inventory",
                "marketing",
                "finance",
                "admin",
                "superadmin",
            },
        )

    def test_sync_recreates_a_deleted_superadmin_group(self):
        Group.objects.filter(name=ROLE_SUPERADMIN).delete()

        groups = sync_role_groups()

        self.assertTrue(Group.objects.filter(name="superadmin").exists())
        self.assertEqual(groups[ROLE_SUPERADMIN].name, "superadmin")

    def test_the_rebootstrap_migration_adds_the_group_to_a_populated_database(self):
        Group.objects.filter(name=ROLE_SUPERADMIN).delete()
        survivor = Group.objects.get(name=ROLE_ADMIN)
        user = make_role_user(ROLE_ADMIN, "survivor")

        self.rebootstrap_migration().sync_staff_role_groups(None, None)

        self.assertTrue(Group.objects.filter(name=ROLE_SUPERADMIN).exists())
        self.assertTrue(Group.objects.filter(pk=survivor.pk).exists())
        self.assertTrue(user.groups.filter(pk=survivor.pk).exists())

    def test_the_rebootstrap_migration_reverse_removes_only_the_added_group(self):
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
        self.assertEqual(held_capabilities(self.superadmin), TOP_TIER_CAPABILITIES)

    def test_platform_configuration_is_the_superadmin_only_capability(self):
        self.assertTrue(user_has_capability(self.superadmin, "platform.configure"))
        for user in (self.admin, self.support, self.customer):
            with self.subTest(username=user.username):
                self.assertFalse(user_has_capability(user, "platform.configure"))

    def test_an_admin_cannot_configure_the_platform_through_the_permission_class(self):
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
        for capability in ("staff.manage", "settings.manage"):
            with self.subTest(capability=capability):
                self.assertTrue(user_has_capability(self.superadmin, capability))
                self.assertTrue(user_has_capability(self.admin, capability))

    def test_no_operational_role_reaches_the_privileged_capabilities(self):
        for capability in TOP_TIER_CAPABILITIES:
            with self.subTest(capability=capability):
                self.assertEqual(
                    CAPABILITY_ROLES[capability] - {ROLE_ADMIN, ROLE_SUPERADMIN},
                    frozenset(),
                )

    def test_deny_by_default_holds_for_the_new_tier(self):
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
    def setUp(self):
        self.admin_user = make_role_user(ROLE_ADMIN, "chief")
        self.superadmin_user = make_role_user(ROLE_SUPERADMIN, "tier-root")
        self.target = User.objects.create_user(
            username="temp",
            email="temp@example.com",
            password=TEST_PASSWORD,
            is_staff=True,
        )

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
        self.target.groups.add(Group.objects.get(name=ROLE_SUPERADMIN))
        self.client.force_login(self.admin_user)

        res = self.post_roles([])

        self.assertEqual(res.status_code, 403)
        self.target.refresh_from_db()
        self.assertTrue(self.target.groups.filter(name=ROLE_SUPERADMIN).exists())

    def test_a_direct_role_sync_is_refused_for_an_admin(self):
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
        flags = {"is_staff": "on"}

        first = self.post_roles([ROLE_SUPERADMIN], **flags)
        self.assertEqual(first.status_code, 200)
        committed = self.post_roles([ROLE_SUPERADMIN], confirm=True, **flags)

        self.assertEqual(committed.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.groups.filter(name=ROLE_SUPERADMIN).exists())

    def test_the_top_tier_is_the_only_role_whose_grant_needs_platform_configure(self):
        self.assertEqual(ROLE_GRANT_CAPABILITY, {ROLE_SUPERADMIN: "platform.configure"})
        for role in STAFF_ROLES:
            with self.subTest(role=role):
                user = make_role_user(role, f"assign-{role}")
                self.assertEqual(
                    user_may_assign_role(user, ROLE_SUPERADMIN),
                    role == ROLE_SUPERADMIN,
                )
                self.assertTrue(user_may_assign_role(user, ROLE_ADMIN))

    def test_the_guard_never_opens_a_role_to_an_unauthenticated_caller(self):
        self.assertFalse(user_may_assign_role(None, ROLE_SUPERADMIN))
        self.assertFalse(user_may_assign_role(AnonymousUser(), ROLE_SUPERADMIN))

    def test_the_django_superuser_flag_is_the_only_bypass_on_the_guard(self):
        superuser = User.objects.create_superuser("root", "root@example.com", None)
        self.assertTrue(user_may_assign_role(superuser, ROLE_SUPERADMIN))
        self.assertEqual(get_user_roles(superuser), frozenset())


class InventoryFulfilmentAuthorityTests(ApiTestCase):
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
        order = self._order()
        self.client.force_authenticate(self.support)

        res = self.client.post(f"/api/admin/orders/{order.id}/fulfill/")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], "confirmed")

    def test_the_json_seam_is_scoped_to_the_queue_for_a_fulfil_only_role(self):
        # orders.fulfill is not order visibility, so the operator's out-of-queue answer is the same uniform 404 an unknown id gets.
        order = self._walk_ready_order("Scoped Rose")
        settled = self._order(status="delivered", total_amount="250.00")
        self.client.force_authenticate(self.operator)

        settled_before = settled.status
        res = self.client.post(f"/api/admin/orders/{settled.id}/fulfill/")

        self.assertEqual(res.status_code, 404, res.data)
        settled.refresh_from_db()
        self.assertEqual(settled.status, settled_before)
        queued = self.client.post(f"/api/admin/orders/{order.id}/fulfill/")
        self.assertEqual(queued.status_code, 200, queued.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")

    def test_an_orders_read_holder_keeps_the_unscoped_json_seam(self):
        settled = self._order(status="delivered", total_amount="250.00")
        self.client.force_authenticate(self.support)

        res = self.client.post(f"/api/admin/orders/{settled.id}/fulfill/")

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data["details"]["allowed"], "")
        self.assertEqual(res.data["code"], "conflict")

    def test_the_superuser_flag_is_not_narrowed_by_the_queue_scope(self):
        root = User.objects.create_superuser("queue-root", "root@example.com", None)
        self.assertTrue(_may_fulfil(root, self._order(status="delivered")))
        self.assertFalse(_may_fulfil(AnonymousUser(), self._order()))


@tag("e2e")
class FulfilmentQueueSurfaceTests(ApiTestCase):
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
        self.client.force_login(self.operator)
        res = self.client.get("/admin/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.CHANGELIST)
        self.assertTrue(
            self.order_admin().has_module_permission(request_for(self.operator))
        )

    def test_the_index_hides_the_queue_from_a_role_that_may_neither_read_nor_pack(self):
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
        self.assertNotContains(res, "export_csv")

    def test_the_queue_withholds_the_money_and_the_customer_record(self):
        self.client.force_login(self.operator)
        res = self.client.get(self.CHANGELIST)
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, str(self.queued.total_amount))
        self.assertNotContains(res, self.queued.full_name)
        self.assertNotContains(res, self.queued.phone)
        self.assertNotContains(res, self.buyer.email)

    def test_the_queue_is_searched_by_reference_not_by_customer(self):
        self.client.force_login(self.operator)
        by_reference = self.client.get(self.CHANGELIST, {"q": self.queued.order_number})
        self.assertEqual(by_reference.status_code, 200)
        self.assertContains(by_reference, self.queued.order_number)
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
        self.assertContains(res, self.product.name)
        self.assertNotContains(res, "1500.00")
        self.assertNotContains(res, "750.00")
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
        self.client.force_login(self.operator)
        res = self.client.get(self.CHANGELIST)
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, 'id="saved-filters"')

    def test_the_scoped_viewer_is_exactly_fulfil_without_orders_read(self):
        model_admin = self.order_admin()
        for user, expected in (
            (self.operator, True),
            (self.support, False),
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
        model_admin = self.order_admin()
        self.assertEqual(
            tuple(model_admin.scoped_queryset), tuple(ADMIN_FULFILMENT_NEXT)
        )
        self.assertIn(model_admin.scoped_view_capability, set(CAPABILITY_ROLES))
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
    CHANGELIST = "/admin/orders/order/"

    def setUp(self):
        self.operator = make_role_user(ROLE_INVENTORY, "write-packer")
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
        Order.objects.filter(pk=order.pk).update(payment_status="captured")
        return order

    def order_admin(self):
        return admin.site._registry[Order]

    def change_url(self):
        return f"{self.CHANGELIST}{self.queued.pk}/change/"

    def change_post(self, **fields):
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
        order = self.queued
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertIsNone(order.cancelled_at)
        self.assertFalse(
            OrderStatusEvent.objects.filter(order=order).exists(),
            "a refused status write must leave no transition audit row",
        )

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
        self.client.force_login(self.operator)

        res = self.changelist_post(status="confirmed")

        self.assertEqual(res.status_code, 302)
        self.queued.refresh_from_db()
        self.assertEqual(self.queued.status, "confirmed")

    def test_a_fulfil_only_role_cannot_cancel_through_the_change_form(self):
        self.client.force_login(self.operator)

        res = self.change_post(status="cancelled")

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
        self.assertLessEqual({"confirmed", "shipped", "delivered"}, set(offered))
        self.assertNotContains(res, '<option value="cancelled">')

    def test_the_list_edit_cell_offers_the_packer_no_cancel_option(self):
        self.client.force_login(self.operator)

        res = self.client.get(self.CHANGELIST)

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="form-0-status"')
        self.assertNotContains(res, '<option value="cancelled"')

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
        self.assertContains(res, 'name="status"')

    def test_a_cancel_holder_keeps_its_change_form_authority(self):
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

    def test_the_write_declaration_names_the_packing_field_and_the_cancel_capability(
        self,
    ):
        model_admin = self.order_admin()
        self.assertEqual(model_admin.scoped_writable_fields, frozenset({"status"}))
        self.assertEqual(
            model_admin.scoped_value_capabilities,
            {"status": {"cancelled": "orders.cancel"}},
        )
        self.assertEqual(
            model_admin.scoped_value_capabilities["status"]["cancelled"],
            model_admin.action_capabilities["cancel_pending"],
        )
        self.assertFalse(user_has_capability(self.operator, "orders.cancel"))
        self.assertFalse(user_has_capability(self.operator, "orders.read"))
        self.assertEqual(held_capabilities(self.operator), INVENTORY_CAPABILITIES)

    def test_every_machine_target_outside_the_fulfilment_walk_is_capability_mapped(
        self,
    ):
        model_admin = self.order_admin()
        machine_targets = set().union(*ALLOWED_TRANSITIONS.values())
        fulfilment_targets = set(ADMIN_FULFILMENT_NEXT.values())
        mapped = set(model_admin.scoped_value_capabilities["status"])
        self.assertEqual(machine_targets - fulfilment_targets, mapped)
        self.assertEqual(mapped, {"cancelled"})

    def test_the_seam_is_deny_by_default_for_an_admin_that_declares_no_writes(self):
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
        self.assertTrue(bare.scoped_value_permitted(request, "status", "cancelled"))

    def test_the_value_predicate_answers_on_the_capability_not_on_the_role(self):
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
        self.assertFalse(self.superuser.groups.exists())
        self.assertTrue(is_privileged(self.superuser))

    def test_the_top_tier_is_privileged_for_mandatory_mfa(self):
        self.assertTrue(is_privileged(self.superadmin))
        self.assertTrue(is_privileged(self.admin))

    def test_the_bypass_is_not_narrowed_to_the_roles_map(self):
        request = request_for(self.superuser)
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

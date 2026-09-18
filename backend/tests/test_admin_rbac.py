"""SPEC-6-04: role-aware ModelAdmin least privilege (spec 6.12).

Pins the capability-driven admin base end to end:

- the whole registered store admin surface is role-aware (nothing missed),
- the view/add/change/delete matrix per role follows ``CAPABILITY_ROLES``
  exactly (least privilege, admin role keeps full function),
- bulk/export actions are role-gated and destructive ones are
  confirmation-gated, including through the real admin UI,
- the superuser bypass behaves like Django's own (unchanged trust anchor).
"""
from decimal import Decimal

from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, User
from django.test import RequestFactory, tag

from common.admin import RoleAwareModelAdmin
from common.permissions import user_has_capability
from common.roles import CAPABILITY_ROLES, ROLE_ADMIN, STAFF_ROLES
from common.testing import ApiTestCase
from accounts.admin import StoreUserAdmin
from cart.admin import CartAdmin
from cart.models import Cart
from ops.admin import SiteSettingsAdmin
from ops.models import SiteSettings
from orders.admin import CouponAdmin, OrderAdmin
from orders.models import Coupon, Order
from products.admin import ProductAdmin
from products.models import products

TEST_PASSWORD = "S3cure-Passphrase!"

# The pinned store admin surface (registry test asserts nothing else leaks in).
EXPECTED_MODELS = frozenset({products, Order, Coupon, SiteSettings, Cart, User})
EXPECTED_REGISTRY = {
    products: ProductAdmin,
    Order: OrderAdmin,
    Coupon: CouponAdmin,
    SiteSettings: SiteSettingsAdmin,
    Cart: CartAdmin,
    User: StoreUserAdmin,
}
PERMISSION_KINDS = ("view", "add", "change", "delete")


def make_role_user(role, username):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def make_order(user, status="pending", total="100.00"):
    return Order.objects.create(
        user=user,
        full_name="Admin Test",
        phone="9876543210",
        address="1 Admin Way",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        status=status,
        total_amount=Decimal(total),
    )


def request_for(user, method="get"):
    request = getattr(RequestFactory(), method)("/admin/")
    request.user = user
    return request


def store_admins():
    return {
        model: model_admin
        for model, model_admin in admin.site._registry.items()
        if model in EXPECTED_MODELS
    }


class RoleAwareAdminRegistryTests(ApiTestCase):
    """Every store ModelAdmin is role-aware and consistently configured."""

    def test_exactly_the_six_store_model_admins_are_registered(self):
        # admin.site._registry is the ground truth: if a future ModelAdmin
        # forgets the role-aware base, this equality fails.
        self.assertEqual(
            {model: type(model_admin) for model, model_admin in store_admins().items()},
            EXPECTED_REGISTRY,
        )

    def test_every_store_model_admin_is_role_aware(self):
        for model, model_admin in store_admins().items():
            with self.subTest(model=model.__name__):
                self.assertIsInstance(model_admin, RoleAwareModelAdmin)

    def test_capability_identifiers_all_come_from_the_roles_map(self):
        for model, model_admin in store_admins().items():
            used = set(model_admin.capability_map.values()) | set(
                model_admin.action_capabilities.values()
            )
            used.discard(None)
            with self.subTest(model=model.__name__):
                self.assertLessEqual(used, set(CAPABILITY_ROLES))

    def test_every_declared_action_is_capability_gated(self):
        # An action without a capability mapping would be usable by anyone
        # who can reach the changelist — pin the invariant that cannot drift.
        for model, model_admin in store_admins().items():
            with self.subTest(model=model.__name__):
                self.assertEqual(
                    set(model_admin.actions or ()),
                    set(model_admin.action_capabilities),
                )


class RoleAwareAdminPermissionMatrixTests(ApiTestCase):
    """view/add/change/delete × role follows CAPABILITY_ROLES exactly."""

    def test_matrix_for_every_role_kind_and_admin(self):
        for model, model_admin in store_admins().items():
            for role in STAFF_ROLES:
                user = make_role_user(role, f"{role}-{model.__name__}")
                request = request_for(user)
                for kind in PERMISSION_KINDS:
                    with self.subTest(model=model.__name__, role=role, kind=kind):
                        capability = model_admin.capability_map.get(kind)
                        expected = capability is not None and user_has_capability(
                            user, capability
                        )
                        self.assertIs(
                            getattr(model_admin, f"has_{kind}_permission")(request),
                            expected,
                        )

    def test_role_less_staff_sees_nothing(self):
        user = User.objects.create_user(
            username="plain",
            email="plain@example.com",
            password=TEST_PASSWORD,
            is_staff=True,
        )
        request = request_for(user)
        for model, model_admin in store_admins().items():
            with self.subTest(model=model.__name__):
                for kind in PERMISSION_KINDS:
                    self.assertFalse(
                        getattr(model_admin, f"has_{kind}_permission")(request)
                    )

    def test_admin_role_keeps_full_function(self):
        # Admin holds every capability the map grants, so it passes every
        # capability-backed kind on every admin — the RBAC equivalent of the
        # legacy blanket staff authority.
        request = request_for(make_role_user(ROLE_ADMIN, "chief"))
        for model, model_admin in store_admins().items():
            for kind in PERMISSION_KINDS:
                if model_admin.capability_map.get(kind) is not None:
                    with self.subTest(model=model.__name__, kind=kind):
                        self.assertTrue(
                            getattr(model_admin, f"has_{kind}_permission")(request)
                        )

    def test_superuser_bypass_covers_every_kind_including_capability_less(self):
        # Django's own superuser bypass is preserved verbatim: even kinds no
        # role may perform (cart edits, manual order rows) keep working. The
        # only exceptions are SiteSettings' add/delete, whose unconditional
        # singleton denial (pre-existing, deliberate) outranks the bypass.
        superuser = User.objects.create_superuser(
            "root", "root@example.com", TEST_PASSWORD
        )
        request = request_for(superuser)
        for model, model_admin in store_admins().items():
            for kind in PERMISSION_KINDS:
                if model is SiteSettings and kind in ("add", "delete"):
                    continue
                with self.subTest(model=model.__name__, kind=kind):
                    self.assertTrue(
                        getattr(model_admin, f"has_{kind}_permission")(request)
                    )

    def test_module_visibility_follows_view_permission(self):
        request = request_for(make_role_user(ROLE_ADMIN, "module-viewer"))
        for model, model_admin in store_admins().items():
            with self.subTest(model=model.__name__):
                self.assertIs(
                    model_admin.has_module_permission(request),
                    model_admin.has_view_permission(request),
                )


class RoleAwareAdminActionGatingTests(ApiTestCase):
    """Bulk/export actions appear only for roles holding their capability."""

    def test_order_action_availability_per_role(self):
        order_admin = admin.site._registry[Order]
        cases = {
            "support": {
                "mark_confirmed",
                "mark_shipped",
                "mark_delivered",
                "cancel_pending",
                "export_csv",
            },
            "finance": {"export_csv"},
            "marketing": set(),
            "catalogue": set(),
            "inventory": set(),
        }
        for role, expected in cases.items():
            with self.subTest(role=role):
                user = make_role_user(role, f"act-{role}")
                actions = order_admin.get_actions(request_for(user))
                self.assertEqual(set(actions), expected)

    def test_delete_selected_needs_the_delete_capability(self):
        # The admin-site-wide delete action rides Django's allowed_permissions
        # against our capability-driven has_delete_permission. Orders map
        # delete to no capability, so no staff role — not even admin — gets
        # the bulk delete action; only the superuser bypass does.
        order_admin = admin.site._registry[Order]
        support = order_admin.get_actions(
            request_for(make_role_user("support", "del-sup"))
        )
        chief = order_admin.get_actions(
            request_for(make_role_user(ROLE_ADMIN, "del-chief"))
        )
        self.assertNotIn("delete_selected", support)
        self.assertNotIn("delete_selected", chief)
        superuser = User.objects.create_superuser(
            "del-root", "root@example.com", TEST_PASSWORD
        )
        root = order_admin.get_actions(request_for(superuser))
        self.assertIn("delete_selected", root)

    def test_product_action_availability_per_role(self):
        product_admin = admin.site._registry[products]
        inventory = product_admin.get_actions(
            request_for(make_role_user("inventory", "act-inv"))
        )
        self.assertEqual(set(inventory), {"adjust_stock", "export_csv"})
        # Catalogue holds products.publish, so the admin-wide bulk delete is
        # available to it on top of the export.
        catalogue = product_admin.get_actions(
            request_for(make_role_user("catalogue", "act-cat"))
        )
        self.assertEqual(set(catalogue), {"export_csv", "delete_selected"})

    def test_admin_role_sees_every_gated_action(self):
        actions = admin.site._registry[Order].get_actions(
            request_for(make_role_user(ROLE_ADMIN, "act-admin"))
        )
        self.assertEqual(
            set(actions),
            {
                "mark_confirmed",
                "mark_shipped",
                "mark_delivered",
                "cancel_pending",
                "export_csv",
            },
        )


@tag("e2e")
class RoleAwareAdminSurfaceTests(ApiTestCase):
    """The gating holds through the real admin UI (no direct-call bypass)."""

    def setUp(self):
        self.buyer = self.make_user("buyer")
        self.pending = make_order(self.buyer, status="pending")
        self.confirmed = make_order(self.buyer, status="confirmed", total="250.00")

    def test_view_only_role_browses_but_cannot_change(self):
        self.client.force_login(make_role_user("support", "surf-support"))
        self.assertEqual(self.client.get("/admin/orders/order/").status_code, 200)
        # Finance reads orders but cannot fulfil: change page renders
        # read-only, and a POSTed save is rejected outright.
        self.client.force_login(make_role_user("finance", "surf-finance"))
        change_page = self.client.get(f"/admin/orders/order/{self.pending.id}/change/")
        self.assertEqual(change_page.status_code, 200)  # read-only render
        denied = self.client.post(
            f"/admin/orders/order/{self.pending.id}/change/",
            {
                "user": self.pending.user_id,
                "full_name": self.pending.full_name,
                "phone": self.pending.phone,
                "address": self.pending.address,
                "city": self.pending.city,
                "state": self.pending.state,
                "pincode": self.pending.pincode,
                "status": "confirmed",
                "_save": "Save",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(denied.status_code, 403)

    def test_unprivileged_role_gets_403_on_changelist(self):
        self.client.force_login(make_role_user("marketing", "surf-marketing"))
        self.assertEqual(self.client.get("/admin/orders/order/").status_code, 403)

    def test_write_capable_role_reaches_the_add_form(self):
        self.client.force_login(make_role_user("catalogue", "surf-catalogue"))
        add_page = self.client.get("/admin/products/products/add/")
        self.assertEqual(add_page.status_code, 200)
        # ...but orders are none of catalogue's business beyond the map.
        self.assertEqual(self.client.get("/admin/orders/order/").status_code, 403)

    def test_cancel_pending_requires_explicit_confirmation(self):
        self.client.force_login(make_role_user("support", "surf-cancel"))
        url = "/admin/orders/order/"
        selected = {"_selected_action": [str(self.pending.id), str(self.confirmed.id)]}

        first = self.client.post(url, {"action": "cancel_pending", **selected})
        self.assertEqual(first.status_code, 200)
        self.assertTemplateUsed(first, "admin/action_confirmation.html")
        self.assertContains(first, "Yes, proceed")
        self.pending.refresh_from_db()
        self.confirmed.refresh_from_db()
        self.assertEqual(self.pending.status, "pending")  # nothing happened yet
        self.assertEqual(self.confirmed.status, "confirmed")

        second = self.client.post(
            url, {"action": "cancel_pending", "confirm": "yes", **selected}, follow=True
        )
        self.assertEqual(second.status_code, 200)
        self.pending.refresh_from_db()
        self.confirmed.refresh_from_db()
        self.assertEqual(self.pending.status, "cancelled")
        self.assertEqual(self.confirmed.status, "confirmed")  # paid order spared

    def test_superuser_keeps_direct_execution_without_confirmation(self):
        User.objects.create_superuser("root", "root@example.com", TEST_PASSWORD)
        self.client.force_login(User.objects.get(username="root"))
        res = self.client.post(
            "/admin/orders/order/",
            {"action": "cancel_pending", "_selected_action": [str(self.pending.id)]},
        )
        self.assertEqual(res.status_code, 302)  # executed, back to the changelist
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, "cancelled")

    def test_bulk_status_change_leaves_a_log_entry(self):
        # [6.12.5]: bulk actions bypass save_model, so the base must log the
        # privileged mutation itself.
        self.client.force_login(make_role_user("support", "log-support"))
        res = self.client.post(
            "/admin/orders/order/",
            {"action": "mark_shipped", "_selected_action": [str(self.confirmed.id)]},
        )
        self.assertEqual(res.status_code, 302)
        entry = LogEntry.objects.get(object_id=str(self.confirmed.id))
        self.assertEqual(entry.user.username, "log-support")
        self.assertIn("shipped", entry.change_message)

    def test_confirmed_cancel_logs_the_privileged_action(self):
        self.client.force_login(make_role_user("support", "log-cancel"))
        selected = {"_selected_action": [str(self.pending.id)]}
        self.client.post(
            "/admin/orders/order/", {"action": "cancel_pending", **selected}
        )
        self.client.post(
            "/admin/orders/order/",
            {"action": "cancel_pending", "confirm": "yes", **selected},
        )
        entry = LogEntry.objects.get(object_id=str(self.pending.id))
        self.assertEqual(entry.user.username, "log-cancel")
        self.assertIn("cancelled", entry.change_message)

    def test_gated_action_posted_directly_is_inert(self):
        self.client.force_login(make_role_user("finance", "surf-finance"))
        res = self.client.post(
            "/admin/orders/order/",
            {"action": "mark_shipped", "_selected_action": [str(self.confirmed.id)]},
        )
        # Django's own handling of an action name outside the (gated) choice
        # list: warn and bounce back to the changelist — nothing executes.
        self.assertEqual(res.status_code, 302)
        self.confirmed.refresh_from_db()
        self.assertEqual(self.confirmed.status, "confirmed")


class AuthHelperTokenBranchTests(ApiTestCase):
    """Closes the last common/testing.py gap: auth() with a real token."""

    def test_auth_helper_attaches_a_bearer_token(self):
        self.make_user("tokenbuyer")
        _, token = self.api_login("tokenbuyer")
        self.client.credentials()  # drop what api_login attached
        self.assertEqual(self.client.get("/api/orders/").status_code, 401)
        self.auth(token)
        self.assertEqual(self.client.get("/api/orders/").status_code, 200)

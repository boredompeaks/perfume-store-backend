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

from common.admin import CONFIRMATION_NOTE_MAX_LENGTH, RoleAwareModelAdmin
from common.permissions import user_has_capability
from common.roles import CAPABILITY_ROLES, ROLE_ADMIN, STAFF_ROLES
from common.testing import ApiTestCase
from accounts.admin import StoreUserAdmin
from cart.admin import CartAdmin
from cart.models import Cart
from ops.admin import SiteSettingsAdmin
from ops.models import SiteSettings
from orders.admin import CouponAdmin, OrderAdmin
from orders.models import Coupon, Order, OrderItem
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
        # SPEC-10-03 fixture: the shipped edge requires items + a captured
        # payment; this row plays the paid, fulfilable order in the tests
        # below that mark it shipped.
        OrderItem.objects.create(
            order=self.confirmed,
            product_name="Fixture perfume",
            price=self.confirmed.total_amount,
            quantity=1,
            subtotal=self.confirmed.total_amount,
        )
        Order.objects.filter(pk=self.confirmed.pk).update(payment_status="captured")

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

    # ——— SPEC-20-1 [R-20.20]: the interstitial states the consequences ———

    def test_cancel_confirmation_names_amount_currency_items_and_state(self):
        # The delivered interstitial only rendered description + count +
        # str(obj); a financially significant confirmation has to name the
        # money, the lines and the state each row lands in. Values are
        # pinned, not just the presence of a section.
        OrderItem.objects.create(
            order=self.pending,
            product_name="Oud Nocturne",
            price=self.pending.total_amount,
            quantity=2,
            subtotal=self.pending.total_amount,
        )
        self.client.force_login(make_role_user("support", "surf-detail"))
        res = self.client.post(
            "/admin/orders/order/",
            {
                "action": "cancel_pending",
                "_selected_action": [str(self.pending.id), str(self.confirmed.id)],
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "admin/action_confirmation.html")
        details = res.context["details"]
        # One pending row of 2 is cancellable; the roll-up names both facts.
        self.assertEqual(
            details["summary"],
            f"1 of 2 selected order(s) will be cancelled, totalling 100.00 "
            f"{self.pending.currency}",
        )
        rows = {row["label"]: row for row in details["rows"]}
        pending_row = rows[f"Order #{self.pending.id} - {self.buyer.username}"]
        confirmed_row = rows[f"Order #{self.confirmed.id} - {self.buyer.username}"]
        self.assertEqual(
            pending_row["fields"],
            [
                ("Amount", self.pending.total_amount),
                ("Currency", self.pending.currency),
                ("Resulting state", "pending → cancelled"),
            ],
        )
        self.assertEqual(pending_row["items"], ["Oud Nocturne x 2"])
        self.assertEqual(
            confirmed_row["fields"][2],
            ("Resulting state", "confirmed → unchanged (paid, never cancelled here)"),
        )
        self.assertEqual(confirmed_row["items"], ["Fixture perfume x 1"])
        # ...and the payload actually reaches the page, not just the context.
        self.assertContains(res, "100.00")
        self.assertContains(res, self.pending.currency)
        self.assertContains(res, "Oud Nocturne x 2")
        self.assertContains(res, "pending → cancelled")
        # The delivered description/count/object list still renders.
        self.assertContains(res, "Affected (2)")
        self.assertContains(res, "Yes, proceed")
        # Nothing committed on the interstitial.
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, "pending")

    def test_already_cancelled_row_is_not_told_it_is_paid(self):
        # SPEC-20-1b: the reason clause, not the resulting state, was wrong.
        # An already-cancelled row lands in "unchanged" either way; calling
        # it "paid" told the operator something false about their selection.
        cancelled = make_order(self.buyer, status="cancelled")
        self.client.force_login(make_role_user("support", "surf-cancelled"))
        res = self.client.post(
            "/admin/orders/order/",
            {
                "action": "cancel_pending",
                "_selected_action": [str(cancelled.id)],
            },
        )
        self.assertEqual(res.status_code, 200)
        row = res.context["details"]["rows"][0]
        self.assertEqual(
            row["fields"][2],
            ("Resulting state", "cancelled → unchanged (already cancelled)"),
        )
        self.assertNotIn("paid", row["fields"][2][1])

    def test_confirmation_details_are_empty_without_a_declared_payload(self):
        # Only a declared financial action spells out its consequences; the
        # generic default must stay empty so no other confirmation grows an
        # invented payload.
        request = request_for(make_role_user("support", "surf-nodefault"))
        order_admin = admin.site._registry[Order]
        self.assertEqual(
            order_admin.confirmation_details(request, "mark_shipped", []), {}
        )
        # An admin that declares no payload at all — the base default —
        # renders the delivered interstitial unchanged.
        coupon_admin = admin.site._registry[Coupon]
        self.assertEqual(coupon_admin.confirmation_details(request, "export", []), {})

    # ——— SPEC-20-5 [R-20.28]: optional reason on cancel_pending ———

    def test_cancel_reason_is_merged_into_the_change_message(self):
        self.client.force_login(make_role_user("support", "log-reason"))
        selected = {"_selected_action": [str(self.pending.id)]}
        step = self.client.post(
            "/admin/orders/order/", {"action": "cancel_pending", **selected}
        )
        self.assertEqual(step.status_code, 200)
        # The textarea is offered here, and it is optional.
        note_field = step.context["note_form"].fields["confirmation_note"]
        self.assertFalse(note_field.required)
        self.assertIn('name="confirmation_note"', step.content.decode())
        # ...and what is typed there rides the commit into the audit trail.
        self.client.post(
            "/admin/orders/order/",
            {
                "action": "cancel_pending",
                "confirm": "yes",
                "confirmation_note": "Customer called to cancel",
                **selected,
            },
        )
        entry = LogEntry.objects.get(object_id=str(self.pending.id))
        self.assertIn("order cancelled", entry.change_message)
        self.assertIn("Reason: Customer called to cancel", entry.change_message)
        self.assertEqual(entry.user.username, "log-reason")
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, "cancelled")

    def test_cancel_without_a_reason_keeps_the_shipped_message(self):
        self.client.force_login(make_role_user("support", "log-noreason"))
        selected = {"_selected_action": [str(self.pending.id)]}
        self.client.post(
            "/admin/orders/order/", {"action": "cancel_pending", **selected}
        )
        self.client.post(
            "/admin/orders/order/",
            {"action": "cancel_pending", "confirm": "yes", **selected},
        )
        entry = LogEntry.objects.get(object_id=str(self.pending.id))
        self.assertIn("order cancelled", entry.change_message)
        self.assertNotIn("Reason:", entry.change_message)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, "cancelled")

    def test_reason_capture_is_gated_to_cancel_pending(self):
        order_admin = admin.site._registry[Order]
        request = request_for(make_role_user("support", "surf-gate"), method="post")
        request.POST = {
            "action": "cancel_pending",
            "confirm": "yes",
            "confirmation_note": "  Customer asked  ",
        }
        # Declared and supplied -> the trimmed reason comes back...
        self.assertEqual(
            order_admin.confirmation_note(request, "cancel_pending"),
            "Customer asked",
        )
        # ...while any other action reads no reason at all, whatever was
        # posted: the gate is the action name, not the presence of the key.
        self.assertEqual(order_admin.confirmation_note(request, "mark_shipped"), "")
        self.assertIsNone(order_admin.confirmation_reason_form("mark_shipped"))
        # An over-long (crafted) note is dropped, never truncated into the
        # audit trail, and never blocks the mutation.
        request.POST["confirmation_note"] = "x" * (CONFIRMATION_NOTE_MAX_LENGTH + 1)
        self.assertEqual(order_admin.confirmation_note(request, "cancel_pending"), "")
        # The declaration cannot drift: a reason is only ever asked for on an
        # action that is itself behind an interstitial.
        for model_admin in store_admins().values():
            with self.subTest(model=model_admin.model.__name__):
                self.assertLessEqual(
                    model_admin.confirmation_reason_actions,
                    model_admin.confirmation_required_actions,
                )

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

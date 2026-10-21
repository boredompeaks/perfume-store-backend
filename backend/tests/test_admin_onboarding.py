"""SPEC-D-06: role-tied staff onboarding, derived from the live admin registry.

The page is only useful if it cannot drift from the code it documents. These
pins therefore do not merely check that a capability string appears: they MUTATE
a registered ModelAdmin's ``capability_map`` and require the page to follow, in
both directions (add a capability, remove it). A hand-maintained list could pass
the static assertions and fail that pair.
"""

import re
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth.models import Group, User
from django.test import TestCase

from common.onboarding import _surface_url
from common.roles import STAFF_ROLES
from shipping.models import Shipment

TEST_PASSWORD = "S3cure-Passphrase!"
ONBOARDING = "/admin/onboarding/"


def make_role_user(role, username, is_staff=True):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=is_staff,
    )
    if is_staff:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def role_section(html, role):
    match = re.search(
        rf'<section id="role-{re.escape(role)}".*?</section>',
        html,
        re.DOTALL,
    )
    return match.group(0) if match else ""


class AdminOnboardingAccessTests(TestCase):
    def test_anonymous_is_redirected_to_the_admin_login(self):
        response = self.client.get(ONBOARDING)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/admin/login/"))

    def test_a_logged_in_non_staff_customer_gets_403(self):
        customer = make_role_user("support", "onboarding-customer", is_staff=False)
        self.client.force_login(customer)

        response = self.client.get(ONBOARDING)

        self.assertEqual(response.status_code, 403)


class AdminOnboardingContentTests(TestCase):
    def setUp(self):
        self.finance = make_role_user("finance", "onboarding-finance")
        self.root = make_role_user("superadmin", "onboarding-root")

    def _page(self, user):
        self.client.force_login(user)
        response = self.client.get(ONBOARDING)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def test_every_staff_role_has_a_section(self):
        _, html = self._page(self.finance)
        for role in STAFF_ROLES:
            self.assertTrue(role_section(html, role), f"missing section for {role}")

    def test_a_role_lists_only_the_capabilities_it_holds(self):
        _, html = self._page(self.finance)
        finance = role_section(html, "finance")

        self.assertIn("refunds.create", finance)
        self.assertIn("shipping.manage", finance)
        self.assertIn("reports.read", finance)
        self.assertNotIn("platform.configure", finance)
        self.assertNotIn("products.write", finance)

    def test_the_surfaces_behind_a_capability_link_to_their_changelist(self):
        _, html = self._page(self.finance)
        finance = role_section(html, "finance")

        self.assertIn('href="/admin/shipping/shippingmethod/"', finance)
        self.assertIn('href="/admin/shipping/shippingrate/"', finance)

    def test_a_surface_without_a_changelist_falls_back_to_the_admin_index(self):
        model = SimpleNamespace(
            _meta=SimpleNamespace(app_label="unregistered", model_name="nothing")
        )
        self.assertEqual(_surface_url(model), "/admin/")

    def test_the_top_tier_holds_the_platform_capability(self):
        _, html = self._page(self.root)
        root = role_section(html, "superadmin")

        self.assertIn("platform.configure", root)
        self.assertIn("staff.manage", root)
        # The top tier is not admin-plus-everything: it holds no operational
        # capability at all.
        self.assertNotIn("orders.fulfill", root)

    def test_the_current_users_capabilities_are_highlighted(self):
        response, _ = self._page(self.finance)
        root = next(
            role for role in response.context["roles"] if role["name"] == "superadmin"
        )
        platform = next(
            capability
            for capability in root["capabilities"]
            if capability["name"] == "platform.configure"
        )
        self.assertFalse(platform["held_by_user"])

        finance = next(
            role for role in response.context["roles"] if role["name"] == "finance"
        )
        refunds = next(
            capability
            for capability in finance["capabilities"]
            if capability["name"] == "refunds.create"
        )
        self.assertTrue(refunds["held_by_user"])

    def test_the_page_is_derived_from_the_registry_in_both_directions(self):
        model_admin = admin.site._registry[Shipment]
        original = dict(model_admin.capability_map)
        try:
            self.client.force_login(self.finance)
            before = role_section(
                self.client.get(ONBOARDING).content.decode(), "finance"
            )
            # Finance holds reports.read but not shipments.read, so the
            # shipments changelist is not on its page today.
            self.assertNotIn('href="/admin/shipping/shipment/"', before)

            model_admin.capability_map = {**original, "view": "reports.read"}
            after_add = role_section(
                self.client.get(ONBOARDING).content.decode(), "finance"
            )
            self.assertIn('href="/admin/shipping/shipment/"', after_add)

            model_admin.capability_map = {**original, "view": None}
            after_remove = role_section(
                self.client.get(ONBOARDING).content.decode(), "finance"
            )
            self.assertNotIn('href="/admin/shipping/shipment/"', after_remove)
        finally:
            model_admin.capability_map = original

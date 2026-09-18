"""SPEC-6-05b: privileged-action logging for API-side ops + audit-log route.

- Every privileged write through the capability-gated DRF API (today: the
  product surface, the only staff-privileged DRF mutations) lands a LogEntry
  naming the acting staff user, so the audit trail covers both the admin
  chrome and API operations (spec 6.12, [6.12.6] "Log privileged actions").
- Denied or invalid attempts are not privileged actions: nothing is written,
  so nothing is logged.
- /admin/audit-log/ is the single LogEntry reader, gated by ``staff.manage``
  (admin role) with Django's superuser bypass preserved: anonymous callers
  are sent to the admin login, unprivileged staff get 403.
"""
import json

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.models import ContentType
from django.test import tag

from common.roles import ROLE_ADMIN, ROLE_CATALOGUE, ROLE_SUPPORT, STAFF_ROLES
from common.testing import ApiTestCase
from products.models import products

TEST_PASSWORD = "S3cure-Passphrase!"


def make_role_user(role, username):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def write_payload(name):
    return {
        "name": name,
        "description": "Created through the gated API.",
        "price": "10.00",
        "size": 30,
        "stock": 3,
        "category": "Floral",
    }


@tag("e2e")
class ApiPrivilegedActionLoggingTests(ApiTestCase):
    """The capability-gated product writes emit LogEntry records ([6.12.6])."""

    def setUp(self):
        self.writer = make_role_user(ROLE_CATALOGUE, "scribe")
        self.client = self.fresh_client()
        self.api_login("scribe", client=self.client)

    def test_create_logs_addition_naming_the_actor(self):
        res = self.client.post(
            "/api/products/", write_payload("Audit Rose"), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        product = products.objects.get(name="Audit Rose")
        entry = LogEntry.objects.get(change_message="Created via API.")
        self.assertEqual(entry.user, self.writer)
        self.assertEqual(entry.action_flag, ADDITION)
        self.assertEqual(entry.object_repr, "Audit Rose")
        self.assertEqual(str(entry.object_id), str(product.pk))
        self.assertEqual(
            entry.content_type, ContentType.objects.get_for_model(products)
        )

    def test_patch_and_put_each_log_a_change(self):
        product = self.make_product(name="Edit Rose")
        res = self.client.patch(
            f"/api/products/{product.slug}/", {"price": "11.00"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        res = self.client.put(
            f"/api/products/{product.slug}/",
            write_payload("Edit Rose"),
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        updates = LogEntry.objects.filter(change_message="Updated via API.")
        self.assertEqual(updates.count(), 2)  # one per privileged write
        for entry in updates:
            self.assertEqual(entry.user, self.writer)
            self.assertEqual(entry.action_flag, CHANGE)
            self.assertEqual(entry.object_repr, "Edit Rose")
            self.assertEqual(str(entry.object_id), str(product.pk))

    def test_delete_logs_deletion_that_outlives_the_row(self):
        product = self.make_product(name="Doomed Rose")
        res = self.client.delete(f"/api/products/{product.slug}/")
        self.assertEqual(res.status_code, 204)
        entry = LogEntry.objects.get(change_message="Deleted via API.")
        self.assertEqual(entry.user, self.writer)
        self.assertEqual(entry.action_flag, DELETION)
        # The record keeps the last-known identity after the row is gone.
        self.assertEqual(entry.object_repr, "Doomed Rose")
        self.assertEqual(str(entry.object_id), str(product.pk))
        self.assertFalse(products.objects.filter(pk=product.pk).exists())

    def test_denied_and_invalid_attempts_log_nothing(self):
        """A refused request is not a privileged action: no write, no record."""
        support = make_role_user(ROLE_SUPPORT, "blocked-scribe")
        support_client = self.fresh_client()
        self.api_login("blocked-scribe", client=support_client)
        res = support_client.post(
            "/api/products/", write_payload("Blocked"), format="json"
        )
        self.assertEqual(res.status_code, 403, res.data)
        res = self.fresh_client().post(
            "/api/products/", write_payload("Sneak"), format="json"
        )
        self.assertEqual(res.status_code, 403, res.data)
        res = self.client.post("/api/products/", {"name": ""}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(LogEntry.objects.count(), 0)
        self.assertFalse(products.objects.exists())


@tag("e2e")
class AuditLogRouteTests(ApiTestCase):
    """The /admin/audit-log/ route: capability-gated LogEntry reader."""

    def setUp(self):
        self.admin_user = make_role_user(ROLE_ADMIN, "chief")

    def test_anonymous_is_redirected_to_admin_login(self):
        res = self.client.get("/admin/audit-log/")
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.url.startswith("/admin/login/"))

    def test_every_non_admin_role_is_403(self):
        # staff.manage is the map's admin-only capability, so the route is
        # invisible to the other five roles: a visible 403, not a redirect.
        non_admin_roles = [r for r in STAFF_ROLES if r != ROLE_ADMIN]
        for role in non_admin_roles:
            with self.subTest(role=role):
                viewer = make_role_user(role, f"viewer-{role}")
                self.client.force_login(viewer)
                res = self.client.get("/admin/audit-log/")
                self.assertEqual(res.status_code, 403)

    def test_admin_role_reads_api_and_admin_entries(self):
        writer = make_role_user(ROLE_CATALOGUE, "route-scribe")
        api_client = self.fresh_client()
        self.api_login("route-scribe", client=api_client)
        res = api_client.post(
            "/api/products/", write_payload("Trail Rose"), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.client.force_login(self.admin_user)
        page = self.client.get("/admin/audit-log/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Trail Rose")
        self.assertContains(page, "Created via API.")
        self.assertContains(page, "scribe")
        self.assertContains(page, "Addition")

    def test_superuser_bypass_reads_an_empty_trail(self):
        User.objects.create_superuser("root", "root@example.com", TEST_PASSWORD)
        self.client.force_login(User.objects.get(username="root"))
        page = self.client.get("/admin/audit-log/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "No audit entries yet.")

    def test_admin_chrome_json_change_messages_render_human(self):
        # The admin's own form saves store JSON-shaped change messages; the
        # page must render LogEntry.get_change_message's human form, never
        # the raw JSON.
        LogEntry.objects.log_actions(
            self.admin_user.pk,
            [self.admin_user],
            CHANGE,
            change_message=json.dumps([{"changed": {"fields": ["first_name"]}}]),
        )
        self.client.force_login(self.admin_user)
        page = self.client.get("/admin/audit-log/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Changed first_name.")
        self.assertNotContains(page, '["changed"')

    def test_pagination_serves_every_entry(self):
        content_type = ContentType.objects.get_for_model(products)
        LogEntry.objects.bulk_create(
            LogEntry(
                user_id=self.admin_user.pk,
                content_type_id=content_type.pk,
                object_id=str(i),
                object_repr=f"Row {i}",
                action_flag=CHANGE,
                change_message=f"Row {i} changed.",
            )
            for i in range(55)
        )
        self.client.force_login(self.admin_user)
        first = self.client.get("/admin/audit-log/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.context["entries"].paginator.count, 55)
        self.assertEqual(first.context["entries"].number, 1)
        second = self.client.get("/admin/audit-log/?page=2")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.context["entries"].number, 2)

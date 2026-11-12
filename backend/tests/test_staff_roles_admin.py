from types import SimpleNamespace

from django.contrib import admin
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, tag

from common.admin import CONFIRM_FIELD, CONFIRMATION_YES
from common.permissions import user_has_capability
from common.roles import (
    ROLE_ADMIN,
    ROLE_MARKETING,
    ROLE_SUPERADMIN,
    ROLE_SUPPORT,
    STAFF_ROLES,
)
from common.testing import ApiTestCase

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


def request_for(user):
    request = RequestFactory().post("/admin/")
    request.user = user
    return request


def user_change_post(user, **extra):
    payload = {
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "is_active": "on",
        "date_joined_0": user.date_joined.strftime("%Y-%m-%d"),
        "date_joined_1": user.date_joined.strftime("%H:%M:%S"),
        "orders-TOTAL_FORMS": "0",
        "orders-INITIAL_FORMS": "0",
        "orders-MIN_NUM_FORMS": "0",
        "orders-MAX_NUM_FORMS": "1000",
        "_save": "Save",
    }
    payload.update(extra)
    return payload


class StaffRoleChangeMixin:
    def change_url(self, target):
        return f"/admin/auth/user/{target.id}/change/"

    def confirm_role_change(self, url, payload):
        return self.client.post(url, {**payload, CONFIRM_FIELD: CONFIRMATION_YES})

    def assert_no_role_change(self, target):
        target.refresh_from_db()
        self.assertFalse(
            target.groups.filter(name__in=STAFF_ROLES).exists(),
            "no staff role may be committed before the confirmation",
        )
        self.assertFalse(
            LogEntry.objects.filter(change_message__icontains="role").exists(),
            "no role audit entry may exist before the confirmation",
        )


@tag("e2e")
class StaffRoleSurfaceUITests(StaffRoleChangeMixin, ApiTestCase):
    def setUp(self):
        self.admin_user = make_role_user(ROLE_ADMIN, "chief")
        self.customer = self.make_user("buyer")
        self.staff_target = User.objects.create_user(
            username="temp",
            email="temp@example.com",
            password=TEST_PASSWORD,
            is_staff=True,
        )

    def test_roles_field_lists_exactly_the_roles_an_admin_may_assign(self):
        self.staff_target.groups.add(Group.objects.get(name=ROLE_SUPPORT))
        self.client.force_login(self.admin_user)
        res = self.client.get(f"/admin/auth/user/{self.staff_target.id}/change/")
        self.assertEqual(res.status_code, 200)
        field = res.context["adminform"].form.fields["staff_roles"]
        self.assertEqual(
            set(field.queryset.values_list("name", flat=True)),
            set(STAFF_ROLES) - {ROLE_SUPERADMIN},
        )
        self.assertEqual(
            set(field.initial.values_list("name", flat=True)), {ROLE_SUPPORT}
        )

    def test_roles_field_lists_every_role_for_the_top_tier(self):
        self.staff_target.groups.add(Group.objects.get(name=ROLE_SUPPORT))
        self.client.force_login(make_role_user(ROLE_SUPERADMIN, "tier-root"))
        res = self.client.get(f"/admin/auth/user/{self.staff_target.id}/change/")
        self.assertEqual(res.status_code, 200)
        field = res.context["adminform"].form.fields["staff_roles"]
        self.assertEqual(
            set(field.queryset.values_list("name", flat=True)), set(STAFF_ROLES)
        )

    def test_support_role_sees_no_roles_field(self):
        self.client.force_login(make_role_user(ROLE_SUPPORT, "support-viewer"))
        res = self.client.get(f"/admin/auth/user/{self.staff_target.id}/change/")
        self.assertEqual(res.status_code, 200)
        rendered = [
            f
            for _, options in res.context["adminform"].fieldsets
            for f in options.get("fields", ())
        ]
        self.assertNotIn("staff_roles", rendered)

    def test_admin_assigns_two_roles_and_each_change_is_logged(self):
        self.client.force_login(self.admin_user)
        url = self.change_url(self.staff_target)
        payload = user_change_post(
            self.staff_target,
            is_staff="on",
            staff_roles=[
                str(Group.objects.get(name=ROLE_SUPPORT).pk),
                str(Group.objects.get(name=ROLE_MARKETING).pk),
            ],
        )
        self.assert_no_role_change(self.staff_target)

        step = self.client.post(url, payload)
        self.assertEqual(step.status_code, 200)
        self.assertTemplateUsed(step, "admin/action_confirmation.html")
        self.assert_no_role_change(self.staff_target)

        res = self.confirm_role_change(url, payload)
        self.assertEqual(res.status_code, 302)
        self.staff_target.refresh_from_db()
        self.assertEqual(
            set(self.staff_target.groups.values_list("name", flat=True)),
            {ROLE_SUPPORT, ROLE_MARKETING},
        )
        added = LogEntry.objects.filter(
            object_id=str(self.staff_target.id),
            change_message__contains='Added role "',
        )
        self.assertEqual(added.count(), 2)
        self.assertEqual(
            {e.change_message for e in added},
            {'Added role "support".', 'Added role "marketing".'},
        )
        for entry in added:
            self.assertEqual(entry.user, self.admin_user)
            self.assertEqual(entry.action_flag, CHANGE)

    def test_admin_removes_a_role_and_it_is_logged(self):
        self.staff_target.groups.add(Group.objects.get(name=ROLE_SUPPORT))
        self.client.force_login(self.admin_user)
        url = self.change_url(self.staff_target)
        payload = user_change_post(self.staff_target, is_staff="on", staff_roles=[])
        step = self.client.post(url, payload)
        self.assertEqual(step.status_code, 200)
        self.assertTemplateUsed(step, "admin/action_confirmation.html")
        self.staff_target.refresh_from_db()
        self.assertTrue(
            self.staff_target.groups.filter(name=ROLE_SUPPORT).exists()
        )  # still held: the removal is only confirmed, not applied

        res = self.confirm_role_change(url, payload)
        self.assertEqual(res.status_code, 302)
        self.staff_target.refresh_from_db()
        self.assertFalse(self.staff_target.groups.filter(name=ROLE_SUPPORT).exists())
        entry = LogEntry.objects.get(
            object_id=str(self.staff_target.id),
            change_message__contains='Removed role "support".',
        )
        self.assertEqual(entry.user, self.admin_user)

    def test_support_role_cannot_self_assign_admin(self):
        support = make_role_user(ROLE_SUPPORT, "self-escalator")
        self.client.force_login(support)
        res = self.client.post(
            f"/admin/auth/user/{support.id}/change/",
            user_change_post(
                support,
                is_staff="on",
                staff_roles=[str(Group.objects.get(name=ROLE_ADMIN).pk)],
            ),
        )
        self.assertEqual(res.status_code, 403)
        support.refresh_from_db()
        self.assertFalse(user_has_capability(support, "staff.manage"))
        self.assertFalse(support.groups.filter(name=ROLE_ADMIN).exists())
        self.assertFalse(LogEntry.objects.filter(object_id=str(support.id)).exists())

    def test_support_role_cannot_escalate_another_staff_user(self):
        self.client.force_login(make_role_user(ROLE_SUPPORT, "other-escalator"))
        res = self.client.post(
            f"/admin/auth/user/{self.staff_target.id}/change/",
            user_change_post(
                self.staff_target,
                is_staff="on",
                staff_roles=[str(Group.objects.get(name=ROLE_ADMIN).pk)],
            ),
        )
        self.assertEqual(res.status_code, 403)
        self.staff_target.refresh_from_db()
        self.assertFalse(self.staff_target.groups.filter(name=ROLE_ADMIN).exists())

    def test_customer_change_has_no_roles_field_and_tampering_is_ignored(self):
        self.client.force_login(self.admin_user)
        page = self.client.get(f"/admin/auth/user/{self.customer.id}/change/")
        self.assertEqual(page.status_code, 200)
        rendered = [
            f
            for _, options in page.context["adminform"].fieldsets
            for f in options.get("fields", ())
        ]
        self.assertNotIn("staff_roles", rendered)
        res = self.client.post(
            f"/admin/auth/user/{self.customer.id}/change/",
            user_change_post(
                self.customer,
                staff_roles=[str(Group.objects.get(name=ROLE_ADMIN).pk)],
            ),
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.context["adminform"].form.errors)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.groups.exists())
        self.assertFalse(
            LogEntry.objects.filter(
                object_id=str(self.customer.id), change_message__icontains="role"
            ).exists()
        )

    def test_demoting_to_non_staff_with_roles_selected_is_refused(self):
        User.objects.create_superuser(
            "root-demote", "root-demote@example.com", TEST_PASSWORD
        )
        root = User.objects.get(username="root-demote")
        self.client.force_login(root)
        url = self.change_url(self.staff_target)
        payload = user_change_post(
            self.staff_target,
            staff_roles=[str(Group.objects.get(name=ROLE_SUPPORT).pk)],
        )
        step = self.client.post(url, payload)
        self.assertEqual(step.status_code, 200)
        self.assertTemplateUsed(step, "admin/action_confirmation.html")
        self.staff_target.refresh_from_db()
        self.assertFalse(self.staff_target.groups.exists())

        res = self.confirm_role_change(url, payload)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Staff roles can only be assigned to staff users.")
        self.staff_target.refresh_from_db()
        self.assertTrue(self.staff_target.is_staff)
        self.assertFalse(self.staff_target.groups.exists())

    def test_admin_role_cannot_grant_is_staff_or_is_superuser(self):
        self.client.force_login(self.admin_user)
        page = self.client.get(f"/admin/auth/user/{self.staff_target.id}/change/")
        self.assertEqual(page.status_code, 200)
        for flag in ("is_staff", "is_superuser"):
            self.assertNotIn(flag, page.context["adminform"].form.fields)
        res = self.client.post(
            f"/admin/auth/user/{self.staff_target.id}/change/",
            user_change_post(
                self.staff_target,
                is_staff="on",
                is_superuser="on",
                staff_roles=[str(Group.objects.get(name=ROLE_SUPPORT).pk)],
            ),
        )
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "admin/action_confirmation.html")
        res = self.confirm_role_change(
            f"/admin/auth/user/{self.staff_target.id}/change/",
            user_change_post(
                self.staff_target,
                is_staff="on",
                is_superuser="on",
                staff_roles=[str(Group.objects.get(name=ROLE_SUPPORT).pk)],
            ),
        )
        self.assertEqual(res.status_code, 302)
        self.staff_target.refresh_from_db()
        self.assertFalse(self.staff_target.is_superuser)
        self.assertTrue(self.staff_target.is_staff)
        self.assertTrue(self.staff_target.groups.filter(name=ROLE_SUPPORT).exists())

    def test_admin_role_cannot_promote_a_customer_to_staff(self):
        self.client.force_login(self.admin_user)
        res = self.client.post(
            f"/admin/auth/user/{self.customer.id}/change/",
            user_change_post(self.customer, is_staff="on"),
        )
        self.assertEqual(res.status_code, 302)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_staff)
        self.assertFalse(self.customer.is_superuser)

    def test_admin_role_cannot_grant_user_permissions(self):
        delete_user = Permission.objects.get(
            content_type__app_label="auth", codename="delete_user"
        )
        self.client.force_login(self.admin_user)
        res = self.client.post(
            f"/admin/auth/user/{self.staff_target.id}/change/",
            user_change_post(self.staff_target, user_permissions=[str(delete_user.pk)]),
        )
        self.assertEqual(res.status_code, 302)
        self.staff_target.refresh_from_db()
        self.assertEqual(self.staff_target.user_permissions.count(), 0)

    def test_superuser_still_grants_the_flags(self):
        User.objects.create_superuser(
            "root-flags", "root-flags@example.com", TEST_PASSWORD
        )
        root = User.objects.get(username="root-flags")
        self.client.force_login(root)
        res = self.client.post(
            f"/admin/auth/user/{self.customer.id}/change/",
            user_change_post(self.customer, is_staff="on", is_superuser="on"),
        )
        self.assertEqual(res.status_code, 302)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.is_staff)
        self.assertTrue(self.customer.is_superuser)

    def test_add_view_still_creates_a_user_without_roles(self):
        self.client.force_login(self.admin_user)
        res = self.client.post(
            "/admin/auth/user/add/",
            {
                "username": "newcomer",
                "password1": TEST_PASSWORD,
                "password2": TEST_PASSWORD,
                "orders-TOTAL_FORMS": "0",
                "orders-INITIAL_FORMS": "0",
                "orders-MIN_NUM_FORMS": "0",
                "orders-MAX_NUM_FORMS": "1000",
                "_save": "Save",
            },
        )
        self.assertEqual(res.status_code, 302)
        newcomer = User.objects.get(username="newcomer")
        self.assertFalse(newcomer.groups.exists())
        self.assertFalse(
            LogEntry.objects.filter(
                object_id=str(newcomer.id), change_message__icontains="role"
            ).exists()
        )

    def test_superuser_bypass_assigns_roles(self):
        User.objects.create_superuser("root", "root@example.com", TEST_PASSWORD)
        root = User.objects.get(username="root")
        self.client.force_login(root)
        url = self.change_url(self.staff_target)
        payload = user_change_post(
            self.staff_target,
            is_staff="on",
            staff_roles=[str(Group.objects.get(name=ROLE_MARKETING).pk)],
        )
        step = self.client.post(url, payload)
        self.assertEqual(step.status_code, 200)
        self.assertTemplateUsed(step, "admin/action_confirmation.html")

        res = self.confirm_role_change(url, payload)
        self.assertEqual(res.status_code, 302)
        self.staff_target.refresh_from_db()
        self.assertTrue(self.staff_target.groups.filter(name=ROLE_MARKETING).exists())
        entry = LogEntry.objects.get(
            object_id=str(self.staff_target.id),
            change_message__contains='Added role "marketing".',
        )
        self.assertEqual(entry.user, root)


@tag("e2e")
class StaffRoleConfirmationTests(StaffRoleChangeMixin, ApiTestCase):
    def setUp(self):
        self.admin_user = make_role_user(ROLE_ADMIN, "chief-confirm")
        self.staff_target = User.objects.create_user(
            username="temp-confirm",
            email="temp-confirm@example.com",
            password=TEST_PASSWORD,
            is_staff=True,
        )
        self.staff_target.groups.add(Group.objects.get(name=ROLE_SUPPORT))
        self.client.force_login(self.admin_user)
        self.url = self.change_url(self.staff_target)

    def role_payload(self, **extra):
        return user_change_post(
            self.staff_target,
            is_staff="on",
            staff_roles=[str(Group.objects.get(name=ROLE_MARKETING).pk)],
            **extra,
        )

    def test_role_change_lands_on_the_interstitial_and_commits_nothing(self):
        res = self.client.post(self.url, self.role_payload())
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "admin/action_confirmation.html")
        self.assertEqual(
            res.context["details"]["summary"],
            "1 role(s) to add, 1 to remove — each is audited separately "
            "under your name once confirmed",
        )
        row = res.context["details"]["rows"][0]
        self.assertEqual(row["label"], self.staff_target.username)
        self.assertEqual(
            row["fields"],
            [("Roles to add", ROLE_MARKETING), ("Roles to remove", ROLE_SUPPORT)],
        )
        self.assertEqual(res.context["action_name"], "staff_role_change")
        content = res.content.decode()
        self.assertIn(f'name="confirm" value="{CONFIRMATION_YES}"', content)
        self.assertIn(f'href="{self.url}"', content)
        pending = dict(res.context["pending_fields"])
        marketing = str(Group.objects.get(name=ROLE_MARKETING).pk)
        self.assertEqual(pending["username"], self.staff_target.username)
        self.assertEqual(pending["staff_roles"], marketing)
        self.assertNotIn(CONFIRM_FIELD, pending)
        self.assertFalse([name for name in pending if name.startswith("csrf")])
        self.assertIsNone(res.context["note_form"])
        self.assert_roles_unchanged()

    def assert_roles_unchanged(self):
        self.staff_target.refresh_from_db()
        self.assertEqual(
            set(self.staff_target.groups.values_list("name", flat=True)),
            {ROLE_SUPPORT},
            "the held role must survive the interstitial untouched",
        )
        self.assertFalse(
            LogEntry.objects.filter(
                object_id=str(self.staff_target.id),
                change_message__contains="role",
            ).exists()
        )

    def test_confirming_commits_the_roles_with_the_per_direction_audit(self):
        payload = self.role_payload()
        self.client.post(self.url, payload)
        res = self.confirm_role_change(self.url, payload)
        self.assertEqual(res.status_code, 302)
        self.staff_target.refresh_from_db()
        self.assertEqual(
            set(self.staff_target.groups.values_list("name", flat=True)),
            {ROLE_MARKETING},
        )
        entries = LogEntry.objects.filter(
            object_id=str(self.staff_target.id),
            change_message__contains="role",
        )
        self.assertEqual(
            {e.change_message for e in entries},
            {f'Added role "{ROLE_MARKETING}".', f'Removed role "{ROLE_SUPPORT}".'},
        )
        self.assertEqual({e.user for e in entries}, {self.admin_user})

    def test_a_direct_post_of_a_role_change_is_never_committed_unconfirmed(self):
        res = self.client.post(self.url, self.role_payload())
        self.assertEqual(res.status_code, 200)
        self.assert_roles_unchanged()

    def test_an_emptied_selector_is_confirmed_as_a_removal(self):
        res = self.client.post(
            self.url, user_change_post(self.staff_target, is_staff="on")
        )
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "admin/action_confirmation.html")
        row = res.context["details"]["rows"][0]
        self.assertEqual(
            row["fields"],
            [("Roles to add", "none"), ("Roles to remove", ROLE_SUPPORT)],
        )
        self.assert_roles_unchanged()

    def test_a_save_that_changes_no_role_never_shows_the_interstitial(self):
        res = self.client.post(
            self.url,
            user_change_post(
                self.staff_target,
                is_staff="on",
                email="moved@example.com",
                staff_roles=[str(Group.objects.get(name=ROLE_SUPPORT).pk)],
            ),
        )
        self.assertEqual(res.status_code, 302)
        self.staff_target.refresh_from_db()
        self.assertEqual(self.staff_target.email, "moved@example.com")

    def test_a_caller_without_staff_manage_is_refused_not_confirmed(self):
        support = make_role_user(ROLE_SUPPORT, "support-confirm")
        self.client.force_login(support)
        res = self.client.post(self.url, self.role_payload())
        self.assertEqual(res.status_code, 403)
        self.assert_roles_unchanged()

    def test_a_customer_change_form_keeps_its_own_guards(self):
        buyer = self.make_user("buyer-confirm")
        res = self.client.post(
            self.change_url(buyer),
            user_change_post(
                buyer, staff_roles=[str(Group.objects.get(name=ROLE_ADMIN).pk)]
            ),
        )
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "admin/change_form.html")
        self.assertTrue(res.context["adminform"].form.errors)
        buyer.refresh_from_db()
        self.assertFalse(buyer.groups.exists())


class StaffRoleGuardUnitTests(ApiTestCase):
    def test_save_model_refuses_role_changes_without_staff_manage(self):
        target = User.objects.create_user(
            username="victim",
            email="victim@example.com",
            password=TEST_PASSWORD,
            is_staff=True,
        )
        escalated_form = SimpleNamespace(
            cleaned_data={"staff_roles": [Group.objects.get(name=ROLE_ADMIN)]}
        )
        with self.assertRaises(PermissionDenied):
            admin.site._registry[User].save_model(
                request_for(make_role_user(ROLE_SUPPORT, "direct-escalator")),
                target,
                escalated_form,
                change=True,
            )
        target.refresh_from_db()
        self.assertFalse(target.groups.exists())

    def test_apply_staff_roles_leaves_unrelated_groups_alone(self):
        chief = make_role_user(ROLE_ADMIN, "chief-unrelated")
        target = User.objects.create_user(
            username="unrelated",
            email="unrelated@example.com",
            password=TEST_PASSWORD,
            is_staff=True,
        )
        legacy = Group.objects.get_or_create(name="legacy-group")[0]
        target.groups.add(legacy)
        form = SimpleNamespace(
            cleaned_data={"staff_roles": [Group.objects.get(name=ROLE_SUPPORT)]}
        )
        admin.site._registry[User]._apply_staff_roles(request_for(chief), target, form)
        self.assertEqual(
            set(target.groups.values_list("name", flat=True)),
            {"legacy-group", ROLE_SUPPORT},
        )

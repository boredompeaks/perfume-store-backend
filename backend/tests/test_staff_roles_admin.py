"""SPEC-6-05a: staff + roles management surfaces (spec 6.12, [6.12.1]).

Pins the role-management surface on StoreUserAdmin end to end:

- the six staff role Groups are assignable/unassignable on staff users,
  but only by ``staff.manage`` holders (the admin role) or the preserved
  superuser bypass,
- a staff user WITHOUT ``staff.manage`` cannot escalate themselves or
  anyone else: the roles field is hidden (least privilege), their POSTs
  are refused by the change gate, and a directly constructed save_model
  call is refused too,
- every Group add/remove on a staff user writes its own admin LogEntry
  naming the acting user and the role ([6.12.5]),
- non-staff customers are unaffected: no roles field, no role groups.
"""
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, tag

from common.permissions import user_has_capability
from common.roles import ROLE_ADMIN, ROLE_MARKETING, ROLE_SUPPORT, STAFF_ROLES
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
    """Minimal valid payload for the User change form (extra=0 orders inline)."""
    payload = {
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "is_active": "on",
        # The admin renders datetimes as a split date/time widget.
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


@tag("e2e")
class StaffRoleSurfaceUITests(ApiTestCase):
    """The gating holds through the real admin UI (no direct-call bypass)."""

    def setUp(self):
        self.admin_user = make_role_user(ROLE_ADMIN, "chief")
        self.customer = self.make_user("buyer")
        self.staff_target = User.objects.create_user(
            username="temp",
            email="temp@example.com",
            password=TEST_PASSWORD,
            is_staff=True,
        )

    def test_roles_field_lists_exactly_the_six_roles_for_admin(self):
        self.staff_target.groups.add(Group.objects.get(name=ROLE_SUPPORT))
        self.client.force_login(self.admin_user)
        res = self.client.get(f"/admin/auth/user/{self.staff_target.id}/change/")
        self.assertEqual(res.status_code, 200)
        field = res.context["adminform"].form.fields["staff_roles"]
        self.assertEqual(
            set(field.queryset.values_list("name", flat=True)), set(STAFF_ROLES)
        )
        # The selector is seeded with the user's current role membership.
        self.assertEqual(
            set(field.initial.values_list("name", flat=True)), {ROLE_SUPPORT}
        )

    def test_support_role_sees_no_roles_field(self):
        # Least privilege: a viewer without staff.manage (but with
        # customers.read) gets the read-only page with roles hidden.
        self.client.force_login(make_role_user(ROLE_SUPPORT, "support-viewer"))
        res = self.client.get(f"/admin/auth/user/{self.staff_target.id}/change/")
        self.assertEqual(res.status_code, 200)
        # Least privilege is about the rendered surface: no fieldset may
        # carry the roles section for a viewer without staff.manage.
        rendered = [
            f for _, options in res.context["adminform"].fieldsets
            for f in options.get("fields", ())
        ]
        self.assertNotIn("staff_roles", rendered)

    def test_admin_assigns_two_roles_and_each_change_is_logged(self):
        self.client.force_login(self.admin_user)
        res = self.client.post(
            f"/admin/auth/user/{self.staff_target.id}/change/",
            user_change_post(
                self.staff_target,
                is_staff="on",
                staff_roles=[
                    str(Group.objects.get(name=ROLE_SUPPORT).pk),
                    str(Group.objects.get(name=ROLE_MARKETING).pk),
                ],
            ),
        )
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
        self.assertEqual(added.count(), 2)  # one entry per Group add
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
        res = self.client.post(
            f"/admin/auth/user/{self.staff_target.id}/change/",
            user_change_post(self.staff_target, is_staff="on", staff_roles=[]),
        )
        self.assertEqual(res.status_code, 302)
        self.staff_target.refresh_from_db()
        self.assertFalse(
            self.staff_target.groups.filter(name=ROLE_SUPPORT).exists()
        )
        entry = LogEntry.objects.get(
            object_id=str(self.staff_target.id),
            change_message__contains='Removed role "support".',
        )
        self.assertEqual(entry.user, self.admin_user)

    def test_support_role_cannot_self_assign_admin(self):
        # The [6.12] privilege-escalation pin: a staff user without
        # staff.manage POSTing their own change form with the admin role
        # gets the whole save refused by the change gate — no group, no
        # capability, no audit trail of it ever happening.
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
        self.assertFalse(
            self.staff_target.groups.filter(name=ROLE_ADMIN).exists()
        )

    def test_customer_change_has_no_roles_field_and_tampering_is_ignored(self):
        self.client.force_login(self.admin_user)
        page = self.client.get(f"/admin/auth/user/{self.customer.id}/change/")
        self.assertEqual(page.status_code, 200)
        rendered = [
            f for _, options in page.context["adminform"].fieldsets
            for f in options.get("fields", ())
        ]
        self.assertNotIn("staff_roles", rendered)
        # Tampering probe: staff_roles is not part of the rendered surface
        # for a non-staff account, and a POSTed value is refused by the
        # form guard (roles require a staff user) — nothing is written.
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
        # Roles + demotion in one save would leave a customer account
        # holding staff API authority; the form-level guard rejects it.
        self.client.force_login(self.admin_user)
        res = self.client.post(
            f"/admin/auth/user/{self.staff_target.id}/change/",
            user_change_post(
                self.staff_target,
                staff_roles=[str(Group.objects.get(name=ROLE_SUPPORT).pk)],
            ),  # is_staff omitted -> demotion attempted alongside roles
        )
        self.assertEqual(res.status_code, 200)  # re-rendered with the error
        self.assertContains(res, "Staff roles can only be assigned to staff users.")
        self.staff_target.refresh_from_db()
        self.assertTrue(self.staff_target.is_staff)  # nothing was written
        self.assertFalse(self.staff_target.groups.exists())

    def test_add_view_still_creates_a_user_without_roles(self):
        # The add view keeps DjangoUserAdmin.add_fieldsets/UserCreationForm;
        # no roles field exists there and none can be injected, so created
        # accounts start role-free (roles are assigned on the change page).
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
        # Django's own superuser bypass is preserved as the trust anchor.
        User.objects.create_superuser("root", "root@example.com", TEST_PASSWORD)
        root = User.objects.get(username="root")
        self.client.force_login(root)
        res = self.client.post(
            f"/admin/auth/user/{self.staff_target.id}/change/",
            user_change_post(
                self.staff_target,
                is_staff="on",
                staff_roles=[str(Group.objects.get(name=ROLE_MARKETING).pk)],
            ),
        )
        self.assertEqual(res.status_code, 302)
        self.staff_target.refresh_from_db()
        self.assertTrue(
            self.staff_target.groups.filter(name=ROLE_MARKETING).exists()
        )
        entry = LogEntry.objects.get(
            object_id=str(self.staff_target.id),
            change_message__contains='Added role "marketing".',
        )
        self.assertEqual(entry.user, root)


class StaffRoleGuardUnitTests(ApiTestCase):
    """Direct-call pins: the guard holds even off the normal view flow."""

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
        self.assertFalse(target.groups.exists())  # nothing was written

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
        admin.site._registry[User]._apply_staff_roles(
            request_for(chief), target, form
        )
        self.assertEqual(
            set(target.groups.values_list("name", flat=True)),
            {"legacy-group", ROLE_SUPPORT},
        )

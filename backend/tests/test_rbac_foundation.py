"""RBAC foundation tests (spec 6.12): staff role groups sync idempotently.

``common.roles`` is the source of truth for the six staff roles; the
``common`` bootstrap data migration calls ``sync_role_groups`` during
``migrate``. These tests pin that the sync is safe to re-run: exactly the
six stable-named groups exist, and a deleted group is re-created rather
than silently missing from a half-bootstrapped database.
"""
from django.contrib.auth.models import Group
from django.test import TestCase

from common.roles import ROLE_CATALOGUE, STAFF_ROLES, sync_role_groups


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

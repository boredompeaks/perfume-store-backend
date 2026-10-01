"""Add the superadmin role group to databases that already ran 0001.

Data-only, like its predecessor: no models exist in ``common``, so
``makemigrations --check`` stays clean. It is genuinely required —
``0001_sync_staff_role_groups`` has already been applied on every deployed
database, and it reads the live ``STAFF_ROLES`` only when it RUNS, so adding
a role to the map cannot reach an existing installation. This pass re-runs
the same idempotent ``sync_role_groups`` to create the one group 0001 could
not know about, leaving every existing group and membership untouched
(``get_or_create`` never touches what is there).

The reverse removes ONLY the role this migration added. 0001's reverse drops
all six of ITS groups, which was honest then; repeating that here would
delete live role assignments that predate this migration.
"""

from django.db import migrations

from common.roles import ROLE_SUPERADMIN, sync_role_groups

# The roles 0001 could not have created. A later tier adds its own migration
# rather than widening this tuple — this migration's reverse owns exactly
# what its forward pass introduced.
ADDED_ROLES = (ROLE_SUPERADMIN,)


def sync_staff_role_groups(apps, schema_editor):
    sync_role_groups()


def remove_superadmin_role_group(apps, schema_editor):
    # Reverses exactly what the forward pass created for this migration: the
    # superadmin group (and, via cascade, its membership rows). The six
    # groups 0001 created stay.
    apps.get_model("auth", "Group").objects.filter(name__in=ADDED_ROLES).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0005_savedfilter"),
    ]

    operations = [
        migrations.RunPython(sync_staff_role_groups, remove_superadmin_role_group),
    ]

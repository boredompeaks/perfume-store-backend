"""Bootstrap the six staff role groups (spec 6.12).

Data-only migration: no models exist in ``common``, so ``makemigrations
--check`` stays clean. Runs once per database during ``migrate`` (including
test-database creation) and is idempotent via ``get_or_create``.
"""

from django.db import migrations

from common.roles import STAFF_ROLES, sync_role_groups


def sync_staff_role_groups(apps, schema_editor):
    sync_role_groups()


def remove_staff_role_groups(apps, schema_editor):
    # Reverse exactly what the forward pass created: the six named groups
    # (and, via cascade, any membership rows pointing at them).
    apps.get_model("auth", "Group").objects.filter(name__in=STAFF_ROLES).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "__latest__"),
    ]

    operations = [
        migrations.RunPython(sync_staff_role_groups, remove_staff_role_groups),
    ]

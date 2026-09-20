# [R-10.1] SPEC-10-01a: explicit lifecycle dimensions (spec 10.2).
#
# Provenance: the legacy→dimensions mapping is IMPORTED from orders.state
# (the single source of truth) rather than copied here — orders.state is
# dependency-free, so it is importable at any migration state on a fresh
# DB, and a mapping edit can never drift between the machine and its
# backfill. The AddField defaults are frozen literals, exactly as the
# autodetector writes them: they stamp every pre-existing row before the
# RunPython pass refines per-status, and they stay historically accurate
# even if the choices later evolve.
from django.db import migrations, models

import orders.state


def backfill_dimensions(apps, schema_editor):
    """Stamp both dimensions for every legacy status (total mapping)."""
    order = apps.get_model('orders', 'Order')
    for legacy_status, dimensions in orders.state.LEGACY_STATUS_DIMENSIONS.items():
        order.objects.filter(status=legacy_status).update(
            payment_status=dimensions[0],
            fulfilment_status=dimensions[1],
        )


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0011_order_idempotency_key_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='fulfilment_status',
            field=models.CharField(choices=[('unfulfilled', 'Unfulfilled'), ('partially_fulfilled', 'Partially fulfilled'), ('fulfilled', 'Fulfilled')], default='unfulfilled', help_text='Fulfilment dimension of the lifecycle (spec 10.2).', max_length=20),
        ),
        migrations.AddField(
            model_name='order',
            name='payment_status',
            field=models.CharField(choices=[('pending', 'Pending'), ('authorized', 'Authorized'), ('captured', 'Captured'), ('failed', 'Failed'), ('partially_refunded', 'Partially refunded'), ('refunded', 'Refunded')], default='pending', help_text='Payment dimension of the lifecycle (spec 10.2).', max_length=20),
        ),
        # Reverse needs no data pass: the AddField operations' own reverse
        # drops the columns, dimension data included.
        migrations.RunPython(backfill_dimensions, migrations.RunPython.noop),
    ]

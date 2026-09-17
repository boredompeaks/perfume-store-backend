from django.db import migrations, models


def merge_duplicate_cart_items(apps, schema_editor):
    CartItem = apps.get_model('cart', 'CartItem')
    seen = {}
    for item in CartItem.objects.order_by('id'):
        key = (item.cart_id, item.product_id)
        existing = seen.get(key)
        if existing is None:
            seen[key] = item
        else:
            existing.quantity += item.quantity
            existing.save(update_fields=['quantity'])
            item.delete()


class Migration(migrations.Migration):
    dependencies = [('cart', '0001_initial')]

    operations = [
        migrations.RunPython(merge_duplicate_cart_items, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='cartitem',
            constraint=models.UniqueConstraint(fields=('cart', 'product'), name='unique_cart_product'),
        ),
    ]

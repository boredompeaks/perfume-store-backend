from django.db import migrations, models


def make_unique_slugs(apps, schema_editor):
    Product = apps.get_model('products', 'products')
    used = set()
    for product in Product.objects.order_by('id'):
        base = product.slug or f'product-{product.pk}'
        slug = base
        suffix = 2
        while slug in used:
            slug = f'{base[:95]}-{suffix}'
            suffix += 1
        used.add(slug)
        if product.slug != slug:
            product.slug = slug
            product.save(update_fields=['slug'])


class Migration(migrations.Migration):
    dependencies = [('products', '0003_products_slug_alter_products_description_and_more')]

    operations = [
        migrations.RunPython(make_unique_slugs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='products',
            name='slug',
            field=models.SlugField(blank=True, max_length=100, null=True, unique=True),
        ),
    ]

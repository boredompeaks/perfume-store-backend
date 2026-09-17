from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('orders', '0004_order_coupon_order_discount_amount')]

    operations = [
        migrations.AddField(model_name='order', name='razorpay_order_id', field=models.CharField(blank=True, max_length=100, null=True, unique=True)),
        migrations.AddField(model_name='order', name='razorpay_payment_id', field=models.CharField(blank=True, max_length=100, null=True, unique=True)),
    ]

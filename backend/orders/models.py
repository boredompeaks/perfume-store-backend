from django.db import models
from django.contrib.auth.models import User

from products.models import products

class Coupon(models.Model):

    DISCOUNT_TYPES = [
        ('percentage', 'Percentage'),
        ('fixed', 'Fixed Amount'),
    ]

    code = models.CharField(
        max_length=50,
        unique=True
    )

    discount_type = models.CharField(
        max_length=20,
        choices=DISCOUNT_TYPES
    )

    discount_value = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    minimum_order_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    maximum_discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True
    )

    active = models.BooleanField(
        default=True
    )

    valid_from = models.DateTimeField()

    valid_until = models.DateTimeField()

    usage_limit = models.PositiveIntegerField(
        null=True,
        blank=True
    )

    used_count = models.PositiveIntegerField(
        default=0
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.code


class Order(models.Model):
    """A checkout-created purchase.

    [R-8.4]/[R-8.5] Identifier-exposure strategy: the sequential ``id`` stays
    the internal key (URL/admin primary key, no URL changes); ``order_number``
    (ORD-YYYY-NNNNNN, per-year sequence) is the customer-facing reference the
    serializer exposes read-only. Guest checkout (SPEC-3-02) will key on
    ``order_number`` -- the pk never leaves server-side routing.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    ]

    # [R-8.4] Customer-facing reference, minted inside create_order's atomic
    # block. Nullable by design: checkout (the only production writer) always
    # sets it, non-checkout ORM creations keep working, and the 0006 data
    # migration backfills every pre-existing row, so the column is fully
    # populated after migrating. The unique index doubles as the concurrency
    # authority for generation (IntegrityError retry) and as the spec 8.3
    # "order number" index.
    order_number = models.CharField(
        max_length=20,  # 15 for ORD-YYYY-NNNNNN + headroom for format drift
        null=True,
        blank=True,
        unique=True,
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='orders'
    )

    full_name = models.CharField(
        max_length=150
    )

    phone = models.CharField(
        max_length=15
    )

    address = models.TextField()

    city = models.CharField(
        max_length=100
    )

    state = models.CharField(
        max_length=100
    )

    pincode = models.CharField(
        max_length=10
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )

    coupon = models.ForeignKey(
        Coupon,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='orders'
    )

    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    razorpay_order_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True, null=True, unique=True)

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    def __str__(self):
        return f"Order #{self.id} - {self.user.username}"


class OrderItem(models.Model):

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='items'
    )

    product = models.ForeignKey(
        products,
        on_delete=models.SET_NULL,
        null=True
    )

    product_name = models.CharField(
        max_length=200,
        default=''
    )

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    quantity = models.PositiveIntegerField()

    subtotal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"

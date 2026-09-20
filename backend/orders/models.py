from django.conf import settings
from django.db import models
from django.contrib.auth.models import User

from products.models import products


def default_currency():
    """[R-8.11] Store-config-driven currency for new money-bearing rows.

    A callable default rather than a hardcoded literal so a deployment can
    retune the store via the DEFAULT_CURRENCY env setting without a code
    change, while every order/item row still carries an explicit currency
    beside its amounts. Migrations stay deterministic regardless: the 0008
    backfill stamps existing rows with the literal 'INR' they were minted
    under, never with whatever the migrating environment's config says.
    """
    return settings.DEFAULT_CURRENCY


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

    # [R-8.11] Currency rides every money column (spec 8.3: "Store the
    # currency alongside the amount. Do not assume all currencies use two
    # decimal places."): total_amount and discount_amount are denominated
    # in this code, and the gateway payload and serializers read it from
    # here instead of assuming INR.
    currency = models.CharField(
        max_length=3,  # ISO 4217 code width
        default=default_currency,
    )

    razorpay_order_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True, null=True, unique=True)

    # [R-8.16] Business-event timeline (spec 8.3 "Timestamps": store distinct
    # timestamps for each business event; do not overload a generic
    # ``updated_at`` to represent one). Each column is NULL until the event
    # happens, is written exactly once by the code path that performs the
    # event, and is never mutated once set (writers guard with an is-none /
    # or-check; serializers and the admin expose them read-only). UTC
    # storage comes from USE_TZ=True, not from the columns. paid_at and
    # cancelled_at have live writers (verify_payment / admin cancel);
    # fulfilled_at, shipped_at, delivered_at and refunded_at are the named
    # pattern the later fulfilment and refund sections write -- no writer
    # touches them yet. Historical rows stay NULL on purpose: the events
    # predate the columns, their times are unknowable, so no backfill is
    # possible. No index yet: spec 8.3 says add indexes from measured query
    # patterns, and none of these dates is queried with status today.
    paid_at = models.DateTimeField(null=True, blank=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        # [R-8.17] SPEC-8-05: spec 8.3 "Indexes" starting set (2557
        # "Customer ID and order creation date", newest-first matching the
        # customer order-history sort; 2567 "Frequently queried status/date
        # combinations"). The remaining prescribed starting indexes are
        # satisfied by constraints and deliberately NOT duplicated:
        # order_number (2555) and the payment provider references
        # razorpay_order_id / razorpay_payment_id (2559 — no Payment model
        # exists, the provider references live on this table) each carry
        # unique=True, whose backing unique index serves those lookups
        # (PRAGMA index_list origin 'u'), and user_id keeps its FK
        # auto-index for user-only joins.
        indexes = [
            models.Index(
                fields=['user', '-created_at'],
                name='orders_user_created_idx',
            ),
            models.Index(
                fields=['status', 'created_at'],
                name='orders_status_created_idx',
            ),
        ]

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

    # [R-8.13] Frozen identity snapshots (spec 8.3 "Historical snapshots"):
    # set once at checkout from the catalogue state the customer bought and
    # never updated afterwards -- no save path mutates them; the customer
    # serializer and the admin inline expose them read-only. Population
    # source today (documented): no variant-selection input exists at
    # checkout (CartItem is product-only -- picking rides SPEC-3-21/SPEC-6-08)
    # and ``products`` carries no product-level SKU, so ``sku`` snapshots
    # empty and ``variant_name`` mirrors the product name; a matched
    # variant's SKU/name replaces both once selection input exists.
    sku = models.CharField(
        max_length=64,  # ProductVariant.sku width, so a later variant-matched
        default=''      # population source fits without another migration
    )

    variant_name = models.CharField(
        max_length=200,  # product_name width: it mirrors the product name
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

    # [R-8.11] Denomination of the price/subtotal money columns: set once
    # beside the amounts it labels, same store-config default as the parent
    # order, so per-line amounts stay unambiguous if the store currency
    # ever changes between order generations.
    currency = models.CharField(
        max_length=3,  # ISO 4217 code width
        default=default_currency,
    )

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"

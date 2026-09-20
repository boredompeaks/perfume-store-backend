from django.conf import settings
from django.db import models
from django.contrib.auth.models import User

from products.models import products

# [R-10.1] The order machine's constants live in orders.state (single
# source); models, admin and views all import the same objects. Named
# imports: a plain ``from . import state`` would be shadowed inside the
# Order class body by its address ``state`` field.
from .state import (
    FULFILMENT_STATUS_CHOICES,
    PAYMENT_STATUS_CHOICES,
    STATUS_CHOICES,
    STATUS_EVENT_TRIGGERS,
)


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

    # [R-10.1] Single-sourced in orders.state; the class attribute stays so
    # existing consumers (ops dashboard, admin filters) keep working.
    STATUS_CHOICES = STATUS_CHOICES

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

    # [R-10.1] SPEC-10-01a: the lifecycle split into explicit dimensions
    # (spec 10.2). Additive by design: ``status`` above remains the compat
    # surface; the writers keep these in sync with every status change
    # (orders.state.LEGACY_STATUS_DIMENSIONS is the mapping). null=False
    # with defaults so every row always answers both questions. No
    # db_index: the §8.3 prescribed starting set (SPEC-8-05, the Meta
    # indexes below) deliberately does not include these columns — indexes
    # come from measured query patterns, per the same policy as the event
    # timestamps.
    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default='pending',
        help_text="Payment dimension of the lifecycle (spec 10.2).",
    )
    fulfilment_status = models.CharField(
        max_length=20,
        choices=FULFILMENT_STATUS_CHOICES,
        default='unfulfilled',
        help_text="Fulfilment dimension of the lifecycle (spec 10.2).",
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

    # [R-9.3.14] SPEC-9-01: header-keyed checkout idempotency. Set once by
    # create_order when the client sent an Idempotency-Key header; NULL for
    # keyless submissions. Uniqueness is scoped per user (a reused key on
    # another account is an independent submission, never an existence
    # leak), and NULLs stay distinct in the constraint, so keyless rows
    # can never collide. No expiry: the key lives with the order row it
    # deduped, so a retry collapses onto the original outcome forever.
    idempotency_key = models.CharField(
        max_length=128,
        null=True,
        blank=True,
    )

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
        constraints = [
            # [R-9.3.14]/[R-9.3.19] The concurrency authority for keyed
            # checkout replays: create_order probes under the user-row lock
            # (fast path), and this constraint is the last-resort guarantee
            # that one user can never hold two orders for one key. The
            # backing index also serves the replay probe lookup.
            models.UniqueConstraint(
                fields=['user', 'idempotency_key'],
                name='orders_user_idem_key_uidx',
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


class OrderStatusEvent(models.Model):
    """[R-10.12]/[R-10.17]/[R-10.18] One immutable row per status transition.

    Every legal order-status transition (checkout creation, verify_payment,
    the admin change form, the admin bulk actions, the 9-07 admin JSON seam)
    appends exactly one row in the SAME transaction as the transition it
    records: a rolled-back writer leaves no event behind, and a committed
    event can never lack its transition ([R-10.18] rollback-together, pinned
    per writer in tests). Append-only by design: the save guard below
    rejects any pk-set re-save, and the admin registration is view-only
    (no add/change/delete permission), so no code path can rewrite history.
    ``actor`` is SET_NULL — deleting a user account must never cascade into
    the audit trail (and verify_payment's events carry actor NULL by design:
    the customer payment flow has no admin actor, the trigger names the
    source). ``from_status`` is NULL exactly for creation events (no source
    state). No backfill: rows predate the table and the transitions that
    produced them are unknowable, so historical orders legitimately have
    no trail before their next live transition.

    This is the TRANSITION trail; the privileged-action LogEntry trail
    (common.audit.log_api_action, SPEC-7-01) separately records who performed
    which admin operation — the two complement, never replace, each other.
    """

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='status_events'
    )

    from_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        null=True,
        blank=True,  # NULL only on creation events (no source state)
    )

    to_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_status_events',
    )

    trigger = models.CharField(
        max_length=30,
        choices=STATUS_EVENT_TRIGGERS,
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        # Newest first: the admin surface (and any future consumer) reads
        # the trail most-recent-first; the id breaks ties between events
        # written in the same transaction with equal timestamps.
        ordering = ("-created_at", "-id")
        verbose_name = "order status event"
        verbose_name_plural = "order status events"

    def save(self, *args, **kwargs):
        # [R-10.18] Append-only: a pk on the instance means an update path
        # (re-save or bulk-style edit via save), which would rewrite
        # history — reject it outright.
        if self.pk is not None:
            raise TypeError("OrderStatusEvent rows are append-only")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.order_id}: {self.from_status}->{self.to_status} ({self.trigger})"

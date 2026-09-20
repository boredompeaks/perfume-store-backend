from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.text import slugify


class products(models.Model):
    name = models.CharField(max_length=100)

    slug = models.SlugField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    description = models.TextField()
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )
    size = models.PositiveBigIntegerField()
    stock = models.PositiveBigIntegerField(default=0)
    category = models.CharField(max_length=50)
    image = models.ImageField(
        upload_to='products/',
        blank=True,
        null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # [R-8.17] SPEC-8-05: spec 8.3 "Indexes" starting set for the
        # catalogue (2551 "Product status and category relationships").
        # Category is the public listing/filter key, so it gets the explicit
        # index. The other catalogue prescriptions are covered without new
        # indexes: slug (2549) is satisfied by unique=True (2479) — its
        # backing unique index IS the slug lookup index — and the status
        # half of 2551 is N/A because this table has no lifecycle-status
        # column (stock health is derived by ``stock_health``, not stored
        # state). ProductVariant.sku (2553) is likewise satisfied by its
        # unique constraint (2481).
        indexes = [
            models.Index(fields=["category"], name="products_category_idx"),
        ]

    def save(self, *args, **kwargs):

        if not self.slug:
            base_slug = slugify(self.name) or 'product'
            slug = base_slug
            suffix = 2
            while products.objects.exclude(pk=self.pk).filter(slug=slug).exists():
                slug = f'{base_slug[:95]}-{suffix}'
                suffix += 1
            self.slug = slug

        super().save(*args, **kwargs)

    def adjust_stock(self, user, delta: int, reason: str, note: str = "") -> None:
        """Admin-side manual inventory adjustment. Raises ValueError if the
        change would push stock below zero. SPEC-6-02 [6.5.17]: there are no
        silent inventory edits — every mutation path (manual adjustments and
        payment-time sales alike) lands a StockMovement ledger row."""
        with transaction.atomic():
            # `self.stock` is stale the moment it is read: the payment flow
            # decrements this same row under a lock. Re-read it locked so the
            # below-zero guard and the ledger's stock_after gate on the real
            # current value instead of racing a concurrent writer.
            locked = products.objects.select_for_update().get(pk=self.pk)
            new_stock = locked.stock + delta
            if new_stock < 0:
                raise ValueError(
                    f"{locked.name}: adjustment of {delta:+d} would push stock "
                    f"below zero (current: {locked.stock})."
                )
            locked.stock = new_stock
            locked.save(update_fields=["stock"])
            self.stock = locked.stock
            StockMovement.objects.create(
                product=locked,
                delta=delta,
                reason=reason,
                note=note,
                stock_after=locked.stock,
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )

    @property
    def stock_health(self) -> str:
        if self.stock == 0:
            return "out"
        if self.stock <= 5:
            return "low"
        return "ok"

    def __str__(self):
        return self.name


class ProductVariant(models.Model):
    """A sellable variant of a product (SPEC-8-02a [R-8.7], spec section 8).

    Schema core only, mirroring spec 8.3's rules: an FK to the core product
    entity (2483), the globally unique merchant-entered SKU (2481 —
    ``unique=True`` is simultaneously the constraint and its backing unique
    index, 2553), the attributes the order-item snapshot list reads (variant
    name/options 2503, SKU 2505, unit price 2507), an on-hand count and
    distinct created/updated timestamps (2523).

    Boundaries: ``price`` is exact decimal money (2489, conventions.md:15);
    ``NULL`` means the product's own price applies — no read-side wiring is
    invented here (OrderItem snapshots are SPEC-8-02b's). ``stock`` is
    deliberately inert — ``products.stock`` remains the ONLY order-time
    authority (checkout/verify_payment never read this column); variant-stock
    reconciliation is SPEC-6-13's inventory-depth work, admin depth (role
    matrix, fieldsets, actions) is SPEC-6-08's. The SKU has no generation
    path, so conventions.md:17's IntegrityError retry has nothing to wrap:
    the DB constraint is the concurrency authority.
    """

    product = models.ForeignKey(
        products,
        on_delete=models.CASCADE,
        related_name="variants",
    )
    name = models.CharField(max_length=100)
    sku = models.CharField(
        max_length=64,
        unique=True,
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    stock = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.product.name} - {self.name} ({self.sku})"


class StockMovement(models.Model):
    """Audit ledger for inventory mutations (SPEC-6-02 [6.5.17]): manual
    admin adjustments via ``adjust_stock`` and payment-time sales via
    ``verify_payment`` — a stock change without a movement row is a bug."""

    class Reason(models.TextChoices):
        SALE = "sale", "Sale"
        RESTOCK = "restock", "Restock"
        CORRECTION = "correction", "Stock correction"
        DAMAGE = "damage", "Damaged / write-off"
        RETURNED = "returned", "Customer return"
        OTHER = "other", "Other"

    product = models.ForeignKey(
        products,
        on_delete=models.CASCADE,
        related_name="stock_movements",
    )
    delta = models.IntegerField()
    stock_after = models.PositiveBigIntegerField()
    reason = models.CharField(max_length=20, choices=Reason.choices)
    note = models.CharField(max_length=200, blank=True, default="")
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Stock movement"
        verbose_name_plural = "Stock movements"

    def __str__(self):
        return f"{self.product.name}: {self.delta:+d} ({self.reason})"


class StockReservation(models.Model):
    """A time-limited hold on units of a product for an in-flight checkout
    (SPEC-12-01, spec section 12.1).

    Section 12.1's inventory split maps onto the schema as follows: on-hand
    inventory stays ``products.stock`` (the only stock authority); reserved
    inventory is the sum of a product's active reservations' ``quantity``;
    available-to-sell is the derived difference — never stored, because the
    reserved/safety-field accounting depth is SPEC-6-13's; expiry lives in
    ``expires_at``; the owner/reference pair is ``owner`` + ``order``.

    Boundaries: schema core only. The writers arrive later — SPEC-12-02
    mints reservations inside create_order's atomic block and converts them
    inside verify_payment's locked block, SPEC-12-03 expires stale ones via
    the reconciler — so this model only carries the state they transition.
    ``quantity`` shares the PositiveBigIntegerField domain of
    ``products.stock`` / ``StockMovement.stock_after``: one integer domain
    for unit counts across the inventory schema.
    """

    class Status(models.TextChoices):
        # §12.1 "Recommended checkout behaviour" names the transitions: the
        # backend creates a time-limited reservation at checkout (step 3),
        # a successful confirmation converts it into a committed sale
        # (step 5), failed/cancelled checkout releases it (step 6), and the
        # scheduled reconciliation expires stale ones (step 7).
        ACTIVE = "active", "Active"
        CONVERTED = "converted", "Converted"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"

    product = models.ForeignKey(
        products,
        on_delete=models.CASCADE,
        related_name="stock_reservations",
    )
    # Cross-app references use the file's existing string-FK pattern (see
    # StockMovement.created_by): the products app never imports orders/auth
    # at model load, and orders.models already imports products.models.
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="stock_reservations",
    )
    owner = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="stock_reservations",
    )
    quantity = models.PositiveBigIntegerField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Checkout mints at most one reservation per order line: a duplicate
        # (order, product) row would double-count reserved stock in the
        # available-to-sell math, and a retried checkout must re-target the
        # existing hold instead of minting a second one. §12.1 prescribes
        # no explicit constraint name; this one is implied by "reserved
        # inventory" being a per-checkout hold with an owner/reference, and
        # it is the concurrency authority the DB enforces under races.
        constraints = [
            models.UniqueConstraint(
                fields=["order", "product"],
                name="uniq_order_product_reservation",
            ),
        ]
        # The reconciler's sweep (SPEC-12-03) reads status=active AND
        # expires_at<=now: the composite index serves exactly that shape,
        # so expiry-based queries never scan the whole reservation table.
        indexes = [
            models.Index(
                fields=["status", "expires_at"],
                name="stockres_status_expires_idx",
            ),
        ]
        verbose_name = "Stock reservation"
        verbose_name_plural = "Stock reservations"

    @classmethod
    def expiry_from_now(cls):
        """``expires_at`` for a reservation minted now: now + RESERVATION_TTL.

        Read at call time, not import time (the ops/services.py pattern):
        the value is env-driven (settings.RESERVATION_TTL) and overridable
        per request/per test, so a module-level constant would freeze the
        first-seen value for the life of the process.
        """
        return timezone.now() + timedelta(seconds=settings.RESERVATION_TTL)

    def __str__(self):
        return f"{self.product.name}: {self.quantity} reserved ({self.status})"

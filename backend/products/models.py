from django.db import models, transaction
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

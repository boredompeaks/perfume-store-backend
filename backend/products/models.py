from django.db import models
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
        """Admin-side inventory adjustment. Raises ValueError if the change
        would push stock below zero. Payment-time decrements (orders flow)
        deliberately do NOT create movements — this ledger tracks manual
        admin adjustments only."""
        new_stock = self.stock + delta
        if new_stock < 0:
            raise ValueError(
                f"{self.name}: adjustment of {delta:+d} would push stock below zero "
                f"(current: {self.stock})."
            )
        self.stock = new_stock
        self.save(update_fields=["stock"])
        StockMovement.objects.create(
            product=self,
            delta=delta,
            reason=reason,
            note=note,
            stock_after=self.stock,
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
    """Audit ledger for manual inventory adjustments made in the admin."""

    class Reason(models.TextChoices):
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

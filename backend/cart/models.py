from django.db import models
from products.models import products


class Cart(models.Model):

    session_id = models.CharField(
        max_length=100,
        unique=True
    )

    # R-9.3.5/R-9.3.6: the applied coupon is persistent cart state, not a
    # per-request payload -- it survives across requests so checkout can
    # pick it up when the payload posts no explicit code (and re-validate
    # it through its pre-existing coupon path). SET_NULL: deleting a coupon
    # drops it from carts instead of stranding them on a dangling FK; the
    # invalidated-coupon case is handled by checkout re-validation.
    coupon = models.ForeignKey(
        'orders.Coupon',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+'
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    def __str__(self):
        return f"Cart {self.id}"


class CartItem(models.Model):

    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name='items'
    )

    product = models.ForeignKey(
        products,
        on_delete=models.CASCADE
    )

    quantity = models.PositiveIntegerField(
        default=1
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['cart', 'product'], name='unique_cart_product')
        ]

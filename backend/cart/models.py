from django.conf import settings
from django.db import models
from django.db.models import Q

from products.models import products


class Cart(models.Model):
    # A cart belongs to an account once the customer signs in, else to the
    # browser session; exactly one key is set. An adopted cart clears session_id,
    # so the cookie can never resolve back to the account cart after logout.
    session_id = models.CharField(max_length=100, unique=True, null=True, blank=True)

    # Nullable for guest carts; CASCADE so deleting an account leaves no
    # addressable cart.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="carts",
    )

    # The applied coupon is persistent cart state, so checkout can pick it up
    # when the payload posts no code; checkout re-validates it. SET_NULL drops
    # it from carts instead of stranding them on a dangling FK.
    coupon = models.ForeignKey(
        "orders.Coupon",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # The order this cart checked out into, bound at order creation so a later
    # capture (even a gateway webhook with no session cookie) can find and empty
    # it. OneToOne, SET_NULL: deleting an order must never destroy cart rows.
    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cart",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Cart {self.id}"

    class Meta:
        constraints = [
            # One active cart per account. Partial on purpose: guest carts all
            # carry NULL and must stay unlimited, while two account carts would
            # fork the basket between requests.
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(user__isnull=False),
                name="unique_active_cart_per_user",
            )
        ]


class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")

    product = models.ForeignKey(products, on_delete=models.CASCADE)

    quantity = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "product"], name="unique_cart_product"
            )
        ]

from django.db import models

from products.models import products


class Cart(models.Model):
    session_id = models.CharField(max_length=100, unique=True)

    # R-9.3.5/R-9.3.6: the applied coupon is persistent cart state, not a
    # per-request payload -- it survives across requests so checkout can
    # pick it up when the payload posts no explicit code (and re-validate
    # it through its pre-existing coupon path). SET_NULL: deleting a coupon
    # drops it from carts instead of stranding them on a dangling FK; the
    # invalidated-coupon case is handled by checkout re-validation.
    coupon = models.ForeignKey(
        "orders.Coupon",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # TIER1-WEBHOOK-CART: the Order this cart checked out into, bound at
    # order creation (orders.views.checkout.create_order) so a later capture
    # - browser verify or a much-later gateway webhook, which has no session
    # cookie - can find this cart and empty it (the clear lives in
    # orders.inventory.commit_order_sale). Nullable: a browsing or abandoned
    # cart has no order yet. OneToOne: one checkout submission snapshots one
    # cart into one order. SET_NULL: deleting an order (admin cleanup, test
    # teardown) must never cascade-destroy cart rows - the link exists for
    # capture-time cleanup, not for the cart's existence.
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

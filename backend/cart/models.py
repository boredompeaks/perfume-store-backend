from django.conf import settings
from django.db import models
from django.db.models import Q

from products.models import products


class Cart(models.Model):
    # D-04a: a cart belongs to an ACCOUNT once the customer signs in, and to
    # the browser session before that. Both keys are nullable because exactly
    # one of them is set on any live cart: a guest cart is keyed by session,
    # a signed-in cart by user. A session cart that is adopted into an
    # account CLEARS session_id, so the same browser cookie can never resolve
    # back to somebody's account cart after logout (resolve_cart's anonymous
    # branch looks the session key up unchanged). unique=True still holds on
    # the session key; both engines allow repeated NULLs.
    session_id = models.CharField(max_length=100, unique=True, null=True, blank=True)

    # D-04a: the account owner. CASCADE because deleting an account must not
    # leave a cart addressable by its old user id. Nullable is the guest half.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="carts",
    )

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

    class Meta:
        constraints = [
            # D-04a: one active cart per account. PARTIAL on purpose — guest
            # carts all carry user=NULL and must stay unlimited, while two
            # account carts for one user would fork the customer's basket
            # between requests (resolve_cart's get_or_create would race onto
            # whichever row the DB returned first).
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

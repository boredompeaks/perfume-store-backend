import logging
import os
from datetime import timedelta
from functools import partial

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from django.utils.text import slugify

# Dedicated channel (mirrors common.notifications/ops.alerts) so restock
# failures route independently in log tooling.
logger = logging.getLogger("products.restock")


# The type half is ImageField's Pillow check; this ceiling is the size half,
# enforced before storage. Read at call time so settings overrides apply.
def _max_upload_bytes():
    return settings.MAX_UPLOAD_MB * 1024 * 1024


def validate_image_size(image):
    """Reject image files above MAX_UPLOAD_MB with a field-level error.

    Attached as a model-field validator so DRF and ModelForm both inherit one
    enforcement point; size is read via seek/tell because upload wrappers do
    not all expose .size.
    """
    if not image or not image.file:
        return
    handle = image.file
    size = getattr(handle, "size", None)
    if size is None:
        position = handle.tell()
        size = handle.seek(0, os.SEEK_END)
        handle.seek(position)
    if size > _max_upload_bytes():
        raise ValidationError(
            "Image exceeds the maximum upload size of %d MB." % settings.MAX_UPLOAD_MB
        )


class products(models.Model):
    name = models.CharField(max_length=100)

    slug = models.SlugField(max_length=100, unique=True, null=True, blank=True)

    description = models.TextField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    size = models.PositiveBigIntegerField()
    stock = models.PositiveBigIntegerField(default=0)
    category = models.CharField(max_length=50)
    # Delist flips state only: never touch ``stock`` or write a StockMovement.
    # Default on-sale (a False default would empty the live catalogue).
    is_active = models.BooleanField(default=True)
    # Merchant delivery rule; False default keeps existing rows unchanged. Not
    # in the public payload: the storefront's same-named field is a shipping flag.
    free_shipping = models.BooleanField(default=False)
    image = models.ImageField(
        upload_to="products/",
        blank=True,
        null=True,
        validators=[validate_image_size],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # ``category`` is the public listing/filter key. ``slug`` and
        # ``ProductVariant.sku`` already have unique indexes; no status column.
        indexes = [
            models.Index(fields=["category"], name="products_category_idx"),
        ]

    def _slug_candidates(self):
        """Yield slug candidates for a row that arrived without one.

        The DB unique constraint is the concurrency authority — ``save()``
        retries on IntegrityError rather than checking first; split from
        ``save()`` because PostgreSQL enforces the column bound and SQLite
        does not, so an over-long name fails on only one engine.
        """
        # Read the bound off the model's own field: hard-coded literals rot
        # the moment the column changes.
        max_length = self._meta.get_field("slug").max_length
        base_slug = slugify(self.name) or "product"
        yield base_slug[:max_length]
        suffix = 2
        while True:
            # Truncate the base, not the finished candidate: the suffix would be
            # cut off, collisions would repeat, and the walk would never end.
            marker = f"-{suffix}"
            yield f"{base_slug[: max_length - len(marker)]}{marker}"
            suffix += 1

    def save(self, *args, **kwargs):

        if self.slug:
            super().save(*args, **kwargs)
            return

        for candidate in self._slug_candidates():
            self.slug = candidate
            try:
                # Each attempt gets its own savepoint so a rejected insert
                # leaves the outer transaction usable for the retry.
                with transaction.atomic():
                    super().save(*args, **kwargs)
                return
            except IntegrityError as exc:
                # Only the slug constraint is retryable (both engines name it in
                # the error); anything else is a real defect and must propagate.
                if "slug" not in str(exc):
                    raise
                continue

        raise AssertionError("slug candidate walk exhausted")  # pragma: no cover

    def adjust_stock(self, user, delta: int, reason: str, note: str = "") -> None:
        """Admin-side manual inventory adjustment; raises ValueError below zero.

        Every stock mutation lands a StockMovement row — no silent inventory
        edits.
        """
        with transaction.atomic():
            # ``self.stock`` is stale once read (payment decrements under a
            # lock): re-read locked so the guard and ledger see the real value.
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
            # Fires on the 0 -> positive crossing (inverse re-arms spent rows)
            # and never raises: mail must not fail the write.
            previous_stock = locked.stock - delta
            if locked.stock > 0 and previous_stock <= 0:
                # Register on commit, don't send here: N SMTP round trips while
                # the row lock verify_payment shares is held; rollback drops it.
                transaction.on_commit(partial(self._notify_back_in_stock, locked))
            elif locked.stock == 0 and previous_stock > 0:
                self._rearm_restock_notifications(locked)

    @staticmethod
    def _rearm_restock_notifications(product):
        """Sell-out re-arm (positive -> 0): clear the spent stamp so the next
        restock re-mails opted-in customers — one email per stock cycle.
        """
        RestockNotification.objects.filter(
            product=product, active=True, notified_at__isnull=False
        ).update(notified_at=None)

    @classmethod
    def register_back_in_stock_release(cls, product):
        """Relist half of the deferred back-in-stock notification.

        A restock landing while delisted is suppressed rather than silently
        dropped: nothing is claimed, and the relist registers the same fan-out
        so waiting opt-ins are released once the product is buyable.
        """
        transaction.on_commit(partial(cls._notify_back_in_stock, product))

    @staticmethod
    def _notify_back_in_stock(product):
        """Email every armed opt-in for ``product`` via the single send path.

        Runs on ``transaction.on_commit`` so no row lock is held across SMTP;
        each row is claimed by a conditional UPDATE before sending (the claim
        is both the mutual exclusion and the spent stamp, released if the send
        fails), and an inactive product claims nothing so the relist's fan-out
        can deliver later.
        """
        from common import notifications

        if not products.objects.filter(pk=product.pk, is_active=True).exists():
            return

        armed = RestockNotification.objects.filter(
            product=product,
            active=True,
            notified_at__isnull=True,
        ).select_related("user")
        for preference in armed:
            claimed_at = timezone.now()
            # The captured list predates this post-commit claim, so re-check
            # the whole arming predicate: an opt-out in between must not mail.
            claimed = RestockNotification.objects.filter(
                pk=preference.pk, active=True, notified_at__isnull=True
            ).update(notified_at=claimed_at)
            if not claimed:
                # Already claimed or disarmed: its email for this cycle is
                # accounted for.
                continue
            try:
                # Owner-shaped address: accounts mail user.email (a body email
                # never redirects it); guests mail the stored email column.
                recipient = (
                    preference.email
                    if preference.user_id is None
                    else preference.user.email
                )
                notifications.send_email(
                    "back_in_stock",
                    {"product": product, "frontend_url": settings.FRONTEND_URL},
                    f"Back in stock: {product.name}",
                    recipient,
                )
            except Exception:
                # Log-only: one SMTP failure must not abort the remaining
                # opt-ins; release the claim so the next crossing retries.
                logger.exception(
                    "Back-in-stock email failed: %s, product %s",
                    (
                        f"guest {preference.email}"
                        if preference.user_id is None
                        else f"user {preference.user_id}"
                    ),
                    product.pk,
                )
                RestockNotification.objects.filter(
                    pk=preference.pk, notified_at=claimed_at
                ).update(notified_at=None)

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
    """A sellable variant of a product.

    ``price`` NULL means the product's own price applies; ``stock`` is
    deliberately inert — ``products.stock`` stays the only order-time
    authority; the SKU has no generation path, so the unique constraint is the
    concurrency authority.
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
    """Audit ledger for every inventory mutation: a stock change without a
    movement row is a bug.
    """

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


class RestockNotification(models.Model):
    """A customer's opt-in to one back-in-stock email for one product.

    One row per owner shape (account x-or guest address, enforced by a DB
    check constraint); the unique constraints are the race authority, and a
    spent ``notified_at`` stamp keeps one email per stock cycle until a
    sell-out re-arms the row.
    """

    # NULL means a GUEST opt-in whose owner is ``email``; CASCADE drops an
    # account's opt-ins, while guest rows die with the product.
    user = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="restock_notifications",
        null=True,
        blank=True,
    )
    # Guest address, stored normalized (strip + lower). Account rows keep ""
    # and never NULL — the XOR constraint reads the empty/non-empty split.
    email = models.EmailField(max_length=254, blank=True, default="")
    product = models.ForeignKey(
        products,
        on_delete=models.CASCADE,
        related_name="restock_notifications",
    )
    active = models.BooleanField(default=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "product"],
                name="uniq_user_product_restock_optin",
            ),
            # Guests need their own key: SQL NULLs are distinct, so (user,
            # product) never collides — get_or_create relies on this constraint.
            models.UniqueConstraint(
                fields=["email", "product"],
                condition=models.Q(user__isnull=True),
                name="uniq_guest_email_product_restock_optin",
            ),
            # An opt-in has exactly one owner: account (user set) xor guest
            # (email set).
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, email="")
                    | models.Q(user__isnull=True, email__gt="")
                ),
                name="restock_owner_xor_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=["product", "active", "notified_at"],
                name="restock_trigger_idx",
            ),
        ]
        verbose_name = "Restock notification"
        verbose_name_plural = "Restock notifications"

    def __str__(self):
        state = "active" if self.active else "opted out"
        owner = self.user_id if self.user_id is not None else self.email
        return f"{owner} -> {self.product_id} ({state})"


class StockReservation(models.Model):
    """A time-limited hold on units of a product for an in-flight checkout.

    Reserved inventory is the sum of active holds' ``quantity`` and
    available-to-sell is derived, never stored; the schema carries only the
    state the checkout/reconciliation writers transition.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CONVERTED = "converted", "Converted"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"

    product = models.ForeignKey(
        products,
        on_delete=models.CASCADE,
        related_name="stock_reservations",
    )
    # String FK to avoid a circular import: orders.models already imports
    # products.models, so products must not import orders at load.
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="stock_reservations",
    )
    # Nullable because guest checkouts have no account; NULL means the hold
    # belongs to a guest. The order FK beside it is always present.
    owner = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="stock_reservations",
        null=True,
        blank=True,
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
        # One hold per (order, product): a duplicate would double-count reserved
        # stock, and this constraint is the race authority for retried checkouts.
        constraints = [
            models.UniqueConstraint(
                fields=["order", "product"],
                name="uniq_order_product_reservation",
            ),
        ]
        # Serves the reconciler's sweep (status=active AND expires_at<=now).
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

        Read at call time so env/test overrides apply — a module-level constant
        would freeze the first-seen value.
        """
        return timezone.now() + timedelta(seconds=settings.RESERVATION_TTL)

    def __str__(self):
        return f"{self.product.name}: {self.quantity} reserved ({self.status})"

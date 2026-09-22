from datetime import timedelta
import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.text import slugify

import logging

# A dedicated channel name (mirrors common.notifications/ops.alerts) so
# deployments can route restock-send failures independently in log tooling.
logger = logging.getLogger("products.restock")


# SPEC-17-08 [R-17.21]: "File upload abuse -> Type/size validation". The
# type half is ImageField's Pillow verification (a non-image payload never
# validates); the size half is this ceiling, enforced before anything is
# written to storage. Env-driven with a 5 MB default because the right cap
# is a per-deployment trade-off between image quality and abuse budget, not
# a code constant. Read through django.conf.settings (not a module-level
# literal) so deployments can override it without a code change and tests
# can exercise the override.
def _max_upload_bytes():
    return settings.MAX_UPLOAD_MB * 1024 * 1024


def validate_image_size(image):
    """Reject image files above MAX_UPLOAD_MB with a field-level error.

    Attached as a model-field validator so DRF's ModelSerializer copies it
    onto the API form field (400 with the uniform error envelope) and
    Django's ModelForm (the admin change/add pages) picks it up too — one
    enforcement point covering every entry surface. The size is read via
    seek/tell on the underlying file handle: upload wrappers vary (BytesIO
    under the 2.5 MB in-memory threshold, TemporaryFile past it) and only
    some expose .size themselves, while File wrappers always keep the
    handle positioned at the start of the payload.
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
        null=True,
        validators=[validate_image_size],
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
            # [SPEC-19-4] Back-in-stock trigger ([R-19.11]): fires on the
            # exact crossing the notification is about — stock arriving
            # at a positive count from zero (PositiveBigIntegerField
            # makes below-zero impossible, but the guard keeps the
            # crossing definition self-contained). The inverse crossing
            # (positive -> 0) re-arms spent rows: the product sold out
            # again, so last cycle's notification is relevant again and
            # the NEXT restock re-mails. Never raises: a notification
            # outage must not fail the inventory write, mirroring the
            # alert/dispatch log-only contracts.
            previous_stock = locked.stock - delta
            if locked.stock > 0 and previous_stock <= 0:
                self._notify_back_in_stock(locked)
            elif locked.stock == 0 and previous_stock > 0:
                self._rearm_restock_notifications(locked)

    @staticmethod
    def _rearm_restock_notifications(product):
        """[SPEC-19-4] Sell-out re-arm (stock positive -> 0): clear the
        spent stamp on the product's active preferences so the NEXT
        0 -> positive crossing re-mails the opted-in customers. One
        stock cycle, one email. Fires beside the restock notify site and
        never raises (the caller guards the crossings already).
        """
        RestockNotification.objects.filter(
            product=product, active=True, notified_at__isnull=False
        ).update(notified_at=None)

    @staticmethod
    def _notify_back_in_stock(product):
        """Email every armed opt-in for ``product`` via the single send
        path, marking each row spent (``notified_at``) exactly once.

        Runs INSIDE adjust_stock's atomic block (rollback-together, the
        StockMovement pattern): the email hand-off happens pre-commit —
        the documented in-process substrate cost, identical to every
        other dispatch site — while the spent-stamp commits with the
        stock change, so a rollback cannot leave a notified row paired
        with stock that does not exist. ``update()`` (not per-row save)
        makes the mark exact even under concurrent adjustments: a row is
        stamped only when the UPDATE itself lands, and the notified_at
        filter in the same statement re-checks armament at write time.
        """
        from common import notifications

        armed = RestockNotification.objects.filter(
            product=product,
            active=True,
            notified_at__isnull=True,
        ).select_related("user", "product")
        for preference in armed:
            try:
                notifications.send_email(
                    "back_in_stock",
                    {"product": product, "frontend_url": settings.FRONTEND_URL},
                    f"Back in stock: {product.name}",
                    preference.user.email,
                )
            except Exception:
                # Log-only: one user's SMTP failure must not abort the
                # loop for the remaining opt-ins (and the inventory write
                # itself is unaffected by contract).
                logger.exception(
                    "Back-in-stock email failed: user %s, product %s",
                    preference.user_id,
                    product.pk,
                )
                continue
            RestockNotification.objects.filter(
                pk=preference.pk, notified_at__isnull=True
            ).update(notified_at=timezone.now())

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


class RestockNotification(models.Model):
    """A customer's opt-in to one back-in-stock email for one product
    ([R-19.11], spec 19.1 "Back-in-stock event -> Optional opt-in
    notification", SPEC-19-4).

    Lifecycle: created active when the customer opts in while the product
    is out of stock (an opt-in to a stocked product is meaningless). The
    restock trigger emails them once and stamps ``notified_at`` — the row
    keeps active=True but is spent, so a second restock without an
    intervening sell-out never re-mails (no duplicate sends). Semantics of
    the flags and the stock cycle:
    - ``active=False`` is an explicit opt-OUT: no mail is ever sent for
      the row until the customer opts back in (which flips active=True
      and clears notified_at, arming a fresh notification);
    - re-arm on subsequent zero-out: the sell-out crossing (stock
      positive -> 0) clears ``notified_at`` on the product's active
      rows, so the next 0 -> positive restock mails the same customer
      again — one email per stock cycle, the natural reading of "notify
      me when it's back";
    - a customer wanting a fresh mail without a sell-out opts out and
      back in (the opt-in clears notified_at).

    Unique per (user, product): at most one preference row per pair, the
    DB constraint is the concurrency authority (two concurrent opt-ins
    race to one row — conventions.md forbids check-then-act). Placement
    in products/ (not a new app): the write trigger is
    ``products.adjust_stock`` and every other customer-facing mount lives
    under the products/cart URL namespaces; a one-model feature does not
    earn an app.
    """

    user = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="restock_notifications",
    )
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
        ]
        indexes = [
            # The restock trigger's exact lookup: armed (active, unnotified)
            # prefs for one product.
            models.Index(
                fields=["product", "active", "notified_at"],
                name="restock_trigger_idx",
            ),
        ]
        verbose_name = "Restock notification"
        verbose_name_plural = "Restock notifications"

    def __str__(self):
        state = "active" if self.active else "opted out"
        return f"{self.user_id} -> {self.product_id} ({state})"


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

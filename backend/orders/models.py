from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q, Sum

from common.money import quantize_money
from products.models import products

# Named imports: a plain ``from . import state`` would be shadowed inside the
# Order class body by its address ``state`` field.
from .state import (
    FULFILMENT_STATUS_CHOICES,
    PAYMENT_METHOD_CHOICES,
    PAYMENT_METHOD_COD,
    PAYMENT_METHOD_PREPAID,
    PAYMENT_STATUS_CHOICES,
    STATUS_CHOICES,
    STATUS_EVENT_TRIGGERS,
    register_transition_preconditions,
)


def default_currency():
    """Store-config-driven currency for new money-bearing rows.

    A callable default so a deployment can retune DEFAULT_CURRENCY without a
    code change; the 0008 backfill stamps existing rows with 'INR', so
    migrations stay deterministic.
    """
    return settings.DEFAULT_CURRENCY


class Coupon(models.Model):
    # Carries NO merchandise discount: it waives the order's shipping charge.
    # A single symbol, so the choice value and the checkout comparison cannot
    # drift apart.
    FREE_SHIPPING = "free_shipping"

    DISCOUNT_TYPES = [
        ("percentage", "Percentage"),
        ("fixed", "Fixed Amount"),
        (FREE_SHIPPING, "Free Shipping"),
    ]

    code = models.CharField(max_length=50, unique=True)

    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPES)

    discount_value = models.DecimalField(max_digits=10, decimal_places=2)

    minimum_order_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )

    maximum_discount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    active = models.BooleanField(default=True)

    valid_from = models.DateTimeField()

    valid_until = models.DateTimeField()

    usage_limit = models.PositiveIntegerField(null=True, blank=True)

    used_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.code


class Order(models.Model):
    """A checkout-created purchase.

    The sequential ``id`` stays the internal key while ``order_number`` is the
    customer-facing reference (a guest checkout keys on it plus
    ``guest_token``). A row is EITHER an account order or a guest order, never
    a hybrid, and the Meta constraints below make that a database fact.
    """

    # Single-sourced in orders.state; kept for existing consumers.
    STATUS_CHOICES = STATUS_CHOICES

    # Minted inside create_order's atomic block; nullable by design (the 0006
    # backfill fills existing rows), and the unique index doubles as the
    # generation concurrency authority.
    order_number = models.CharField(
        max_length=20,  # 17 for ORD-YYYY-XXXXXXXX + headroom for format drift
        null=True,
        blank=True,
        unique=True,
    )

    # Nullable because a guest checks out without an account; CASCADE stays
    # right for account orders, and a guest row has no account to cascade from.
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="orders",
        null=True,
        blank=True,
    )

    # The guest's own identity, validated on the anonymous checkout path;
    # blank '' keeps account orders at one unambiguous value.
    guest_email = models.EmailField(
        max_length=254,
        blank=True,
        default="",
    )

    # The guest's retrieval credential: possession is the ONLY authorization to
    # read a guest order (there is no session to check), so it must be
    # unguessable and globally unique - two orders sharing a token would make
    # the lookup ambiguous. NULL for account orders, so the unique index stays
    # safe.
    guest_token = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
    )

    full_name = models.CharField(max_length=150)

    phone = models.CharField(max_length=15)

    address = models.TextField()

    city = models.CharField(max_length=100)

    state = models.CharField(max_length=100)

    pincode = models.CharField(max_length=10)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    # The lifecycle split into explicit dimensions; ``status`` above remains the
    # compat surface and writers keep these in sync via
    # orders.state.LEGACY_STATUS_DIMENSIONS. No db_index until measured query
    # patterns ask for one.
    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default="pending",
        help_text="Payment dimension of the lifecycle (spec 10.2).",
    )
    fulfilment_status = models.CharField(
        max_length=20,
        choices=FULFILMENT_STATUS_CHOICES,
        default="unfulfilled",
        help_text="Fulfilment dimension of the lifecycle (spec 10.2).",
    )

    # Prepaid (capture before fulfilment) or COD (captured at/after delivery);
    # the machine reads it to pick the shipped-precondition variant. No
    # checkout input exists yet.
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default=PAYMENT_METHOD_PREPAID,
        help_text="How the order intends to pay (spec 10.1 COD mandate).",
    )

    coupon = models.ForeignKey(
        Coupon, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders"
    )

    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    total_amount = models.DecimalField(max_digits=10, decimal_places=2)

    # The amount is the money record and the method its label: it stays exactly
    # as priced even if the method is retired. SET_NULL, not PROTECT, so
    # retiring an option never blocks on the orders that used it.
    shipping_method = models.ForeignKey(
        "shipping.ShippingMethod",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )

    # Frozen label, so hard-deleting a ShippingMethod cannot erase a historical
    # order's delivery option; DEACTIVATION is how options are retired. Set once
    # at checkout and never mutated; empty when no shipping was configured.
    shipping_method_code = models.CharField(
        max_length=40,  # ShippingMethod.code width, so the label always fits
        blank=True,
        default="",
    )

    # Zero is a real charge both before shipping was configured and on a
    # free-shipping order, told apart by whether shipping_method is set.
    shipping_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    # total_amount and discount_amount are denominated in this code; the
    # gateway payload and serializers read it here instead of assuming INR.
    currency = models.CharField(
        max_length=3,  # ISO 4217 code width
        default=default_currency,
    )

    razorpay_order_id = models.CharField(
        max_length=100, blank=True, null=True, unique=True
    )
    razorpay_payment_id = models.CharField(
        max_length=100, blank=True, null=True, unique=True
    )

    # Set once by create_order when the client sent an Idempotency-Key header;
    # uniqueness is scoped per user (a reused key on another account is an
    # independent submission) and NULLs stay distinct. No expiry: the key lives
    # with the order it deduped.
    idempotency_key = models.CharField(
        max_length=128,
        null=True,
        blank=True,
    )

    # Each is NULL until the event happens, written once and never mutated;
    # historical rows stay NULL because their times are unknowable. No index
    # until a measured query pattern asks for one.
    paid_at = models.DateTimeField(null=True, blank=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # The spec 8.3 starting set; the remaining prescribed indexes are
        # satisfied by unique constraints and deliberately not duplicated.
        indexes = [
            models.Index(
                fields=["user", "-created_at"],
                name="orders_user_created_idx",
            ),
            models.Index(
                fields=["status", "created_at"],
                name="orders_status_created_idx",
            ),
        ]
        constraints = [
            # The concurrency authority for keyed checkout replays: create_order
            # probes under the user-row lock, and this is the last-resort
            # guarantee one user cannot hold two orders for one key.
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                name="orders_user_idem_key_uidx",
            ),
            # The guest twin: a guest row's user is NULL, so without this a
            # keyed guest replay would have no database authority at all.
            models.UniqueConstraint(
                fields=["guest_email", "idempotency_key"],
                condition=Q(user__isnull=True),
                name="orders_guest_idem_key_uidx",
            ),
            # The two-owner invariant as a database fact: account order or guest
            # order, never neither and never both.
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=True, guest_email__gt="", guest_token__isnull=False)
                    | Q(user__isnull=False, guest_email="", guest_token__isnull=True)
                ),
                name="orders_account_xor_guest_ck",
            ),
        ]

    def __str__(self):
        # `user` is nullable, so the label must not crash for a guest row.
        return f"Order #{self.id} - {self.customer_name}"

    @property
    def customer_name(self):
        """Who placed the order, for a surface that labels a row: the account's
        username, else the guest's email (the only identity a guest carries)."""
        if self.user_id is None:
            return self.guest_email
        return self.user.username

    @property
    def recipient(self):
        """Where order mail for this row goes: the account's email, else the
        guest email captured at checkout, so the order.paid notification never
        raises on a NULL ``user``."""
        if self.user_id is None:
            return self.guest_email
        return self.user.email

    @property
    def refundable_remaining(self):
        """Money still refundable against this order's captured payment.

        The ceiling is ``total_amount``: no Payment model exists, so an order's
        captured amount IS its total. Quantized on both sides to 2-dp money.
        """
        return quantize_money(self.total_amount) - Refund.refunded_total(self)


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")

    product = models.ForeignKey(products, on_delete=models.SET_NULL, null=True)

    product_name = models.CharField(max_length=200, default="")

    # Frozen identity snapshots, set once at checkout and never updated. No
    # variant-selection input exists yet, so ``sku`` snapshots empty and
    # ``variant_name`` mirrors the product name until a matched variant fills
    # both.
    sku = models.CharField(
        max_length=64,  # ProductVariant.sku width, so a later variant-matched
        default="",  # population source fits without another migration
    )

    variant_name = models.CharField(
        max_length=200,  # product_name width: it mirrors the product name
        default="",
    )

    price = models.DecimalField(max_digits=10, decimal_places=2)

    quantity = models.PositiveIntegerField()

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Denomination of price/subtotal, set once beside the amounts it labels.
    currency = models.CharField(
        max_length=3,  # ISO 4217 code width
        default=default_currency,
    )

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"


# The machine stays dependency-free, so the ORM-backed precondition callables
# live here and register into its extension hook at import; each returns a list
# of failure reasons (empty = met).


def _require_captured_payment(order):
    """Ship only after the money is real: a shipped order whose payment later
    fails is un-reconcilable.

    ``partially_refunded`` is real money too (the Refund row is what
    reconciliation reads) so the remainder may ship; refusing it would strand
    paid-for inventory with no operator route out. COD is paid at/after
    delivery, so it waives the capture and ships on the items check alone."""
    if order.payment_method == PAYMENT_METHOD_COD:
        return []
    if order.payment_status not in ("captured", "partially_refunded"):
        return [f"payment must be captured (is '{order.payment_status}')"]
    return []


def _require_items_to_ship(order):
    """The order must have fulfilment-ready items - no lines, nothing to ship."""
    if not order.items.exists():
        return ["order has no items to ship"]
    return []


register_transition_preconditions(
    "shipped", _require_captured_payment, _require_items_to_ship
)


class OrderStatusEvent(models.Model):
    """One immutable row per status transition.

    Every transition appends exactly one row in the same transaction
    (rollback-together); the save guard rejects any re-save and the admin is
    view-only, so no code path rewrites history. ``actor`` is SET_NULL so
    deleting a user never cascades into the audit trail, and ``from_status`` is
    NULL only for creation events. This is the transition trail; the
    privileged-action LogEntry trail complements it, never replaces it.
    """

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="status_events"
    )

    from_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        null=True,
        blank=True,
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
        related_name="order_status_events",
    )

    trigger = models.CharField(
        max_length=30,
        choices=STATUS_EVENT_TRIGGERS,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Newest first; the id breaks ties between events written in the same
        # transaction with equal timestamps.
        ordering = ("-created_at", "-id")
        verbose_name = "order status event"
        verbose_name_plural = "order status events"

    def save(self, *args, **kwargs):
        # Append-only: a pk means an update path that would rewrite history.
        if self.pk is not None:
            raise TypeError("OrderStatusEvent rows are append-only")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.order_id}: {self.from_status}->{self.to_status} ({self.trigger})"


class Refund(models.Model):
    """One refund issued against an order's captured payment.

    ``kind`` records the REQUEST (FULL cleared the remaining balance, PARTIAL
    left some money refundable), never the order's resulting state. ``status``
    is written twice in one transaction: PENDING while the gateway call is in
    flight, PROCESSED once the provider returns an id; a failed attempt rolls
    back and leaves no row.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSED = "processed", "Processed"

    class Kind(models.TextChoices):
        FULL = "full", "Full"
        PARTIAL = "partial", "Partial"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="refunds")

    amount = models.DecimalField(max_digits=10, decimal_places=2)

    reason = models.TextField()

    kind = models.CharField(max_length=10, choices=Kind.choices)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    # The provider's refund reference, nullable only for the PENDING write;
    # unique so one gateway refund is never recorded against two rows.
    gateway_refund_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        unique=True,
    )

    # SET_NULL, like OrderStatusEvent.actor: deleting a staff account must
    # never cascade into the financial trail.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_refunds",
    )

    # Replay identity scoped to the order: a retry after a timeout collapses
    # onto the refund already produced rather than paying twice. NULLs stay
    # distinct, so keyless attempts are unaffected.
    idempotency_key = models.CharField(
        max_length=128,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "refund"
        verbose_name_plural = "refunds"
        constraints = [
            # The concurrency authority for a keyed retry: the writer probes
            # under the Order lock, and this is the last-resort guarantee one
            # order cannot hold two refunds for one key.
            models.UniqueConstraint(
                fields=["order", "idempotency_key"],
                name="orders_refund_order_idem_uidx",
            ),
        ]

    @classmethod
    def refunded_total(cls, order):
        """Money already refunded against ``order``, quantized to 2 dp.

        Only PROCESSED rows count: a PENDING row is an attempt not yet
        settled, and a failed attempt left no row, so this is the money the
        provider has actually moved.
        """
        total = cls.objects.filter(
            order=order,
            status=cls.Status.PROCESSED,
        ).aggregate(total=Sum("amount"))["total"]
        return quantize_money(total if total is not None else Decimal("0.00"))

    def __str__(self):
        return f"Refund #{self.pk} {self.amount} ({self.kind})"


# A customer request is a different lifecycle from Refund, deliberately a
# separate table: a Refund PENDING row means the gateway call is in flight,
# while a pending request means staff have not decided yet. Loading both onto
# one row would let a request masquerade as money already moving.


class RefundRequestStatus(models.TextChoices):
    """The decision vocabulary for a customer's refund request.

    Module level so the partial unique index below can be built before the
    model class exists; the model aliases it as ``RefundRequest.Status``.
    """

    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


REFUND_REQUEST_STATUS_CHOICES = RefundRequestStatus.choices


class RefundRequest(models.Model):
    """One customer's request for a refund on an order.

    The row is the ASK, never the money: filing one writes no ``Refund`` and
    moves no ``payment_status``. ``requester`` and ``decided_by`` are SET_NULL
    so deleting an account cannot erase the financial trail, and ``items``
    holds order-item ids (empty = the whole order), validated at the view.
    """

    Status = RefundRequestStatus

    order = models.ForeignKey(
        Order,
        # CASCADE: a request is a record OF one order, with no referent once
        # that order is gone.
        on_delete=models.CASCADE,
        related_name="refund_requests",
    )

    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        # SET_NULL, never CASCADE: deleting an account must not cascade into
        # the financial trail.
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="refund_requests",
    )

    reason = models.TextField()

    # JSON list of order-item ids; empty = the whole order. No per-element DB
    # constraint is possible on SQLite, so the view validates every id.
    items = models.JSONField(default=list, blank=True)

    status = models.CharField(
        max_length=20,
        choices=REFUND_REQUEST_STATUS_CHOICES,
        default=RefundRequestStatus.PENDING,
    )

    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_refund_requests",
    )

    decided_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "refund request"
        verbose_name_plural = "refund requests"
        constraints = [
            # One pending request per order: the view probes under the Order
            # lock, and this partial index is the last-resort guarantee. Scoped
            # to ``pending`` so a decided request releases the order.
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status=RefundRequestStatus.PENDING),
                name="orders_refundrequest_pending_uidx",
            ),
        ]

    def __str__(self):
        return f"Refund request #{self.pk} ({self.status})"


# The return has its OWN lifecycle dimension (requested, approved, rejected,
# received, inspected, closed), deliberately not wired into orders.state: that
# module is audited and holds the order status and payment dimensions only.


class ReturnStatus(models.TextChoices):
    """The return dimension's vocabulary, as a choices enum.

    Module level because the machine table and the partial unique index are
    built from these values before the model class exists; the model aliases it
    as ``ReturnRequest.Status``.
    """

    REQUESTED = "requested", "Requested"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    RECEIVED = "received", "Received"
    INSPECTED = "inspected", "Inspected"
    CLOSED = "closed", "Closed"


RETURN_STATUS_CHOICES = ReturnStatus.choices

# The legal edges: ``requested`` is the only branching point, and "rejected" is
# a decision rather than a stage - a rejected request never becomes a return
# that arrived.
RETURN_ALLOWED_TRANSITIONS = {
    ReturnStatus.REQUESTED: {ReturnStatus.APPROVED, ReturnStatus.REJECTED},
    ReturnStatus.APPROVED: {ReturnStatus.RECEIVED},
    ReturnStatus.RECEIVED: {ReturnStatus.INSPECTED},
    ReturnStatus.INSPECTED: {ReturnStatus.CLOSED},
    ReturnStatus.REJECTED: set(),
    ReturnStatus.CLOSED: set(),
}


def return_transition_allowed(old_status, new_status):
    """Whether a return request may move from ``old_status`` to ``new_status``.

    Mirrors ``orders.state.transition_allowed``, including its self-transition
    replay rule; separate so the audited order machine cannot acquire a
    return's vocabulary.
    """
    return new_status == old_status or new_status in RETURN_ALLOWED_TRANSITIONS.get(
        old_status, set()
    )


# A customer may hold only ONE of these per order, enforced by the partial
# unique constraint below; a closed or rejected request releases the order.
RETURN_OPEN_STATUSES = (
    ReturnStatus.REQUESTED,
    ReturnStatus.APPROVED,
    ReturnStatus.RECEIVED,
    ReturnStatus.INSPECTED,
)


def _allowed_from(status):
    """The statuses ``status`` may move to, as one human-readable clause.

    One formatter for both refusal messages (the save guard and the admin
    form), so the operator is never told two different things. ``"nothing"``
    rather than an empty string for a terminal status.
    """
    allowed = ", ".join(sorted(RETURN_ALLOWED_TRANSITIONS.get(status, set())))
    return allowed or "nothing"


def _reject_oversized_value(model, field_name, value):
    """Raise when ``value`` is a string wider than the column it is written to.

    A per-app copy of ``shipping.models._reject_oversized``: SQLite does not
    enforce a ``varchar(n)`` width while Postgres raises ``DataError``, so an
    over-length save would pass the suite and 500 in production. The bound is
    read off the field so it cannot drift from the schema; only ``str`` is
    measured.
    """
    max_length = getattr(model._meta.get_field(field_name), "max_length", None)
    if max_length is None or not isinstance(value, str):
        return
    if len(value) > max_length:
        raise ValueError(
            f"{model.__name__}.{field_name} must be at most {max_length} characters"
        )


def _reject_oversized_return_values(instance):
    """Apply :func:`_reject_oversized_value` to every bounded column.

    Called from ``ReturnRequest.save``, the write boundary the admin form
    funnels into; the queryset paths that bypass ``save`` are covered by the
    same helper in ``ReturnRequestQuerySet``.
    """
    for field in type(instance)._meta.concrete_fields:
        if not isinstance(field, models.CharField):
            continue
        _reject_oversized_value(
            type(instance), field.name, getattr(instance, field.attname)
        )


class ReturnRequestQuerySet(models.QuerySet):
    """A ``ReturnRequest`` queryset that will not move a status behind the machine.

    ``update()`` and ``bulk_update()`` never reach the admin's checked writer,
    so both are refused; the width gate rides those two paths but NOT
    ``bulk_create()``, an ORM-only gap documented rather than gated.
    """

    def update(self, **kwargs):
        if "status" in kwargs:
            raise ValueError(
                "ReturnRequest.status is moved only through the admin change "
                "form, which checks the return machine first; a bulk update "
                "cannot."
            )
        # The width gate rides here too: a bulk update writes straight to the
        # column.
        for field_name, value in kwargs.items():
            _reject_oversized_value(ReturnRequest, field_name, value)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        """Refuse a status move here too, and width-gate the rest.

        Also refused here (not just in ``update``) because the internal route is
        a Django implementation detail, and Django wraps the operation in
        ``transaction.atomic(savepoint=False)``, so raising before the block
        opens keeps the refusal recoverable.
        """
        fields = list(fields)
        if "status" in fields:
            raise ValueError(
                "ReturnRequest.status is moved only through the admin change "
                "form, which checks the return machine first; a bulk update "
                "cannot."
            )
        for obj in objs:
            for field_name in fields:
                _reject_oversized_value(
                    ReturnRequest, field_name, getattr(obj, field_name)
                )
        return super().bulk_update(objs, fields, batch_size=batch_size)


class ReturnRequest(models.Model):
    """One customer's request to send an order back.

    The ASK moves no money: it creates no ``Refund``, changes no
    ``payment_status`` and touches no ``total_amount``, and approval is a staff
    decision. Ownership is read off the order rather than stored twice, and
    guest requests are deliberately not offered (the spec routes returns under
    the account section).
    """

    class ReasonCode(models.TextChoices):
        # A code from a bounded vocabulary, not free text: codes are what staff
        # filter and report on; ``reason_note`` carries the customer's words.
        DAMAGED = "damaged", "Damaged in transit"
        DEFECTIVE = "defective", "Defective product"
        WRONG_ITEM = "wrong_item", "Wrong item received"
        NOT_AS_DESCRIBED = "not_as_described", "Not as described"
        CHANGED_MIND = "changed_mind", "Changed mind"

    # The module-level vocabulary, aliased so callers reach it the same way as
    # ``Shipment.Status`` and ``Refund.Status``.
    Status = ReturnStatus

    order = models.ForeignKey(
        Order,
        # CASCADE: a return request is a record OF one order, with no referent
        # once that order is gone.
        on_delete=models.CASCADE,
        related_name="return_requests",
    )

    status = models.CharField(
        # Width is enforced at every write boundary below because SQLite
        # would not.
        max_length=20,
        choices=RETURN_STATUS_CHOICES,
        default="requested",
    )

    reason_code = models.CharField(
        max_length=30,
        choices=ReasonCode.choices,
    )

    # TextField: the customer's own words have no honest bound, so there is no
    # width to enforce (the same reason ``Order.address`` is unbounded).
    reason_note = models.TextField(
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "return request"
        verbose_name_plural = "return requests"
        constraints = [
            # One live request per order: the writer probes under the Order
            # lock, and this partial index makes the second INSERT raise
            # IntegrityError. Scoped to open statuses so a closed request
            # releases the order.
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status__in=RETURN_OPEN_STATUSES),
                name="orders_returnrequest_open_uidx",
            ),
        ]

    objects = ReturnRequestQuerySet.as_manager()

    def save(self, *args, **kwargs):
        """Refuse a width violation and an illegal status edge, then write.

        The width guard catches direct ORM writes SQLite would accept and
        Postgres would reject; the edge guard makes "no illegal transition" a
        property of the model. Neither is a lock - the admin form's
        ``select_for_update()`` is the critical section and this is the
        backstop; bulk paths that never reach ``save`` are refused in
        ``ReturnRequestQuerySet``.
        """
        _reject_oversized_return_values(self)
        if self.pk is not None:
            stored = (
                type(self)
                ._default_manager.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )
            if stored is not None and not return_transition_allowed(
                stored, self.status
            ):
                raise ValueError(
                    f"ReturnRequest #{self.pk} cannot move from '{stored}' to "
                    f"'{self.status}'. Allowed from '{stored}': "
                    f"{_allowed_from(stored)}."
                )
        return super().save(*args, **kwargs)

    @property
    def is_open(self):
        """Whether this request is still being worked (see RETURN_OPEN_STATUSES)."""
        return self.status in RETURN_OPEN_STATUSES

    def __str__(self):
        # No `order` traversal: this renders per row in the admin grid, the CSV
        # export and LogEntry.
        return f"Return request #{self.pk} ({self.status})"


class PaymentEvent(models.Model):
    """One delivery of one payment-provider webhook event.

    Every delivery is recorded: the unique ``event_id`` (not a check-then-write)
    is the replay authority, ``outcome`` says what the handler decided,
    ``order`` is SET_NULL so a refusal outlives its lookup, and ``payload``
    keeps the decoded event exactly as delivered. ``amount`` is the store's
    2-dp Decimal, NULL when the event carries none.
    """

    class Outcome(models.TextChoices):
        APPLIED = "applied", "Applied"
        REFUSED = "refused", "Refused"
        RECORDED = "recorded", "Recorded"

    # The provider's own event name. No `choices`: the provider may add event
    # types at any time, and a delivery it considers real must be recordable
    # even with no writer; the behaviour vocabulary is `outcome`.
    event_id = models.CharField(
        max_length=100,
        unique=True,
    )

    event_type = models.CharField(
        max_length=50,
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_events",
    )

    # The gateway payment the event is about (a refund event carries the one it
    # returned money against). Blank rather than NULL: "" says "the event named
    # no payment" without claiming one.
    gateway_payment_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )

    payload = models.JSONField()

    outcome = models.CharField(
        max_length=10,
        choices=Outcome.choices,
        default=Outcome.RECORDED,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Newest first, like OrderStatusEvent: the trail is read most recent.
        ordering = ("-created_at", "-id")
        verbose_name = "payment event"
        verbose_name_plural = "payment events"

    def __str__(self):
        return f"{self.event_type} {self.event_id} ({self.outcome})"

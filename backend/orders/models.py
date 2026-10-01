from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q, Sum
from django.contrib.auth.models import User

from common.money import quantize_money
from products.models import products

# [R-10.1] The order machine's constants live in orders.state (single
# source); models, admin and views all import the same objects. Named
# imports: a plain ``from . import state`` would be shadowed inside the
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
    """[R-8.11] Store-config-driven currency for new money-bearing rows.

    A callable default rather than a hardcoded literal so a deployment can
    retune the store via the DEFAULT_CURRENCY env setting without a code
    change, while every order/item row still carries an explicit currency
    beside its amounts. Migrations stay deterministic regardless: the 0008
    backfill stamps existing rows with the literal 'INR' they were minted
    under, never with whatever the migrating environment's config says.
    """
    return settings.DEFAULT_CURRENCY


class Coupon(models.Model):

    DISCOUNT_TYPES = [
        ('percentage', 'Percentage'),
        ('fixed', 'Fixed Amount'),
    ]

    code = models.CharField(
        max_length=50,
        unique=True
    )

    discount_type = models.CharField(
        max_length=20,
        choices=DISCOUNT_TYPES
    )

    discount_value = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    minimum_order_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    maximum_discount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True
    )

    active = models.BooleanField(
        default=True
    )

    valid_from = models.DateTimeField()

    valid_until = models.DateTimeField()

    usage_limit = models.PositiveIntegerField(
        null=True,
        blank=True
    )

    used_count = models.PositiveIntegerField(
        default=0
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.code


class Order(models.Model):
    """A checkout-created purchase.

    [R-8.4]/[R-8.5] Identifier-exposure strategy: the sequential ``id`` stays
    the internal key (URL/admin primary key, no URL changes); ``order_number``
    (ORD-YYYY-NNNNNN, per-year sequence) is the customer-facing reference the
    serializer exposes read-only. Guest checkout (SPEC-1-B04) keys on
    ``order_number`` plus the ``guest_token`` below -- the pk never leaves
    server-side routing.

    Two owners, never a hybrid (spec line 74: the guest's role is to "browse
    products and optionally check out without an account"; spec 9.3 binds a
    checkout session "to the correct customer or guest session"). A row is
    EITHER an account order (``user`` set, both guest columns empty) OR a
    guest order (``user`` NULL, ``guest_email`` + ``guest_token`` set), and
    the Meta constraints below are what make that a database fact rather than
    a convention: an order nobody can reach is unrecoverable for the store
    and unreachable for the customer who placed it.
    """

    # [R-10.1] Single-sourced in orders.state; the class attribute stays so
    # existing consumers (ops dashboard, admin filters) keep working.
    STATUS_CHOICES = STATUS_CHOICES

    # [R-8.4] Customer-facing reference, minted inside create_order's atomic
    # block. Nullable by design: checkout (the only production writer) always
    # sets it, non-checkout ORM creations keep working, and the 0006 data
    # migration backfills every pre-existing row, so the column is fully
    # populated after migrating. The unique index doubles as the concurrency
    # authority for generation (IntegrityError retry) and as the spec 8.3
    # "order number" index.
    order_number = models.CharField(
        max_length=20,  # 15 for ORD-YYYY-NNNNNN + headroom for format drift
        null=True,
        blank=True,
        unique=True,
    )

    # [R-1.13] SPEC-1-B04: nullable because a guest checks out without an
    # account (spec line 74). CASCADE is unchanged and still right: it is the
    # policy for the rows it applies to (an account's orders die with the
    # account), and a guest row simply has no account to cascade from - the
    # store keeps the sale and the fulfilment queue keeps seeing it.
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="orders",
        null=True,
        blank=True,
    )

    # [R-1.13] The guest's own identity, and the address the store
    # acknowledges the sale to. Required input on the anonymous checkout path
    # (the view refuses an anonymous submission without one) and validated
    # there through django.core.validators, so the column holds a real address
    # rather than whatever the client typed. blank/default '' keeps the
    # account-order case at a single, unambiguous value (no null-vs-empty
    # drift); EmailField's 254 is the RFC 5321 maximum, so the width is the
    # same one on SQLite and Postgres.
    guest_email = models.EmailField(
        max_length=254,
        blank=True,
        default="",
    )

    # [R-1.13] The guest's retrieval credential: possession of this value is
    # the ONLY authorization to read a guest order (there is no session to
    # check), so it must be unguessable - minted with `secrets`, never
    # `random`, and never sequential. 32 bytes of ``secrets.token_urlsafe``
    # entropy render as 43 URL-safe characters; the 64-char column leaves
    # headroom for a future format change without another migration.
    #
    # Unique, and unique GLOBALLY rather than per user: the lookup is "the
    # order whose token is this", so two orders sharing a token would make
    # that lookup ambiguous. NULL for account orders - which is why the
    # uniqueness is safe at all (NULLs stay distinct in a unique index on
    # both engines), the same shape ``razorpay_order_id`` above already uses.
    guest_token = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
    )

    full_name = models.CharField(
        max_length=150
    )

    phone = models.CharField(
        max_length=15
    )

    address = models.TextField()

    city = models.CharField(
        max_length=100
    )

    state = models.CharField(
        max_length=100
    )

    pincode = models.CharField(
        max_length=10
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )

    # [R-10.1] SPEC-10-01a: the lifecycle split into explicit dimensions
    # (spec 10.2). Additive by design: ``status`` above remains the compat
    # surface; the writers keep these in sync with every status change
    # (orders.state.LEGACY_STATUS_DIMENSIONS is the mapping). null=False
    # with defaults so every row always answers both questions. No
    # db_index: the §8.3 prescribed starting set (SPEC-8-05, the Meta
    # indexes below) deliberately does not include these columns — indexes
    # come from measured query patterns, per the same policy as the event
    # timestamps.
    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default='pending',
        help_text="Payment dimension of the lifecycle (spec 10.2).",
    )
    fulfilment_status = models.CharField(
        max_length=20,
        choices=FULFILMENT_STATUS_CHOICES,
        default='unfulfilled',
        help_text="Fulfilment dimension of the lifecycle (spec 10.2).",
    )

    # [R-10.2] SPEC-10-04: how the order intends to pay — prepaid (the
    # store's current and default behavior: gateway capture before
    # fulfilment) or cash on delivery (captured at/after delivery). The
    # machine reads it to pick the shipped-precondition variant below.
    # null=False with a literal default: every row always answers the
    # question, and the 0014 backfill stamps pre-existing rows 'prepaid' —
    # exactly the behavior those rows lived under. No checkout input
    # exists yet: accepting COD at checkout is a checkout-section row.
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default=PAYMENT_METHOD_PREPAID,
        help_text="How the order intends to pay (spec 10.1 COD mandate).",
    )

    coupon = models.ForeignKey(
        Coupon,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='orders'
    )

    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    # [R-8.11] Currency rides every money column (spec 8.3: "Store the
    # currency alongside the amount. Do not assume all currencies use two
    # decimal places."): total_amount and discount_amount are denominated
    # in this code, and the gateway payload and serializers read it from
    # here instead of assuming INR.
    currency = models.CharField(
        max_length=3,  # ISO 4217 code width
        default=default_currency,
    )

    razorpay_order_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True, null=True, unique=True)

    # [R-9.3.14] SPEC-9-01: header-keyed checkout idempotency. Set once by
    # create_order when the client sent an Idempotency-Key header; NULL for
    # keyless submissions. Uniqueness is scoped per user (a reused key on
    # another account is an independent submission, never an existence
    # leak), and NULLs stay distinct in the constraint, so keyless rows
    # can never collide. No expiry: the key lives with the order row it
    # deduped, so a retry collapses onto the original outcome forever.
    idempotency_key = models.CharField(
        max_length=128,
        null=True,
        blank=True,
    )

    # [R-8.16] Business-event timeline (spec 8.3 "Timestamps": store distinct
    # timestamps for each business event; do not overload a generic
    # ``updated_at`` to represent one). Each column is NULL until the event
    # happens, is written exactly once by the code path that performs the
    # event, and is never mutated once set (writers guard with an is-none /
    # or-check; serializers and the admin expose them read-only). UTC
    # storage comes from USE_TZ=True, not from the columns. paid_at and
    # cancelled_at have live writers (verify_payment / admin cancel);
    # refunded_at has one too (the SPEC-1-05 refund seam); fulfilled_at,
    # shipped_at and delivered_at are the named pattern the fulfilment
    # section writes -- no writer touches them yet. Historical rows stay
    # NULL on purpose: the events predate the columns, their times are
    # unknowable, so no backfill is possible. No index yet: spec 8.3 says
    # add indexes from measured query patterns, and none of these dates is
    # queried with status today.
    paid_at = models.DateTimeField(null=True, blank=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        # [R-8.17] SPEC-8-05: spec 8.3 "Indexes" starting set (2557
        # "Customer ID and order creation date", newest-first matching the
        # customer order-history sort; 2567 "Frequently queried status/date
        # combinations"). The remaining prescribed starting indexes are
        # satisfied by constraints and deliberately NOT duplicated:
        # order_number (2555) and the payment provider references
        # razorpay_order_id / razorpay_payment_id (2559 — no Payment model
        # exists, the provider references live on this table) each carry
        # unique=True, whose backing unique index serves those lookups
        # (PRAGMA index_list origin 'u'), and user_id keeps its FK
        # auto-index for user-only joins.
        indexes = [
            models.Index(
                fields=['user', '-created_at'],
                name='orders_user_created_idx',
            ),
            models.Index(
                fields=['status', 'created_at'],
                name='orders_status_created_idx',
            ),
        ]
        constraints = [
            # [R-9.3.14]/[R-9.3.19] The concurrency authority for keyed
            # checkout replays: create_order probes under the user-row lock
            # (fast path), and this constraint is the last-resort guarantee
            # that one user can never hold two orders for one key. The
            # backing index also serves the replay probe lookup.
            models.UniqueConstraint(
                fields=['user', 'idempotency_key'],
                name='orders_user_idem_key_uidx',
            ),
            # [R-1.13] The guest twin of the constraint above. A guest row's
            # `user` is NULL, and NULLs stay distinct in the constraint
            # above - so without this one, a keyed guest replay would have
            # NO database authority at all and two guest orders could answer
            # one key (two payable orders for one submission). Scoped to the
            # guest rows by the condition, so the account pair above keeps
            # answering for them and keyless rows (NULL key) never collide.
            models.UniqueConstraint(
                fields=["guest_email", "idempotency_key"],
                condition=Q(user__isnull=True),
                name="orders_guest_idem_key_uidx",
            ),
            # [R-1.13] The two-owner invariant, as a database fact: a row is
            # an account order (both guest columns empty) or a guest order
            # (both set), never neither and never both. "Neither" would be an
            # order no customer and no staff lookup could ever reach, and
            # "both" would put two competing owners on one sale.
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=True, guest_email__gt="", guest_token__isnull=False)
                    | Q(user__isnull=False, guest_email="", guest_token__isnull=True)
                ),
                name="orders_account_xor_guest_ck",
            ),
        ]

    def __str__(self):
        # [R-1.13] `user` is nullable now, so the label must be - not crash -
        # for a guest row: this string is what the admin change list, the
        # admin CSV export and LogEntry render for every order.
        return f"Order #{self.id} - {self.customer_name}"

    @property
    def customer_name(self):
        """Who placed the order, for a surface that labels a row.

        The account's username, else the guest's email (the only identity a
        guest checkout carries). One place, so the admin grid, the CSV export
        and ``__str__`` can never disagree about what a guest row is.
        """
        if self.user_id is None:
            return self.guest_email
        return self.user.username

    @property
    def recipient(self):
        """Where order mail for this row goes.

        The account's email, else the guest email captured at checkout. Read
        by the order.paid notification (common.notifications) so the guest
        confirmation reaches the guest instead of raising on ``user`` being
        NULL, and so the store never invents an address of its own.
        """
        if self.user_id is None:
            return self.guest_email
        return self.user.email

    @property
    def refundable_remaining(self):
        """Money still refundable against this order's captured payment.

        The ceiling is ``total_amount``: no Payment model exists in this
        schema, so an order's captured amount IS its total, and the refund
        writer only ever runs once ``payment_status`` says the payment was
        captured. Quantized on both sides, so the balance is a 2-dp money
        Decimal and never a carry-over of extra decimal places (conventions.md
        money rule, same as every other amount in this file).
        """
        return quantize_money(self.total_amount) - Refund.refunded_total(self)


class OrderItem(models.Model):

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='items'
    )

    product = models.ForeignKey(
        products,
        on_delete=models.SET_NULL,
        null=True
    )

    product_name = models.CharField(
        max_length=200,
        default=''
    )

    # [R-8.13] Frozen identity snapshots (spec 8.3 "Historical snapshots"):
    # set once at checkout from the catalogue state the customer bought and
    # never updated afterwards -- no save path mutates them; the customer
    # serializer and the admin inline expose them read-only. Population
    # source today (documented): no variant-selection input exists at
    # checkout (CartItem is product-only -- picking rides SPEC-3-21/SPEC-6-08)
    # and ``products`` carries no product-level SKU, so ``sku`` snapshots
    # empty and ``variant_name`` mirrors the product name; a matched
    # variant's SKU/name replaces both once selection input exists.
    sku = models.CharField(
        max_length=64,  # ProductVariant.sku width, so a later variant-matched
        default=''      # population source fits without another migration
    )

    variant_name = models.CharField(
        max_length=200,  # product_name width: it mirrors the product name
        default=''
    )

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    quantity = models.PositiveIntegerField()

    subtotal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    # [R-8.11] Denomination of the price/subtotal money columns: set once
    # beside the amounts it labels, same store-config default as the parent
    # order, so per-line amounts stay unambiguous if the store currency
    # ever changes between order generations.
    currency = models.CharField(
        max_length=3,  # ISO 4217 code width
        default=default_currency,
    )

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"


# ——— [R-10.19]/[R-10.14] SPEC-10-03: built-in shipped preconditions ——————
# The machine (orders.state) stays dependency-free, so the ORM-backed
# precondition callables live here beside the model they inspect and
# register into the state's extension hook at import. Every callable
# returns a list of human-readable failure reasons (empty = met); the
# writers evaluate them through state.precondition_failures only.

def _require_captured_payment(order):
    """Ship only after the money is real: a shipped order whose payment
    later fails is un-reconcilable, and the payment dimension (spec 10.2) is
    exactly where that truth lives.

    [R-1.14] SPEC-1-05: ``partially_refunded`` is real money too - a capture
    minus a recorded refund, and the Refund row is precisely what
    reconciliation reads - so the remainder may ship. Refusing it would strand
    paid-for inventory permanently: cancel is illegal from ``confirmed`` and
    ``mark_shipped`` is the only path to ``fulfilled``, so there is no
    operator route out. ``pending`` / ``authorized`` / ``failed`` are still
    refused; none of them is captured money.

    [R-10.2] SPEC-10-04 COD variant: a cash-on-delivery order is paid at/
    after delivery, so demanding a capture before shipping would make COD
    orders unfulfillable — the precondition waives for COD (items-only via
    _require_items_to_ship) and the capture point is the delivery surface
    (spec 10.1 mandates handling COD orders but prescribes no capture
    timing; the reading is documented in changes.md)."""
    if order.payment_method == PAYMENT_METHOD_COD:
        return []
    if order.payment_status not in ("captured", "partially_refunded"):
        return [f"payment must be captured (is '{order.payment_status}')"]
    return []


def _require_items_to_ship(order):
    """Spec 10.3's mark-shipped example: the order must have
    fulfilment-ready items — no lines, nothing to ship."""
    if not order.items.exists():
        return ["order has no items to ship"]
    return []


register_transition_preconditions(
    "shipped", _require_captured_payment, _require_items_to_ship
)


class OrderStatusEvent(models.Model):
    """[R-10.12]/[R-10.17]/[R-10.18] One immutable row per status transition.

    Every legal order-status transition (checkout creation, verify_payment,
    the admin change form, the admin bulk actions, the 9-07 admin JSON seam)
    appends exactly one row in the SAME transaction as the transition it
    records: a rolled-back writer leaves no event behind, and a committed
    event can never lack its transition ([R-10.18] rollback-together, pinned
    per writer in tests). Append-only by design: the save guard below
    rejects any pk-set re-save, and the admin registration is view-only
    (no add/change/delete permission), so no code path can rewrite history.
    ``actor`` is SET_NULL — deleting a user account must never cascade into
    the audit trail (and verify_payment's events carry actor NULL by design:
    the customer payment flow has no admin actor, the trigger names the
    source). ``from_status`` is NULL exactly for creation events (no source
    state). No backfill: rows predate the table and the transitions that
    produced them are unknowable, so historical orders legitimately have
    no trail before their next live transition.

    This is the TRANSITION trail; the privileged-action LogEntry trail
    (common.audit.log_api_action, SPEC-7-01) separately records who performed
    which admin operation — the two complement, never replace, each other.
    """

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='status_events'
    )

    from_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        null=True,
        blank=True,  # NULL only on creation events (no source state)
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
        related_name='order_status_events',
    )

    trigger = models.CharField(
        max_length=30,
        choices=STATUS_EVENT_TRIGGERS,
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        # Newest first: the admin surface (and any future consumer) reads
        # the trail most-recent-first; the id breaks ties between events
        # written in the same transaction with equal timestamps.
        ordering = ("-created_at", "-id")
        verbose_name = "order status event"
        verbose_name_plural = "order status events"

    def save(self, *args, **kwargs):
        # [R-10.18] Append-only: a pk on the instance means an update path
        # (re-save or bulk-style edit via save), which would rewrite
        # history — reject it outright.
        if self.pk is not None:
            raise TypeError("OrderStatusEvent rows are append-only")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.order_id}: {self.from_status}->{self.to_status} ({self.trigger})"


class Refund(models.Model):
    """[R-1.14] One refund issued against an order's captured payment.

    Spec section 1 puts "Payment gateway, refunds, webhooks" in the payments
    row of the system overview, and the finance operator's job is to
    "reconcile payments, refunds and financial reports" ([1.31]) - which
    needs a row per refund: what was returned, why, who returned it and which
    refund the provider acknowledged.

    ``kind`` records the REQUEST, not the order's resulting state: a refund
    is FULL when it cleared the order's remaining balance (an omitted amount,
    or an explicit one equal to what was left) and PARTIAL when it left some
    money still refundable. The order's payment dimension is the derived
    answer to "what is left", never a field stored here.

    ``status`` is written twice inside one transaction by the refund writer:
    PENDING the moment the attempt starts (so the requested money is a real
    row under the order's locks while the gateway call is in flight) and
    PROCESSED once the provider returned a refund id. A failed attempt leaves
    NO row - the transaction rolls back - so the two values are the whole
    vocabulary: an outstanding refund is the absence of a row, which the
    provider's own record is the authority for.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSED = "processed", "Processed"

    class Kind(models.TextChoices):
        FULL = "full", "Full"
        PARTIAL = "partial", "Partial"

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='refunds'
    )

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    reason = models.TextField()

    kind = models.CharField(
        max_length=10,
        choices=Kind.choices
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    # The provider's own refund reference, nullable only for the in-flight
    # PENDING write above and unique so one gateway refund can never be
    # recorded against two rows (the reconciliation join key).
    gateway_refund_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        unique=True,
    )

    # SET_NULL for the same reason as OrderStatusEvent.actor: deleting a
    # staff account must never cascade into the financial trail.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_refunds',
    )

    # [R-9.3.14]-shaped replay identity, scoped to the order: a client that
    # retries the same refund after a timeout must collapse onto the refund
    # it already produced rather than pay the customer twice. NULLs stay
    # distinct in the constraint, so keyless attempts are unaffected.
    idempotency_key = models.CharField(
        max_length=128,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "refund"
        verbose_name_plural = "refunds"
        constraints = [
            # The concurrency authority for a keyed retry: the writer holds the
            # Order row lock while it probes for a replay and binds the key,
            # and this constraint is the last-resort guarantee that one order
            # can never hold two refunds for one key. Its backing index also
            # serves the replay probe.
            models.UniqueConstraint(
                fields=['order', 'idempotency_key'],
                name='orders_refund_order_idem_uidx',
            ),
        ]

    @classmethod
    def refunded_total(cls, order):
        """Money already refunded against ``order``, quantized to 2 dp.

        Only PROCESSED rows count: a PENDING row is an attempt whose
        transaction has not settled yet, and a failed attempt left no row at
        all, so this sum is exactly the money the provider has moved.
        """
        total = cls.objects.filter(
            order=order,
            status=cls.Status.PROCESSED,
        ).aggregate(total=Sum("amount"))["total"]
        return quantize_money(total if total is not None else Decimal("0.00"))

    def __str__(self):
        return f"Refund #{self.pk} {self.amount} ({self.kind})"


class PaymentEvent(models.Model):
    """[R-1.15] SPEC-1-06: one delivery of one payment-provider webhook event.

    Spec section 1 puts "Payment gateway, refunds, webhooks" in the payments
    row of the system overview and spec 11.2 makes webhook signature
    verification and duplicate-delivery handling payment requirements in their
    own right. Until this table existed, the only statement this store had
    about a payment was what the customer's browser said after checkout - so a
    dropped callback left a paid order pending forever, and a forged callback
    could claim one. Razorpay's own record is the authority; this table is the
    store's side of hearing it.

    Every delivery is recorded, whatever it turns out to be worth:

    * ``event_id`` (the provider's per-delivery id) is UNIQUE, and that
      constraint - not a check-then-write - is the replay authority. A second
      delivery of the same event collides, the handler rolls back, and the
      store returns success without a second effect. The unique index is the
      guarantee; the IntegrityError-retry loop in ``orders.webhooks`` is how
      it is honored, exactly as ``create_payment`` honors the payment-intent
      uniqueness (conventions.md:17).
    * ``outcome`` is what the handler decided. APPLIED means money moved onto
      the order; REFUSED means a validly-signed event that must not move it
      (unknown order, mismatched payment reference, wrong amount, an edge the
      machine does not declare); RECORDED means the delivery was genuine but
      this store has no writer for it (a refund notification, an event type
      the vocabulary does not name). Refusals and records are kept, not
      dropped: "the provider says this happened and we did nothing about it"
      is the row reconciliation reads.
    * ``order`` is SET_NULL and nullable because a refusal must outlive the
      lookup that produced it - an event naming an order this store has never
      seen is exactly the row an operator needs, and a financial trail must
      not cascade away with a deleted order row.
    * ``payload`` keeps the decoded event exactly as delivered. The
      event-type-specific columns beside it are the reconcilable answers, but
      they are chosen by this store's writers: when a provider adds a field or
      a future handler needs one this batch did not anticipate, the delivered
      event is the only record that still holds it.

    ``amount`` is the store's Decimal money (never provider minor units,
    never a float), quantized through ``common.money`` by the writer. It is
    NULL for an event that carries no amount.
    """

    class Outcome(models.TextChoices):
        APPLIED = "applied", "Applied"
        REFUSED = "refused", "Refused"
        RECORDED = "recorded", "Recorded"

    # The provider's own event name (see WEBHOOK_* in orders.state). No
    # `choices`: the provider may add event types at any time, and a delivery
    # it considers real must be recordable even when this store has no writer
    # for it. The behaviour vocabulary is `outcome`, not this column.
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
        related_name='payment_events',
    )

    # The gateway payment the event is about (a refund event carries the
    # payment it returned money against). Blank rather than NULL: every
    # delivery has a text answer here, and "" says "the event named no
    # payment" without claiming one.
    gateway_payment_id = models.CharField(
        max_length=100,
        blank=True,
        default='',
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

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        # Newest first for the same reason OrderStatusEvent is: the trail is
        # read most-recent-first.
        ordering = ("-created_at", "-id")
        verbose_name = "payment event"
        verbose_name_plural = "payment events"

    def __str__(self):
        return f"{self.event_type} {self.event_id} ({self.outcome})"


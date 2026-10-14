"""The ONE place an order's sale is committed to inventory.

Two writers can turn a pending order into a paid one: the customer callback
(``orders.views.payment.verify_payment``) and the signed gateway delivery
(``orders.webhooks._apply_captured``). They used to carry SEPARATE copies of
the same three statements - convert the holds, decrement ``products.stock``,
write the ``StockMovement`` ledger row - and the copies drifted: the callback
decremented and the webhook did not, so a customer who closed the tab after
paying left an order whose money was captured, whose holds read CONVERTED, and
whose stock was never decremented. There was nothing to reconcile against,
because the reconciler the webhook docstring deferred the work to does not
exist.

So the work lives here, once, and both writers call it. The point of a shared
service is not fewer lines: it is that the two paths cannot DISAGREE about what
committing a sale means, which is the property a duplicated block loses
silently (the same failure SPEC-1-B01 recorded for the refund decrement).

Three properties this function owes its callers, and why each one is here:

1. **It re-reads and re-locks what it reads.** The caller has already locked
   the Order row, but the service locks it AGAIN on its own instance and takes
   the product locks itself in ascending-pk order (conventions.md:16). The
   ascending order is the deadlock-avoidance constraint SPEC-12-02 established
   for the callback: unordered ``filter(id__in=...)`` lets the plan choose the
   lock sequence, so two carts naming the same products in different insertion
   orders could deadlock across the handoff. The callback's own pre-check keeps
   its locks (they produce the 409 the customer sees); this one is what makes the
   service correct for a caller that has NOT pre-checked - which is exactly the
   webhook, and is why the webhook can no longer confirm an order whose stock
   is gone.
2. **It is idempotent against its own callers.** The guard is the order's own
   payment dimension, read under the Order lock: a capture that some earlier
   committed writer already recorded means this order's sale is already on the
   books, and a second decrement would be the double decrement. The set is
   ``orders.state.CAPTURED_SALE_PAYMENT_STATUSES`` - DERIVED from the payment
   transition table rather than listed here, and deliberately NOT the returns
   module's ``CAPTURED_MONEY_PAYMENT_STATUSES``, which answers a different
   question - because both writers set ``payment_status`` in the SAME
   transaction as this call, so "the money is already recorded against this
   order" and "this order's sale is already committed" are the same fact read
   two ways. The guard fails SAFE: a caller that somehow reached here with a
   captured payment moves no stock.
3. **It writes the ledger row or it did not happen.** ``products.stock`` is the
   only stock authority, and SPEC-6-02 [6.5.17] makes a stock change without a
   movement row a bug: every decrement lands in ``StockMovement`` with the order
   as its reference, a negative delta and the real post-decrement
   ``stock_after``.

Stock insufficiency raises :class:`StockUnavailable` rather than returning a
flag, and writes NOTHING before raising - the re-check runs before the first
mutation, so a caller that catches it has a clean transaction to release holds
and record its own audit row in. What a caller does with that is its own
protocol and deliberately not this module's business: the callback answers 409
and leaves the order retryable, while the webhook cannot un-capture money the
gateway already holds, so it records the conflict and leaves the order for the
reconciliation the PaymentEvent row exists to feed.

**The linked cart is emptied HERE, past the idempotency gate
(TIER1-WEBHOOK-CART).** It used to be true that only the browser callback
could empty the caller's cart - it has the session cookie - because ``Cart``
was keyed only by ``session_id`` with no FK to an Order, so a server-to-server
webhook had no way to name this customer's cart, and a returning customer saw
a phantom full cart after a webhook capture. ``Cart.order`` (SET_NULL, bound
at checkout) now names the cart from the order, so this service - the ONE
place both capture writers already share - clears the linked cart in the same
transaction as the sale. Idempotent by construction: the clear runs only past
the payment-dimension guard, so a replayed capture returns before it and a
second clear never happens; an order with no linked cart (pre-link rows, guest
rows) is skipped silently. The whole linked cart is emptied, not just the
ordered lines: checkout snapshots every cart line into the order, so the
linked cart's contents ARE the order's.
"""

import logging

from django.db import transaction

# TIER1-WEBHOOK-CART: the linked-cart clear needs the Cart model. String
# FK reference on Cart.order ("orders.Order") means no import cycle.
from cart.models import Cart
from common.models import AuditEvent
from products.models import StockMovement, StockReservation, products

from .models import Coupon, Order
from .state import CAPTURED_SALE_PAYMENT_STATUSES

logger = logging.getLogger(__name__)


class StockUnavailable(Exception):
    """A line of the order cannot be sold from the stock that exists.

    Carries the three numbers a caller needs to record the conflict: which
    product, how many the order wanted, and how many are actually there (0 when
    the product row is gone entirely - ``OrderItem.product`` is
    ``on_delete=SET_NULL``, so a deleted product leaves the line unsellable
    rather than absent). Raised BEFORE any mutation, so the caller's
    transaction is untouched.
    """

    def __init__(self, product_id, requested, available):
        self.product_id = product_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"product {product_id}: {requested} requested, {available} available"
        )


def commit_order_sale(order, *, actor=None):
    """Commit ``order``'s sale: convert holds, decrement stock, ledger the rows.

    Idempotent with respect to both of this store's capture writers: if the
    order's payment dimension already records a capture, this returns without
    moving stock, so a delivery arriving after the customer callback confirmed
    the same payment is a no-op rather than a second decrement. Callers must
    invoke this INSIDE the same ``transaction.atomic()`` block that writes the
    order's capture state - that shared transaction is what makes the guard in
    point 2 sound, since the marker and the decrement then commit or roll back
    together.

    Raises :class:`StockUnavailable` if a line cannot be sold, having written
    nothing. ``actor`` is the ``StockMovement.created_by`` for each row; the
    payment writers both pass nothing, because a capture is a system mutation
    and SPEC-6-02 pins it as such (the buyer is not the actor who moved stock).
    """
    with transaction.atomic():
        # Re-locked rather than trusting the caller's instance: the guard below
        # reads committed payment state, and a caller holding a half-mutated
        # order would otherwise decide this order's sale is already committed
        # (or is not) from its own uncommitted edits.
        locked_order = Order.objects.select_for_update().get(pk=order.pk)

        if locked_order.payment_status in CAPTURED_SALE_PAYMENT_STATUSES:
            logger.info(
                "Sale commit for order %s skipped: payment already %s; "
                "an earlier capture committed this sale",
                locked_order.id,
                locked_order.payment_status,
            )
            return

        # The items are re-read here, not taken from the caller, so a service
        # call can never commit a SUBSET of what the order is: the line set and
        # the decrement set are the same list by construction.
        order_items = list(locked_order.items.all())
        product_ids = [item.product_id for item in order_items]

        # Ascending pk: one total order over the lock set, so two carts naming
        # the same products cannot deadlock across the handoff.
        locked_products = {
            product.id: product
            for product in products.objects.select_for_update()
            .filter(id__in=product_ids)
            .order_by("id")
        }

        # The sufficiency re-check, before the first mutation. R-12.12: the sale
        # never re-learns availability from a reservation - it asks the stock
        # authority under its own locks.
        #
        # Demand is SUMMED per product, not checked line by line. Checkout
        # mints one line per product (the cart is unique on cart+product), so
        # the two agree for every order that flow produces - but ``OrderItem``
        # carries no such constraint, and a per-line check would wave through
        # two lines of 2 against a stock of 3 and then drive it negative on the
        # decrement pass. ``products.stock`` is unsigned, so that is not a small
        # number, it is a failed write on PostgreSQL. Summing is the same answer
        # for the ordinary order and the only safe one for the rest.
        required = {}
        for item in order_items:
            required[item.product_id] = required.get(item.product_id, 0) + item.quantity
        for product_id, wanted in required.items():
            product = locked_products.get(product_id)
            if product is None or product.stock < wanted:
                raise StockUnavailable(
                    product_id,
                    wanted,
                    product.stock if product else 0,
                )

        # The coupon's usage is consumed HERE rather than by each writer, for
        # the same reason the decrement is: two copies of one statement drift,
        # and a webhook-confirmed order that never consumed its coupon's usage
        # lets a single-use coupon be burned again and again.
        #
        # It is a RECORD of what happened, not a gate. The discount is already
        # baked into ``order.total_amount`` - the capture reconciles the
        # gateway's amount against exactly that number - so a confirmed sale
        # HAS consumed this coupon whether or not the coupon is still inside
        # its ``usage_limit`` today. Re-validating here would ask the wrong
        # question and would refuse to record a discount that was already
        # given away; validity is the CALLER's precondition (verify_payment
        # answers 409 on an invalidated coupon), and a webhook has no client to
        # answer.
        #
        # AFTER the sufficiency re-check, so a line that cannot be sold does
        # not burn usage for a sale that did not happen.
        #
        # The Coupon is locked by the service itself rather than trusted from
        # the caller, for the same reason the products are. It is taken AFTER
        # the product locks so both capture writers share the one total order
        # Order -> products -> coupon, which is the order verify_payment
        # already used for its own coupon check; a service that took it first
        # would invert that against the callback and open a deadlock.
        #
        # ``select_for_update().get()`` cannot miss: ``Order.coupon`` is
        # ``on_delete=SET_NULL``, so deleting this coupon issues an UPDATE
        # against the Order row this transaction already holds locked. That
        # delete therefore blocks until this block commits, and either the
        # row exists or ``coupon_id`` was already NULL before the lock.
        if locked_order.coupon_id is not None:
            coupon = Coupon.objects.select_for_update().get(pk=locked_order.coupon_id)
            coupon.used_count += 1
            coupon.save(update_fields=["used_count"])
            # The increment above is a RECORD, never a gate (see the block
            # comment): when it pushes usage PAST the limit — a race between
            # cart-add validation and capture, or a limit reached while this
            # payment was in flight — the over-redemption is made VISIBLE in
            # the audit trail instead of silent in the usage counter. Written
            # inside this same atomic block so it rolls back with the capture,
            # and savepoint-wrapped so a failing audit sink can never poison
            # the transaction or roll back captured money (the trailing-write
            # never-raise contract orders.webhooks uses for the notification
            # enqueue).
            if (
                coupon.usage_limit is not None
                and coupon.used_count > coupon.usage_limit
            ):
                try:
                    with transaction.atomic():
                        AuditEvent.record(
                            AuditEvent.EventType.PAYMENT_COUPON_OVER_LIMIT,
                            order=locked_order,
                            detail={
                                "coupon_code": coupon.code,
                                "used_count": coupon.used_count,
                                "usage_limit": coupon.usage_limit,
                                # Named "prevention", not
                                # "recorded_not_prevented": the audit
                                # sanitizer drops any key whose normalized
                                # form contains a credential marker, and
                                # "recorded_not_prevented" contains "otp"
                                # ("notp"). The value carries the phrase.
                                "prevention": "recorded_not_prevented",
                            },
                        )
                except Exception:
                    logger.exception(
                        "over-limit coupon redemption audit write failed; "
                        "capture stays committed order=%s coupon=%s",
                        locked_order.id,
                        coupon.code,
                    )

        # Filtering on ACTIVE makes the conversion idempotent and retry-safe: a
        # hold an earlier failed attempt released is terminal and is never
        # resurrected into a sale, and an order with no holds (legacy, or the
        # post-failure retry) commits unchanged. A lapsed TTL is deliberately
        # NOT a conversion gate - expiry belongs to the SPEC-12-03 sweep, and a
        # paid order's holds must not be swept away.
        locked_order.stock_reservations.filter(
            status=StockReservation.Status.ACTIVE
        ).update(status=StockReservation.Status.CONVERTED)

        for item in order_items:
            product = locked_products[item.product_id]
            product.stock -= item.quantity
            product.save(update_fields=["stock"])
            StockMovement.objects.create(
                product=product,
                delta=-item.quantity,
                reason=StockMovement.Reason.SALE,
                stock_after=product.stock,
                note=f"Order #{locked_order.id}",
                created_by=actor,
            )

        # TIER1-WEBHOOK-CART: empty the cart that produced this order, in
        # this transaction (rollback-together with the sale) and only past
        # the payment-dimension guard above - a replayed capture returns
        # there and never reaches this line. The cart row is locked like the
        # other side-effect targets (conventions.md:16); the lock lands
        # after Order -> products -> coupon, the shared order both capture
        # writers already use, so no lock-order cycle is introduced.
        # filter().first() rather than the reverse OneToOne accessor: None
        # for an order with no linked cart (pre-link, guest) is a silent
        # skip, and an already-empty relation makes the DELETE a no-op, so
        # neither a missing nor a previously-cleared cart can raise.
        linked_cart = (
            Cart.objects.select_for_update().filter(order=locked_order).first()
        )
        if linked_cart is not None:
            linked_cart.items.all().delete()

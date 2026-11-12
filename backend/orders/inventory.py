"""The one place an order's sale is committed to inventory, shared by the
customer callback and the signed webhook so the two capture paths cannot
disagree about what committing a sale means.

It re-locks the order and products (ascending pk, for deadlock avoidance), is
idempotent on the order's captured-payment state, raises StockUnavailable
before writing anything, and empties the linked cart only past that gate."""

import logging

from django.db import transaction

# The string FK on Cart.order ("orders.Order") means no import cycle.
from cart.models import Cart
from common.models import AuditEvent
from products.models import StockMovement, StockReservation, products

from .models import Coupon, Order
from .state import CAPTURED_SALE_PAYMENT_STATUSES

logger = logging.getLogger(__name__)


class StockUnavailable(Exception):
    """A line of the order cannot be sold from the stock that exists.

    Carries the product, requested and available counts (0 when the product row
    is gone) and is raised before any mutation, so the caller's transaction is
    untouched.
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

    Idempotent on the order's captured-payment state and must run inside the
    caller's atomic block that writes that state; raises StockUnavailable having
    written nothing. ``actor`` is the ``StockMovement.created_by`` (payment
    captures are system mutations and pass none).
    """
    with transaction.atomic():
        # Re-locked rather than trusting the caller's instance: the guard reads
        # committed payment state, not a caller's uncommitted edits.
        locked_order = Order.objects.select_for_update().get(pk=order.pk)

        if locked_order.payment_status in CAPTURED_SALE_PAYMENT_STATUSES:
            logger.info(
                "Sale commit for order %s skipped: payment already %s; "
                "an earlier capture committed this sale",
                locked_order.id,
                locked_order.payment_status,
            )
            return

        # Re-read here, not taken from the caller, so the line set and the
        # decrement set are the same list by construction.
        order_items = list(locked_order.items.all())
        product_ids = [item.product_id for item in order_items]

        # Ascending pk: one total lock order, so two carts naming the same
        # products cannot deadlock across the handoff.
        locked_products = {
            product.id: product
            for product in products.objects.select_for_update()
            .filter(id__in=product_ids)
            .order_by("id")
        }

        # Sufficiency re-check before the first mutation, with demand SUMMED per
        # product: a per-line check would wave two lines of 2 through against a
        # stock of 3 and then drive the unsigned column negative on PostgreSQL.
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

        # Coupon usage is a RECORD, not a gate: the discount is already baked
        # into total_amount, so re-validating would refuse to record a discount
        # already given away; validity is the CALLER's precondition. Taken after
        # the product locks to keep the shared Order -> products -> coupon order
        # both capture writers use.
        #
        # This get() cannot miss: Order.coupon is SET_NULL, so deleting the
        # coupon blocks on the Order row this transaction already holds locked.
        if locked_order.coupon_id is not None:
            coupon = Coupon.objects.select_for_update().get(pk=locked_order.coupon_id)
            coupon.used_count += 1
            coupon.save(update_fields=["used_count"])
            # When the above pushes usage past the limit, make the
            # over-redemption visible in the audit trail; the savepoint keeps a
            # failing audit sink from poisoning the transaction.
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
                                # Key is "prevention", not
                                # "recorded_not_prevented": the audit sanitizer
                                # drops keys containing "otp" ("notp"). The
                                # value carries the phrase.
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

        # Filtering on ACTIVE makes the conversion retry-safe; a lapsed TTL is
        # deliberately not a gate (expiry belongs to the sweep), so a paid
        # order's holds are not swept away.
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

        # Empty the linked cart in this transaction, only past the payment
        # guard above; the lock lands after Order -> products -> coupon so no
        # cycle is introduced, and filter().first() makes a missing or
        # already-cleared cart a no-op.
        linked_cart = (
            Cart.objects.select_for_update().filter(order=locked_order).first()
        )
        if linked_cart is not None:
            linked_cart.items.all().delete()

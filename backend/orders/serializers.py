from rest_framework import serializers

from .models import Coupon, Order, OrderItem, ReturnRequest

# ==================================
# Order Item Serializer
# ==================================


class OrderItemSerializer(serializers.ModelSerializer):
    subtotal = serializers.ReadOnlyField()

    class Meta:
        model = OrderItem

        fields = [
            "id",
            "product",
            "product_name",
            "sku",
            "variant_name",
            "price",
            "quantity",
            "subtotal",
            # [R-8.11] Denomination of price/subtotal — the label rides the
            # money wherever the money is exposed. Read-only: currency is
            # minted at creation from store config, never client-chosen.
            "currency",
        ]

        read_only_fields = [
            "id",
            "product",
            "product_name",
            "sku",
            "variant_name",
            "price",
            "subtotal",
            "currency",
        ]


# ==================================
# Order Serializer
# ==================================


class OrderSerializer(serializers.ModelSerializer):
    """[R-8.5] Identifier-exposure strategy: the sequential ``id`` stays the
    internal key -- it remains the URL/admin primary key and no URL changes.
    ``order_number`` (ORD-YYYY-NNNNNN, spec 8.3) is the read-only
    customer-facing reference, surfaced at checkout and on every order read.
    Guest checkout (SPEC-1-B04) keys on ``order_number`` + the order's own
    ``guest_token``; the pk never leaves server-side routing.

    ``guest_email`` rides the read because the staff surfaces answer "who is
    this order for" and a guest order has no ``user`` to answer with. The
    token itself is deliberately NOT a serializer field: it is the guest's
    credential, so it is returned exactly once, in the checkout response
    that minted it (views._checkout_response), and never by a list,
    detail or admin read.
    """

    items = OrderItemSerializer(many=True, read_only=True)
    coupon = serializers.StringRelatedField(read_only=True)
    # [R-1.07] SPEC-1-B05: the delivery option by its client-facing CODE, so
    # the storefront can echo the option it priced and never learns a row id.
    # Read-only like the money it rides with: checkout prices the option and
    # stores the amount, and no client may set either afterwards.
    shipping_method = serializers.SlugRelatedField(
        slug_field="code",
        read_only=True,
    )

    class Meta:
        model = Order

        fields = [
            "id",
            "order_number",
            "user",
            # [R-1.13] The guest's own address, so a guest order names a
            # customer on the staff reads that a customer order gets. Read
            # only like `user`: identity is settled at checkout, never
            # client-chosen afterwards. Empty string on an account order.
            "guest_email",
            "full_name",
            "phone",
            "address",
            "city",
            "state",
            "pincode",
            "status",
            # [R-10.1] The explicit lifecycle dimensions (spec 10.2) ride
            # every order read beside the legacy ``status`` they mirror.
            # Read-only like status itself: dimensions are machine-maintained.
            "payment_status",
            "fulfilment_status",
            # [R-10.2] SPEC-10-04: how the order intends to pay rides every
            # order read beside the dimensions. Read-only: the marker is
            # machine-maintained (checkout's COD input is a checkout-
            # section row; nothing client-writable today).
            "payment_method",
            "coupon",
            "discount_amount",
            "total_amount",
            # [R-1.07] SPEC-1-B05: what this order was charged to deliver it.
            # The amount is the money record (it stays exactly as priced even
            # if the method is retired later); the method is the label, and it
            # is null on an order priced when the store had no shipping
            # configured - which is a real zero charge, never a missing one.
            "shipping_method",
            "shipping_amount",
            # [R-8.13] The frozen delivery-option label. It rides every order
            # read so a hard-deleted ShippingMethod does not make a historical
            # order unreadable: `shipping_method` goes null, this keeps naming
            # the option the customer bought, and it is empty on an order
            # priced while no shipping was configured - so the two stay
            # tellable apart. Read-only like the amount it was priced with.
            "shipping_method_code",
            # [R-8.11] The denomination of total_amount/discount_amount is
            # exposed beside them (checkout, dedup replay, and order reads
            # all serialize through here). Read-only like the money itself.
            "currency",
            "items",
            "created_at",
            "updated_at",
            # [R-8.16] The business-event timeline rides every order read
            # (checkout, dedup replay, list/detail). Read-only like the
            # events themselves: a client can never claim an event happened.
            "paid_at",
            "fulfilled_at",
            "shipped_at",
            "delivered_at",
            "cancelled_at",
            "refunded_at",
        ]

        read_only_fields = [
            "id",
            "order_number",
            "user",
            "guest_email",
            "status",
            "payment_status",
            "fulfilment_status",
            "payment_method",
            "coupon",
            "discount_amount",
            "total_amount",
            "shipping_method",
            "shipping_amount",
            "shipping_method_code",
            "currency",
            "items",
            "created_at",
            "updated_at",
            "paid_at",
            "fulfilled_at",
            "shipped_at",
            "delivered_at",
            "cancelled_at",
            "refunded_at",
        ]


# ==================================
# Return Request Serializer
# ==================================


class ReturnRequestSerializer(serializers.ModelSerializer):
    """[R-1.16] SPEC-1-B07b: ONE customer-facing projection of a return request.

    This is the single representation every customer-facing returns read
    carries (list, detail, and the create confirmation SPEC-1-B07a already
    returns). SPEC-1-B07a built its confirmation body inline as a deliberate
    interim shape and left ownership of the real serializer to this task; the
    create view now drives it through this class, so there is ONE customer
    body to keep in step rather than two.

    THE OMISSIONS ARE THE CONTRACT, and they mirror B06's
    ``ShipmentTrackingSerializer`` (read that class's docstring for the same
    reasoning):

    * the ``order`` FOREIGN KEY is not exposed. Ownership is read off the order
      (``order__user``), so the pk is a server-side routing key only. The
      customer-facing handle is ``order_number``, the read-only reference
      ``OrderSerializer`` already treats as the customer-facing identifier
      ([R-8.5]).
    * no money rides this projection: no ``total_amount``, no
      ``payment_status``, no ``refundable_remaining``, no ``currency``.
      SPEC-1-B07a's guarantee - a return request moves no money and the money
      fields belong to the refund seam (SPEC-1-05) - is exactly why none of
      them is here. A return row carries none of them to begin with, and this
      serializer deliberately does NOT reach through the relation to pull them
      off the order.
    * ``internal_note`` and anything else staff-only is not a field on this
      serializer. ``ReturnRequest`` has no such column today, and the customer
      reason the customer IS entitled to see is ``reason_code`` +
      ``reason_note`` (the reason they typed). There is no staff annotation
      surface on this row to leak.

    Every field is ``read_only``: a ``ModelSerializer`` is writable by default,
    so this says so explicitly rather than trusting the view to be GET-only.
    That makes "a client can never write a status or a reason" a structural
    property of the projection, not a per-view promise.
    """

    order_number = serializers.CharField(source="order.order_number", read_only=True)

    class Meta:
        model = ReturnRequest
        fields = [
            "id",
            "order_number",
            "status",
            "reason_code",
            "reason_note",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


# ==================================
# Coupon Serializer
# ==================================


class CouponSerializer(serializers.ModelSerializer):
    class Meta:
        model = Coupon

        fields = [
            "id",
            "code",
            "discount_type",
            "discount_value",
            "minimum_order_amount",
            "maximum_discount",
            "active",
            "valid_from",
            "valid_until",
            "usage_limit",
            "used_count",
        ]

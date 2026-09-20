from rest_framework import serializers

from .models import Order, OrderItem, Coupon


# ==================================
# Order Item Serializer
# ==================================

class OrderItemSerializer(serializers.ModelSerializer):

    subtotal = serializers.ReadOnlyField()

    class Meta:
        model = OrderItem

        fields = [
            'id',
            'product',
            'product_name',
            'sku',
            'variant_name',
            'price',
            'quantity',
            'subtotal',
            # [R-8.11] Denomination of price/subtotal — the label rides the
            # money wherever the money is exposed. Read-only: currency is
            # minted at creation from store config, never client-chosen.
            'currency',
        ]

        read_only_fields = [
            'id',
            'product',
            'product_name',
            'sku',
            'variant_name',
            'price',
            'subtotal',
            'currency',
        ]


# ==================================
# Order Serializer
# ==================================

class OrderSerializer(serializers.ModelSerializer):
    """[R-8.5] Identifier-exposure strategy: the sequential ``id`` stays the
    internal key -- it remains the URL/admin primary key and no URL changes.
    ``order_number`` (ORD-YYYY-NNNNNN, spec 8.3) is the read-only
    customer-facing reference, surfaced at checkout and on every order read.
    Guest checkout (SPEC-3-02) will key on ``order_number``; the pk never
    leaves server-side routing.
    """

    items = OrderItemSerializer(
        many=True,
        read_only=True
    )
    coupon = serializers.StringRelatedField(
        read_only=True
    )

    class Meta:
        model = Order

        fields = [
            'id',
            'order_number',
            'user',
            'full_name',
            'phone',
            'address',
            'city',
            'state',
            'pincode',
            'status',
            # [R-10.1] The explicit lifecycle dimensions (spec 10.2) ride
            # every order read beside the legacy ``status`` they mirror.
            # Read-only like status itself: dimensions are machine-maintained.
            'payment_status',
            'fulfilment_status',
            # [R-10.2] SPEC-10-04: how the order intends to pay rides every
            # order read beside the dimensions. Read-only: the marker is
            # machine-maintained (checkout's COD input is a checkout-
            # section row; nothing client-writable today).
            'payment_method',

            'coupon',
            'discount_amount',
            'total_amount',
            # [R-8.11] The denomination of total_amount/discount_amount is
            # exposed beside them (checkout, dedup replay, and order reads
            # all serialize through here). Read-only like the money itself.
            'currency',

            'items',
            'created_at',
            'updated_at',
            # [R-8.16] The business-event timeline rides every order read
            # (checkout, dedup replay, list/detail). Read-only like the
            # events themselves: a client can never claim an event happened.
            'paid_at',
            'fulfilled_at',
            'shipped_at',
            'delivered_at',
            'cancelled_at',
            'refunded_at',
        ]

        read_only_fields = [
            'id',
            'order_number',
            'user',
            'status',
            'payment_status',
            'fulfilment_status',
            'payment_method',
            'coupon',
            'discount_amount',
            'total_amount',
            'currency',
            'items',
            'created_at',
            'updated_at',
            'paid_at',
            'fulfilled_at',
            'shipped_at',
            'delivered_at',
            'cancelled_at',
            'refunded_at',
        ]


# ==================================
# Coupon Serializer
# ==================================

class CouponSerializer(serializers.ModelSerializer):

    class Meta:
        model = Coupon

        fields = [
            'id',
            'code',
            'discount_type',
            'discount_value',
            'minimum_order_amount',
            'maximum_discount',
            'active',
            'valid_from',
            'valid_until',
            'usage_limit',
            'used_count',
        ]
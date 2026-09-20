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
        ]

        read_only_fields = [
            'id',
            'product',
            'product_name',
            'sku',
            'variant_name',
            'price',
            'subtotal',
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

            'coupon',
            'discount_amount',
            'total_amount',

            'items',
            'created_at',
            'updated_at',
        ]

        read_only_fields = [
            'id',
            'order_number',
            'user',
            'status',
            'coupon',
            'discount_amount',
            'total_amount',
            'items',
            'created_at',
            'updated_at',
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
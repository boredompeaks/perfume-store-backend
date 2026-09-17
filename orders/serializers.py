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
            'price',
            'quantity',
            'subtotal',
        ]

        read_only_fields = [
            'id',
            'product',
            'product_name',
            'price',
            'subtotal',
        ]


# ==================================
# Order Serializer
# ==================================

class OrderSerializer(serializers.ModelSerializer):

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
from rest_framework import serializers

from products.serializers import ProductSerializer

from .models import Cart, CartItem


class CartItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)

    class Meta:
        model = CartItem
        fields = [
            "id",
            "product",
            "quantity",
            "created_at",
        ]


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)

    coupon_code = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        # No `session_id`: the client already owns the session cookie, and
        # echoing the raw key back only widens who can capture it.
        fields = [
            "id",
            "items",
            "coupon_code",
            "created_at",
            "updated_at",
        ]

    def get_coupon_code(self, obj):
        # A method field keeps the nullable FK traversal explicit instead of
        # relying on DRF's source-chain None handling.
        return obj.coupon.code if obj.coupon_id else None

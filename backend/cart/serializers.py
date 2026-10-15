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
        # V-09/F-15 (Q-S1): no `session_id` here. The client already owns
        # the session cookie; echoing the raw key back only widens who can
        # capture it (logs, proxies, XSS exfil) with zero benefit.
        fields = [
            "id",
            "items",
            "coupon_code",
            "created_at",
            "updated_at",
        ]

    def get_coupon_code(self, obj):
        # The code is all the frontend needs to render the applied coupon
        # (R-9.3.5); a method field keeps the nullable FK traversal explicit
        # instead of relying on DRF's source-chain None handling.
        return obj.coupon.code if obj.coupon_id else None

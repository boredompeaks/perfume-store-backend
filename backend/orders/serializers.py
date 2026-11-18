from rest_framework import serializers

from common.money import quantize_money

from .models import Coupon, Order, OrderItem, RefundRequest, ReturnRequest


def money_string(value):
    """Render a money ``Decimal`` as the 2-dp string the JSON wire carries.

    DRF's JSON renderer encodes a bare ``Decimal`` as a float, dropping the
    trailing zero, so every money value leaves through this one formatter while
    the stored column stays a ``Decimal``.
    """
    return f"{quantize_money(value):f}"


class MoneyField(serializers.DecimalField):
    """Read-only money field whose representation is :func:`money_string`."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 10)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("read_only", True)
        super().__init__(**kwargs)

    def to_representation(self, value):
        return money_string(value)


class OrderItemSerializer(serializers.ModelSerializer):
    price = MoneyField()
    subtotal = MoneyField()

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


class OrderSerializer(serializers.ModelSerializer):
    """The order read projection: the sequential ``id`` stays the internal key
    while ``order_number`` is the customer-facing reference.

    The guest token is deliberately not a field: it is the guest's credential,
    returned exactly once in the checkout response that minted it, never by a
    list, detail or admin read.
    """

    items = OrderItemSerializer(many=True, read_only=True)
    coupon = serializers.StringRelatedField(read_only=True)
    # The option by its client-facing code, so the storefront never learns a row
    # id; read-only like the money it rides with.
    shipping_method = serializers.SlugRelatedField(
        slug_field="code",
        read_only=True,
    )
    discount_amount = MoneyField()
    total_amount = MoneyField()
    shipping_amount = MoneyField()

    class Meta:
        model = Order

        fields = [
            "id",
            "order_number",
            "user",
            # Identity is settled at checkout; empty on an account order.
            "guest_email",
            "full_name",
            "phone",
            "address",
            "city",
            "state",
            "pincode",
            "status",
            # Lifecycle dimensions beside the legacy ``status``; read-only.
            "payment_status",
            "fulfilment_status",
            "payment_method",
            "coupon",
            "discount_amount",
            "total_amount",
            # The amount is the money record and stays as priced even if the
            # method is retired; null method means a real zero charge.
            "shipping_method",
            "shipping_amount",
            # Frozen label: a hard-deleted ShippingMethod nulls the method but
            # this still names what the customer bought.
            "shipping_method_code",
            "currency",
            "items",
            "created_at",
            "updated_at",
            # Read-only: a client can never claim an event happened.
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


class CouponPreviewSerializer(serializers.Serializer):
    """The public coupon preview body.

    Projects the view's ``Decimal`` figures to the wire's 2-dp strings, so the
    preview and the checkout order cannot spell money differently.
    """

    coupon = serializers.CharField(read_only=True)
    subtotal = MoneyField()
    discount = MoneyField()
    final_total = MoneyField()


class ReturnRequestSerializer(serializers.ModelSerializer):
    """The single customer-facing projection of a return request (list, detail,
    and create confirmation).

    The order FK, money fields and staff-only columns are deliberately omitted,
    and every field is read-only, so ownership stays server-side and a client
    can never write a status or reason.
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


class RefundRequestSerializer(serializers.ModelSerializer):
    """The customer-facing refund-request projection, used both to validate the
    POST body and to render the 201.

    Explicit fields, never ``'__all__'``, with ``order``/``requester``/``status``
    read-only server-side facts; ``items`` is a ListField so a malformed body is
    a 400 rather than unvalidated JSON.
    """

    order_number = serializers.CharField(source="order.order_number", read_only=True)
    reason = serializers.CharField(trim_whitespace=True, allow_blank=False)
    items = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = RefundRequest
        fields = [
            "id",
            "order",
            "order_number",
            "requester",
            "reason",
            "items",
            "status",
            "decided_at",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "order",
            "order_number",
            "requester",
            "status",
            "decided_at",
            "created_at",
        ]

    def validate_items(self, value):
        """Every supplied id must be a line of the order being refunded."""
        order = self.context.get("order")
        if order is None:
            return value
        supplied = list(dict.fromkeys(value))
        owned = set(order.items.filter(pk__in=supplied).values_list("id", flat=True))
        if any(item_id not in owned for item_id in supplied):
            raise serializers.ValidationError("Every item must belong to this order")
        return supplied


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

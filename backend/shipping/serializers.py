"""Shipping serializers for the public estimate and the tracking read.

The rate table is staff configuration with no customer-facing write API, and
the amount rides as a decimal string so a JSON double cannot change the price.
"""

from rest_framework import serializers

from .models import Shipment, ShipmentEvent


class ShippingOptionSerializer(serializers.Serializer):
    """One delivery option priced for one destination.

    Read-only by construction; the response name ``method`` maps onto the
    quote's ``method_code`` so clients only ever choose an identifier.
    """

    method = serializers.SlugField(source="method_code")
    name = serializers.CharField(source="method_name")
    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    free_shipping = serializers.BooleanField()


class ShipmentEventSerializer(serializers.ModelSerializer):
    """One customer-visible step of a tracking trail.

    Two fields only: warehouse notes and the staff actor are deliberately
    absent from the customer contract.
    """

    status = serializers.CharField(source="to_status")
    occurred_at = serializers.DateTimeField(source="created_at")

    class Meta:
        model = ShipmentEvent
        fields = ["status", "occurred_at"]


class ShipmentTrackingSerializer(serializers.ModelSerializer):
    """What a customer is shown about one parcel.

    Customer-appropriate by spec: no address, recipient, money or
    ``internal_note``; the provider exposes only its code and rendered link.
    Every field is read-only, so a client can never post a tracking number.
    """

    order_number = serializers.CharField(source="order.order_number", read_only=True)
    provider = serializers.SerializerMethodField()
    tracking_url = serializers.CharField(read_only=True, allow_null=True)
    events = ShipmentEventSerializer(many=True, read_only=True)

    def get_provider(self, obj):
        """The provider's slug code, or ``None`` when the parcel is unlinked.

        A method field rather than ``source="provider.code"`` because DRF
        resolves a dotted source eagerly and would raise on the legitimate
        ``None``.
        """
        return obj.provider.code if obj.provider_id else None

    class Meta:
        model = Shipment
        fields = [
            "order_number",
            "status",
            "carrier",
            "provider",
            "tracking_number",
            "tracking_url",
            "estimated_delivery",
            "events",
        ]
        read_only_fields = fields

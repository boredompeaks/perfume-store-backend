"""Shipping serializers (SPEC-1-B05 [R-1.07], SPEC-1-B06 [R-1.08]).

One serializer for the one public shipping response a customer can get
(B05): a priced delivery option for a destination. The rate table itself is
staff configuration and is managed through the RBAC admin seam
(shipping/admin.py), not through a customer-facing write API - a client that
could post its own rates would be the SPEC-1-B04 P1 in a new place.

The amount rides as a decimal STRING (DRF's default for DecimalField), never
as a JSON number: a JSON number is a double in every browser, and a shipping
price that changes in the last paisa on the way to the screen is a money bug
with no trace in the backend.
"""

from rest_framework import serializers

from .models import Shipment, ShipmentEvent


class ShippingOptionSerializer(serializers.Serializer):
    """One delivery option priced for one destination.

    Read-only by construction: a plain Serializer with no create/update
    methods, so nothing here can be posted back to be trusted. The response
    names are mapped onto the quote's own fields so the client contract says
    ``method`` (an identifier, the only thing a client may ever choose) while
    the quote keeps the explicit ``method_code`` / ``method_name`` names.
    """

    method = serializers.SlugField(source="method_code")
    name = serializers.CharField(source="method_name")
    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    free_shipping = serializers.BooleanField()


class ShipmentEventSerializer(serializers.ModelSerializer):
    """[R-1.08] One customer-visible step of a tracking trail.

    Two fields, and that is the whole contract. Spec 6.9 line 2027 requires
    the customer tracking page to expose neither internal carrier credentials
    nor warehouse notes, so the source names are mapped onto customer names
    and ``actor`` (a staff account) is not among them: an operator's username
    is not something a customer needs to see a parcel move.
    """

    status = serializers.CharField(source="to_status")
    occurred_at = serializers.DateTimeField(source="created_at")

    class Meta:
        model = ShipmentEvent
        fields = ["status", "occurred_at"]


class ShipmentTrackingSerializer(serializers.ModelSerializer):
    """[R-1.08] What a customer is shown about ONE parcel.

    Everything here is customer-appropriate by spec 6.9 line 2027, and the
    omissions are the point rather than an oversight: there is no delivery
    address, no recipient name, no order total and no money, and
    ``internal_note`` is not on the list. The only order field carried is the
    reference the caller already supplied.

    One parcel, not one order: the view serializes this ``many=True`` under a
    ``shipments`` key so a split shipment (spec 6.9 line 2021) reports all of
    its parcels. Every field below is a fact about the parcel being serialized,
    which is why hoisting any of them to the envelope would have changed what
    the body says.

    Every field is read-only, which is the structural half of "a client can
    never post a tracking number": a ModelSerializer is writable by default,
    so this one says so explicitly rather than relying on the view being GET.
    """

    order_number = serializers.CharField(source="order.order_number", read_only=True)
    events = ShipmentEventSerializer(many=True, read_only=True)

    class Meta:
        model = Shipment
        fields = [
            "order_number",
            "status",
            "carrier",
            "tracking_number",
            "estimated_delivery",
            "events",
        ]
        read_only_fields = fields

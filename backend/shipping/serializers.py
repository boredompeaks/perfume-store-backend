"""Shipping serializers (SPEC-1-B05 [R-1.07]).

One serializer, for the one public shipping response: a priced delivery
option for a destination. The rate table itself is staff configuration and
is managed through the RBAC admin seam (shipping/admin.py), not through a
customer-facing write API - a client that could post its own rates would be
the SPEC-1-B04 P1 in a new place.

The amount rides as a decimal STRING (DRF's default for DecimalField), never
as a JSON number: a JSON number is a double in every browser, and a shipping
price that changes in the last paisa on the way to the screen is a money bug
with no trace in the backend.
"""

from rest_framework import serializers


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

from rest_framework import serializers

from .models import products


class ProductSerializer(serializers.ModelSerializer):
    """Public product surface (audit F-24 / V-19): an explicit whitelist
    instead of ``__all__``, so future model columns are never auto-exposed.
    Must stay identical to the set the API has always returned — pinned by
    ``test_serializer_field_set_is_pinned``."""

    class Meta:
        model = products
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "price",
            "size",
            "stock",
            "category",
            "image",
            "created_at",
        ]

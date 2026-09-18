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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # SPEC-6-02 [6.5.17]: a REST write of `stock` on an existing row
        # would be a ledger-free inventory mutation (audit cycle-1 BUG-1),
        # so updates expose the field read-only and every existing-row
        # change is forced through products.products.adjust_stock(), which
        # writes the StockMovement row. Creation keeps `stock` writable: it
        # establishes the opening balance, which is not an edit. The field
        # stays in the response payload either way.
        if self.instance is not None:
            self.fields["stock"].read_only = True

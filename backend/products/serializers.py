from rest_framework import serializers

from .models import products


class ProductSerializer(serializers.ModelSerializer):
    """Public product surface: an explicit whitelist instead of ``__all__``, so
    future model columns are never auto-exposed; the field set is pinned by a
    test.
    """

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
        # A REST write of `stock` on an existing row would bypass the ledger, so
        # updates expose it read-only; creation's opening balance stays writable.
        if self.instance is not None:
            self.fields["stock"].read_only = True

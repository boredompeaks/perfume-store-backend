"""The RBAC admin seam for shipping methods and rates (SPEC-1-B05 [R-1.07]).

`shipping.manage` follows the money: finance and admin, exactly as
`refunds.create` does (see common/roles.py for why the packing operator's
spec 1.1 line 110 "shipping" is `orders.fulfill` and NOT rate authority).
A rate is a price every future order will be charged, so managing one is
managing money, not working a shipment.

Every permission kind maps to the same capability: reading the rate table is
part of managing it (a staff member who can see a rate can work out what an
order will be charged), and nothing here is customer data.
"""

from django.contrib import admin

from common.admin import RoleAwareModelAdmin
from .models import ShippingMethod, ShippingRate

SHIPPING_MANAGE = "shipping.manage"

SHIPPING_CAPABILITY_MAP = {
    "view": SHIPPING_MANAGE,
    "add": SHIPPING_MANAGE,
    "change": SHIPPING_MANAGE,
    "delete": SHIPPING_MANAGE,
}


@admin.register(ShippingMethod)
class ShippingMethodAdmin(RoleAwareModelAdmin):
    capability_map = SHIPPING_CAPABILITY_MAP
    list_display = ("code", "name", "is_active", "rate_count", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    ordering = ("code",)

    @admin.display(description="Rates")
    def rate_count(self, obj):
        return obj.rates.count()


@admin.register(ShippingRate)
class ShippingRateAdmin(RoleAwareModelAdmin):
    capability_map = SHIPPING_CAPABILITY_MAP
    list_display = (
        "method",
        "amount",
        "region",
        "postal_code_prefix",
        "is_active",
    )
    list_filter = ("is_active", "method")
    # Autocomplete (not a raw id box) because the method is picked from a
    # table whose codes ARE the client-facing identifiers: a staff member
    # typing "express" into an id box would mint a method nobody can order.
    autocomplete_fields = ("method",)
    search_fields = ("region", "postal_code_prefix")
    ordering = ("method__code", "amount")

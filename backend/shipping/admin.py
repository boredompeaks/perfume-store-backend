"""RBAC admin seam for shipping methods/rates and for shipments.

Rate management follows the money (``shipping.manage``), while shipment data
needs ``shipments.read``/``shipments.write``; neither admin declares
``scoped_view_capability``, so the packing operator gets no second door.
"""

from django.contrib import admin, messages
from django.db import transaction

from common.admin import RoleAwareModelAdmin

from .lifecycle import sync_order_status
from .models import (
    Shipment,
    ShipmentEvent,
    ShippingMethod,
    ShippingProvider,
    ShippingRate,
)

SHIPPING_MANAGE = "shipping.manage"
SHIPMENT_READ = "shipments.read"
SHIPMENT_WRITE = "shipments.write"

SHIPPING_CAPABILITY_MAP = {
    "view": SHIPPING_MANAGE,
    "add": SHIPPING_MANAGE,
    "change": SHIPPING_MANAGE,
    "delete": SHIPPING_MANAGE,
}

SHIPMENT_CAPABILITY_MAP = {
    "view": SHIPMENT_READ,
    "add": SHIPMENT_WRITE,
    "change": SHIPMENT_WRITE,
    "delete": SHIPMENT_WRITE,
}

# Shown beside the order select on the add form; the change form is read-only.
SHIPMENT_ORDER_HELP_TEXT = (
    "A parcel belongs to the order it was created for. To serve another "
    "order, create a new parcel."
)


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
    # Autocomplete, not a raw id box: method codes are the client-facing
    # identifiers, so typing an id would mint a method nobody can order.
    autocomplete_fields = ("method",)
    search_fields = ("region", "postal_code_prefix")
    ordering = ("method__code", "amount")


@admin.register(Shipment)
class ShipmentAdmin(RoleAwareModelAdmin):
    """The ``/admin/shipments`` surface.

    ``order`` and ``provider`` are plain selects by design: the order admin's
    search lacks ``order_number``, and provider autocomplete would 403 the
    support role that holds ``shipments.write`` but not ``shipping.manage``.
    ``order`` is bound once (add-only) so a parcel's trail cannot be re-pointed.
    """

    capability_map = SHIPMENT_CAPABILITY_MAP
    list_display = (
        "order",
        "tracking_number",
        "carrier",
        "provider",
        "status",
        "estimated_delivery",
        "updated_at",
    )
    list_filter = ("status", "carrier", "provider")
    search_fields = (
        "tracking_number",
        "carrier",
        "provider__code",
        "provider__name",
        "order__order_number",
    )
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"

    def get_readonly_fields(self, request, obj=None):
        """Bind the parcel to its order after creation.

        Django's ``get_form`` drops read-only fields from the ModelForm, so a
        posted ``order`` on the change form cannot re-point an existing parcel.
        """
        readonly = super().get_readonly_fields(request, obj)
        if obj is not None:
            readonly += ("order",)
        return readonly

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "order":
            kwargs["help_text"] = SHIPMENT_ORDER_HELP_TEXT
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        """Append the tracking event for this save, in the same transaction.

        ``old`` is read under ``select_for_update()`` so concurrent operators
        cannot record the same ``from_status``; an unchanged status writes no
        event, and the linked order's move runs in the same transaction with a
        refusal surfaced as a warning rather than a failed save.
        """
        with transaction.atomic():
            old = None
            if change:
                old = Shipment.objects.select_for_update().get(pk=obj.pk).status
            super().save_model(request, obj, form, change)
            if old != obj.status:
                ShipmentEvent.objects.create(
                    shipment=obj,
                    from_status=old,
                    to_status=obj.status,
                    actor=request.user,
                )
            result = sync_order_status(obj, actor=request.user)
            if result.refusal:
                self.message_user(
                    request,
                    f"Order not moved: {result.refusal}",
                    level=messages.WARNING,
                )


@admin.register(ShipmentEvent)
class ShipmentEventAdmin(RoleAwareModelAdmin):
    """View-only surface for the append-only tracking trail.

    Staff with ``shipments.read`` can read it; add/change/delete are
    capability-less and refused even for superusers, with the model and
    queryset guards as the immutable layers behind it.
    """

    capability_map = {
        "view": SHIPMENT_READ,
        "add": None,
        "change": None,
        "delete": None,
    }
    list_display = ("shipment", "from_status", "to_status", "actor", "created_at")
    list_filter = ("to_status",)
    search_fields = (
        "shipment__tracking_number",
        "shipment__order__order_number",
        "actor__username",
    )
    readonly_fields = ("shipment", "from_status", "to_status", "actor", "created_at")
    # Newest first, overriding the model's oldest-first: operators read the
    # trail backwards, while the customer's tracking page reads it forward.
    ordering = ("-created_at", "-id")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ShippingProvider)
class ShippingProviderAdmin(RoleAwareModelAdmin):
    capability_map = SHIPPING_CAPABILITY_MAP
    list_display = ("code", "name", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    ordering = ("code",)

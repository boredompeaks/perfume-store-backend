"""The RBAC admin seam for shipping methods and rates (SPEC-1-B05 [R-1.07])
and for the shipments that leave on them (SPEC-1-B06 [R-1.08]).

`shipping.manage` follows the money: finance and admin, exactly as
`refunds.create` does (see common/roles.py for why the packing operator's
spec 1.1 line 110 "shipping" is `orders.fulfill` and NOT rate authority).
A rate is a price every future order will be charged, so managing one is
managing money, not working a shipment.

Every permission kind maps to the same capability: reading the rate table is
part of managing it (a staff member who can see a rate can work out what an
order will be charged), and nothing here is customer data.

`shipments.read` / `shipments.write` are the OPPOSITE answer for the shipment
surface, and the difference is the point. A rate holds no customer data; a
shipment reaches one through its order, so "which parcels are moving" is
order work rather than money work and follows support + admin (spec 6.9's
admin order actions, lines 1749/1757/1759). Neither ModelAdmin below declares
`scoped_view_capability`, so the packing operator's `orders.fulfill` opens no
second door onto shipments: it was never granted the capability, and
SPEC-1-B03's exact-set pin on that role stays true.
"""

from django.contrib import admin
from django.db import transaction

from common.admin import RoleAwareModelAdmin
from .models import (
    ShippingMethod,
    ShippingRate,
    Shipment,
    ShipmentEvent,
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


@admin.register(Shipment)
class ShipmentAdmin(RoleAwareModelAdmin):
    """[R-1.08] The `/admin/shipments` surface (spec 6.9 line 1991).

    Read is ``shipments.read`` and every write is ``shipments.write``, so a
    role can be given the trail without the pen. ``internal_note`` is the one
    field a customer never sees (spec 6.9 line 2027), which is why it is
    writable here and absent from the tracking serializer.

    ``order`` is a plain select rather than an autocomplete widget on purpose:
    autocomplete would resolve through ``OrderAdmin.search_fields``, and that
    search does not contain ``order_number``, so the operator would be
    picking an order by customer name instead of by the reference they are
    holding. Adding it would edit the SPEC-1-B03-audited order admin, which
    this task has no reason to touch.
    """

    capability_map = SHIPMENT_CAPABILITY_MAP
    list_display = (
        "order",
        "tracking_number",
        "carrier",
        "status",
        "estimated_delivery",
        "updated_at",
    )
    list_filter = ("status", "carrier")
    # Spec 6.9 line 2013: the tracking number is how staff and customers find
    # a parcel, so it is searchable alongside the order reference it belongs to.
    search_fields = ("tracking_number", "carrier", "order__order_number")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"

    def save_model(self, request, obj, form, change):
        """Append the tracking event for this save, in the same transaction.

        Spec 10.3 lists an "Audit record" as the last element of every
        transition's contract, and this form is the only writer a shipment has
        today, so the event is written HERE rather than left to a caller to
        remember - that is what keeps "a status change with a timestamp"
        (spec 6.9 line 2015) true of every path, including this one.

        ``old`` is read from the row under ``select_for_update()``, not from the
        posted form and not from a second unlocked read, so the event's source
        state is the status the database holds for as long as this save
        commits - two operators editing the same parcel cannot both record the
        same ``from_status``. (SQLite emits no FOR UPDATE, so the lock is inert
        there; the explicit ``transaction.atomic()`` around the whole pair is
        what holds on both engines, and it is the reason the block exists here
        rather than relying on the change form's own transaction the way the
        comment above does.)

        One event per ACTUAL move. ``old`` is None on the add form, so the
        creation this form performs records its opening step (a shipment
        arriving); an edit that leaves the status alone (``old == obj.status``)
        writes nothing, so the trail records transitions rather than clicks.
        A row minted straight through the ORM records no opening step, and
        that is deliberate: an event means a status CHANGED, and nothing
        changed at creation.
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


@admin.register(ShipmentEvent)
class ShipmentEventAdmin(RoleAwareModelAdmin):
    """[R-1.08] View-only surface for the append-only tracking trail.

    Mirrors ``OrderStatusEventAdmin``: staff holding ``shipments.read`` can
    read the trail, no staff role gets add/change/delete (all capability-less),
    and even the superuser bypass is refused at add and delete - audit history
    is never creatable or deletable through the admin. The change form renders
    every field read-only, so it is a view, not an editor, and the model's
    save guard is the second immutable layer behind this one.
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
    # Newest first, overriding the model's oldest-first ordering: an operator
    # reads the trail backwards, while the customer's tracking page reads it
    # forward.
    ordering = ("-created_at", "-id")

    def has_add_permission(self, request):
        return False  # append-only: nobody hand-writes audit rows

    def has_delete_permission(self, request, obj=None):
        return False  # audit history is never deletable

from django.contrib import admin

from common.admin import RoleAwareModelAdmin
from .models import Cart, CartItem


class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    can_delete = True
    fields = ("product", "quantity", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Cart)
class CartAdmin(RoleAwareModelAdmin):
    # Carts are customer-owned session state: staff may inspect them to help
    # customers (``customers.read``) but never edit them from the admin —
    # row edits would bypass the cart API's quantity/stock checks. The
    # mutation kinds therefore map to no capability (superuser bypass only).
    capability_map = {
        "view": "customers.read",
        "add": None,
        "change": None,
        "delete": None,
    }
    list_display = ("id", "session_short", "item_count", "created_at", "updated_at")
    search_fields = ("session_id", "id")
    ordering = ("-updated_at",)
    inlines = (CartItemInline,)

    @admin.display(description="Session")
    def session_short(self, obj):
        return f"{obj.session_id[:12]}…" if obj.session_id else "—"

    @admin.display(description="Items")
    def item_count(self, obj):
        return obj.items.count()

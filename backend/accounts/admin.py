from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User

from common.admin import RoleAwareModelAdmin
from orders.models import Order
# Replace auth's default User admin with the store-aware one below.
admin.site.unregister(User)


class OrderInline(admin.TabularInline):
    model = Order
    extra = 0
    can_delete = False
    fields = ("id", "status", "total_amount", "created_at")
    readonly_fields = ("id", "status", "total_amount", "created_at")
    verbose_name = "Order"
    verbose_name_plural = "Orders (newest first)"
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(User)
class StoreUserAdmin(RoleAwareModelAdmin, DjangoUserAdmin):
    """Customer view: who they are, what they've ordered.

    Role-aware least privilege (spec 6.12): anyone with ``customers.read``
    may inspect customer records, but the User row is also where
    ``is_staff`` and role groups live — viewing/creating/editing/deleting
    it is privilege management, so every mutation rides ``staff.manage``
    (admin only). This base class must precede ``DjangoUserAdmin`` so the
    capability-driven permission methods win the MRO.
    """

    capability_map = {
        "view": "customers.read",
        "add": "staff.manage",
        "change": "staff.manage",
        "delete": "staff.manage",
    }
    list_display = (
        "username",
        "email",
        "order_count",
        "spent_total",
        "is_active",
        "is_staff",
        "date_joined",
    )
    list_filter = ("is_active", "is_staff", "date_joined")
    search_fields = ("username", "email", "first_name", "last_name")
    inlines = (OrderInline,)

    @admin.display(description="Orders")
    def order_count(self, obj):
        return obj.orders.count()

    @admin.display(description="Spent")
    def spent_total(self, obj):
        from django.db.models import Sum

        total = obj.orders.filter(status__in=("confirmed", "shipped", "delivered")).aggregate(
            s=Sum("total_amount")
        )["s"]
        return f"₹{total}" if total is not None else "₹0"

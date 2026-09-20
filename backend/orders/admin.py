import csv

from django.contrib import admin, messages
from django.http import HttpResponse

from common.admin import RoleAwareModelAdmin
from .models import Coupon, Order, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    # [R-8.13] Explicit order: the frozen sku/variant_name snapshots render
    # beside the product name they were taken from. The permission overrides
    # below keep every snapshot column read-only -- admin edits would
    # falsify purchase history.
    fields = (
        "product",
        "product_name",
        "sku",
        "variant_name",
        "price",
        "quantity",
        "subtotal",
    )
    verbose_name = "Order item (snapshot)"
    verbose_name_plural = "Order items (snapshot)"

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# Legal status flow. Cancelling a *paid* order is deliberately impossible —
# there is no refund flow yet (V-03); reconciliation is manual by design.
ALLOWED_TRANSITIONS = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"shipped"},
    "shipped": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}


def transition_allowed(old_status: str, new_status: str) -> bool:
    return new_status == old_status or new_status in ALLOWED_TRANSITIONS.get(old_status, set())


@admin.register(Order)
class OrderAdmin(RoleAwareModelAdmin):
    # Role-aware least privilege (spec 6.12): support fulfils and cancels,
    # finance reads. Add/delete stay capability-less on purpose — orders
    # originate from checkout (manual rows would bypass payment), and hard
    # delete would bypass the legal status flow; the sanctioned paths are
    # the status actions and the cancel action below.
    capability_map = {
        "view": "orders.read",
        "add": None,
        "change": "orders.fulfill",
        "delete": None,
    }
    action_capabilities = {
        "mark_confirmed": "orders.fulfill",
        "mark_shipped": "orders.fulfill",
        "mark_delivered": "orders.fulfill",
        "cancel_pending": "orders.cancel",
        "export_csv": "orders.read",
    }
    # Cancelling orders is the one destructive bulk action here ([6.12.4]):
    # irreversible status change on a financial record, so it must be
    # explicitly confirmed before it executes.
    confirmation_required_actions = frozenset({"cancel_pending"})
    list_display = (
        "id",
        # [R-8.5] the customer-facing reference beside the internal pk
        "order_number",
        "user",
        "full_name",
        "status",
        "total_amount",
        "discount_amount",
        "coupon",
        "payment_ref",
        "created_at",
    )
    list_editable = ("status",)
    list_filter = ("status", "created_at")
    search_fields = ("id", "user__username", "user__email", "full_name", "phone")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    inlines = (OrderItemInline,)
    actions = ("mark_confirmed", "mark_shipped", "mark_delivered", "cancel_pending", "export_csv")
    readonly_fields = (
        "created_at",
        "updated_at",
        "total_amount",
        "discount_amount",
        "coupon",
        "razorpay_order_id",
        "razorpay_payment_id",
    )
    fieldsets = (
        ("Customer", {"fields": ("user", "full_name", "phone")}),
        ("Delivery address", {"fields": ("address", "city", "state", "pincode")}),
        (
            "Payment (server-computed — read only)",
            {
                "fields": (
                    "status",
                    "total_amount",
                    "discount_amount",
                    "coupon",
                    "razorpay_order_id",
                    "razorpay_payment_id",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Payment ref")
    def payment_ref(self, obj):
        return obj.razorpay_payment_id or "—"

    # ——— single-object guard (covers the inline status editor) ———

    def save_model(self, request, obj, form, change):
        if change:
            old = Order.objects.get(pk=obj.pk).status
            if not transition_allowed(old, obj.status):
                allowed = ", ".join(sorted(ALLOWED_TRANSITIONS[old])) or "nothing"
                self.message_user(
                    request,
                    f"Order #{obj.pk}: cannot move from '{old}' to '{obj.status}'. "
                    f"Allowed from '{old}': {allowed}. "
                    + (
                        "Cancelling a paid order needs a refund — reconcile manually."
                        if old in ("confirmed", "shipped", "delivered")
                        else ""
                    ),
                    messages.ERROR,
                )
                return  # abort the save; status unchanged
        super().save_model(request, obj, form, change)

    # ——— bulk actions (respect the same guards) ———

    def _bulk_set_status(self, request, queryset, new_status):
        allowed_from = [s for s, targets in ALLOWED_TRANSITIONS.items() if new_status in targets]
        matched_pks = list(
            queryset.filter(status__in=allowed_from).values_list("pk", flat=True)
        )
        count = queryset.filter(pk__in=matched_pks).update(status=new_status)
        skipped = queryset.count() - count
        if count:
            self.log_bulk_action(
                request,
                self.get_queryset(request).filter(pk__in=matched_pks),
                f"Bulk action: status changed to {new_status}.",
            )
            self.message_user(
                request, f"{count} order(s) marked {new_status}.", messages.SUCCESS
            )
        if skipped:
            self.message_user(
                request,
                f"{skipped} order(s) skipped — their current status does not allow "
                f"moving to '{new_status}'.",
                messages.WARNING,
            )

    @admin.action(description="Mark selected as confirmed")
    def mark_confirmed(self, request, queryset):
        self._bulk_set_status(request, queryset, "confirmed")

    @admin.action(description="Mark selected as shipped")
    def mark_shipped(self, request, queryset):
        self._bulk_set_status(request, queryset, "shipped")

    @admin.action(description="Mark selected as delivered")
    def mark_delivered(self, request, queryset):
        self._bulk_set_status(request, queryset, "delivered")

    @admin.action(description="Cancel selected (unpaid only)")
    def cancel_pending(self, request, queryset):
        unpaid_pks = list(
            queryset.filter(status="pending").values_list("pk", flat=True)
        )
        count = queryset.filter(pk__in=unpaid_pks).update(status="cancelled")
        skipped = queryset.count() - count
        if count:
            self.log_bulk_action(
                request,
                self.get_queryset(request).filter(pk__in=unpaid_pks),
                "Bulk action: order cancelled.",
            )
            self.message_user(request, f"{count} unpaid order(s) cancelled.", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f"{skipped} order(s) skipped — paid orders cannot be cancelled "
                f"(no refund flow; reconcile manually).",
                messages.WARNING,
            )

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="orders.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["id", "customer", "status", "total", "discount", "coupon", "created_at"]
        )
        for order in queryset:
            writer.writerow(
                [
                    order.id,
                    order.user.username,
                    order.status,
                    order.total_amount,
                    order.discount_amount,
                    order.coupon or "",
                    order.created_at,
                ]
            )
        return response


@admin.register(Coupon)
class CouponAdmin(RoleAwareModelAdmin):
    # Promotions are marketing's domain and the capability map has no
    # read-only split for them, so every model permission rides
    # ``discounts.write`` (marketing + admin) — least privilege by default:
    # support/finance/inventory get no coupon surface.
    capability_map = {
        "view": "discounts.write",
        "add": "discounts.write",
        "change": "discounts.write",
        "delete": "discounts.write",
    }
    list_display = (
        "code",
        "discount_type",
        "discount_value",
        "minimum_order_amount",
        "maximum_discount",
        "valid_from",
        "valid_until",
        "usage_display",
        "validity_state",
        "active",
    )
    list_editable = ("active",)
    list_filter = ("active", "discount_type")
    search_fields = ("code",)
    readonly_fields = ("used_count",)

    @admin.display(description="Usage")
    def usage_display(self, obj):
        if obj.usage_limit is None:
            return f"{obj.used_count} / ∞"
        return f"{obj.used_count} / {obj.usage_limit}"

    @admin.display(description="Validity", ordering="valid_until")
    def validity_state(self, obj):
        from django.utils import timezone

        now = timezone.now()
        if not obj.active:
            return "inactive"
        if now < obj.valid_from:
            return "scheduled"
        if now > obj.valid_until:
            return "expired"
        return "running"

import csv

from django.contrib import admin, messages
from django.http import HttpResponse
from django.utils import timezone

from common.admin import RoleAwareModelAdmin
# [R-10.1] The order machine lives in orders.state (single source); this
# module only consumes it.
from .models import Coupon, Order, OrderItem
from .state import ALLOWED_TRANSITIONS, transition_allowed


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


# The legal status flow (ALLOWED_TRANSITIONS) and its gate
# (transition_allowed) live in orders.state — [R-10.1] single source. The
# cancelling-a-paid-order rationale is documented beside the table there.


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
        # [R-8.11] the denomination beside the money columns it labels
        "currency",
        "coupon",
        "payment_ref",
        "created_at",
        # [R-8.16] the two live business-event stamps beside the row; the
        # still-unwritten events (fulfilled/shipped/delivered/refunded) stay
        # off the changelist until their writers land.
        "paid_at",
        "cancelled_at",
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
        # [R-8.16] the business-event timeline: admin edits would falsify
        # the lifecycle record, so every event stamp renders read-only.
        "paid_at",
        "fulfilled_at",
        "shipped_at",
        "delivered_at",
        "cancelled_at",
        "refunded_at",
        "total_amount",
        "discount_amount",
        "currency",
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
                    "currency",
                    "coupon",
                    "razorpay_order_id",
                    "razorpay_payment_id",
                )
            },
        ),
        (
            "Timestamps",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "paid_at",
                    "fulfilled_at",
                    "shipped_at",
                    "delivered_at",
                    "cancelled_at",
                    "refunded_at",
                )
            },
        ),
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
            # [R-8.16] The change form can legally move pending -> cancelled;
            # stamp the business-event timestamp beside the transition (the
            # bulk twin is cancel_pending below). The is-none guard keeps an
            # existing value: a set event time is never mutated.
            if obj.status == "cancelled" and obj.cancelled_at is None:
                obj.cancelled_at = timezone.now()
        super().save_model(request, obj, form, change)

    # ——— bulk actions (respect the same guards) ———

    def _bulk_set_status(self, request, queryset, new_status):
        allowed_from = [s for s, targets in ALLOWED_TRANSITIONS.items() if new_status in targets]
        matched_pks = list(
            queryset.filter(status__in=allowed_from).values_list("pk", flat=True)
        )
        # SPEC-6-04 audit advisory: the pk snapshot above and this UPDATE are
        # two statements — a row whose status changes in between (e.g. a
        # concurrent cancel of a pending order) would still match by pk and
        # be swept to the new status. Re-applying status__in in the UPDATE's
        # WHERE clause makes the transition predicate the final authority:
        # an out-of-set row can never be written, only counted as skipped.
        count = queryset.filter(
            pk__in=matched_pks, status__in=allowed_from
        ).update(status=new_status)
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
        # [R-8.16] The stamp rides the same update as the status transition.
        # Every row in unpaid_pks is still pending, so its cancelled_at is
        # necessarily NULL (only a cancel writes it) — the update can never
        # overwrite an existing stamp, and a re-run skips cancelled rows.
        count = queryset.filter(pk__in=unpaid_pks).update(
            status="cancelled", cancelled_at=timezone.now()
        )
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

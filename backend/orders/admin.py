import csv

from django.contrib import admin, messages
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone

from common.admin import RoleAwareModelAdmin
# [R-10.1] The order machine lives in orders.state (single source); this
# module only consumes it.
from .models import Coupon, Order, OrderItem, OrderStatusEvent
# [R-10.16] SPEC-10-05: the per-transition side-effect contract (one
# dispatch point, shared with the JSON seam).
from .events import notify_transition
# [R-12.8] SPEC-12-02: the admin cancel writers release the checkout's
# stock holds with the same vocabulary the API twin uses.
from products.models import StockReservation
# [R-10.1] SPEC-10-01b: the fulfilment-dimension mapping for the writers.
# [R-10.12] SPEC-10-02: the trigger vocabulary for the audit writers.
from .state import (
    ALLOWED_TRANSITIONS,
    TRIGGER_ADMIN_BULK_ACTION,
    TRIGGER_ADMIN_CHANGE_FORM,
    fulfilment_for_status,
    precondition_failures,
    transition_allowed,
)


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


def _append_status_event(order, *, from_status, to_status, actor, trigger):
    """[R-10.12]/[R-10.17] SPEC-10-02: one immutable audit row per
    transition. Callers MUST run this inside the transaction that persists
    the transition, so the two commit and roll back together ([R-10.18]) —
    the rollback pins in the test suite hold every writer to it."""
    return OrderStatusEvent.objects.create(
        order=order,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        trigger=trigger,
    )


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
        old = None
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
            # [R-10.19]/[R-10.14] SPEC-10-03: the machine gate above says
            # the edge exists; the preconditions say the row qualifies for
            # it (shipped: payment captured + items present). An unmet
            # precondition aborts the save exactly like an illegal edge —
            # message + return, status unchanged, no audit event.
            precondition_reasons = precondition_failures(obj, obj.status)
            if precondition_reasons:
                self.message_user(
                    request,
                    f"Order #{obj.pk}: cannot move to '{obj.status}' — "
                    + "; ".join(precondition_reasons)
                    + ".",
                    messages.ERROR,
                )
                return  # abort the save; status unchanged
            # [R-8.16] The change form can legally move pending -> cancelled;
            # stamp the business-event timestamp beside the transition (the
            # bulk twin is cancel_pending below). The is-none guard keeps an
            # existing value: a set event time is never mutated.
            if obj.status == "cancelled" and obj.cancelled_at is None:
                obj.cancelled_at = timezone.now()
        # [R-10.1] SPEC-10-01b: the fulfilment dimension rides every legal
        # status change through this form — the transition guard above is
        # the gate, fulfilment_for_status is the mapping. Admin never
        # touches payment_status: that dimension moves only with payment
        # events (verify_payment; SPEC-10-04 owns the rest).
        obj.fulfilment_status = fulfilment_for_status(obj.status)
        # [R-10.12]/[R-10.18] SPEC-10-02: the transition and its audit row
        # commit together. The admin changeform view already wraps this in
        # transaction.atomic; the explicit block keeps the rollback-together
        # guarantee local even if a future caller invokes save_model
        # outside it. Self-transitions (old == obj.status) are replays,
        # not transitions — no event.
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            if change and old != obj.status:
                _append_status_event(
                    obj,
                    from_status=old,
                    to_status=obj.status,
                    actor=request.user,
                    trigger=TRIGGER_ADMIN_CHANGE_FORM,
                )
                # [R-12.8] SPEC-12-02 §12.1 step 6: a cancelled checkout
                # releases its holds in this same transaction — cancelled
                # units return to available-to-sell immediately, not at
                # the TTL sweep. This is the admin twin of the release in
                # views.admin_order_cancel; the active-only filter is a
                # no-op on already-released holds.
                if obj.status == "cancelled":
                    obj.stock_reservations.filter(
                        status=StockReservation.Status.ACTIVE
                    ).update(status=StockReservation.Status.RELEASED)
                # [R-10.16] SPEC-10-05: the side-effect hook rides the
                # same atomic block, after the transition + its audit row.
                notify_transition(obj, old, obj.status)

    # ——— bulk actions (respect the same guards) ———

    def _bulk_set_status(self, request, queryset, new_status):
        allowed_from = [s for s, targets in ALLOWED_TRANSITIONS.items() if new_status in targets]
        matched_pks = list(
            queryset.filter(status__in=allowed_from).values_list("pk", flat=True)
        )
        count = 0
        # [R-10.19]/[R-10.14] SPEC-10-03: rows skipped for unmet
        # transition preconditions, kept separate from status skips so
        # each skip message reports its own true reason.
        precondition_skipped = []
        if matched_pks:
            with transaction.atomic():
                # SPEC-6-04 audit advisory, hardened in SPEC-9-07: the pk
                # snapshot above and the writes below are two steps — a row
                # whose status changes in between (e.g. a concurrent cancel
                # of a pending order) must never be swept to the new status.
                # The 9-07 fix made the transition predicate the final
                # authority via the UPDATE's WHERE clause; [R-10.12]
                # SPEC-10-02 reworks the same sweep into per-row saves (one
                # audit event per row), and each row's status is re-checked
                # here at write time under the row lock — the same
                # final-authority predicate, one row at a time. An
                # out-of-set row can never be written, only counted as
                # skipped. Per-row saves (unlike .update()) also fire
                # auto_now, fixing the stale updated_at the bulk path
                # shipped with.
                for order in (
                    queryset.filter(pk__in=matched_pks)
                    .select_for_update()
                    .order_by("pk")
                ):
                    if order.status not in allowed_from:
                        continue  # flipped between snapshot and save: skip
                    # [R-10.19]/[R-10.14] SPEC-10-03: the per-row
                    # precondition check under the row lock — the same
                    # final-authority shape as the status re-check above.
                    # A row that qualified at snapshot time but not at
                    # write time is skipped, never swept.
                    reasons = precondition_failures(order, new_status)
                    if reasons:
                        precondition_skipped.append(reasons)
                        continue
                    _append_status_event(
                        order,
                        from_status=order.status,
                        to_status=new_status,
                        actor=request.user,
                        trigger=TRIGGER_ADMIN_BULK_ACTION,
                    )
                    previous_status = order.status
                    order.status = new_status
                    order.fulfilment_status = fulfilment_for_status(new_status)
                    # updated_at rides update_fields explicitly: on this
                    # Django, save(update_fields=...) leaves auto_now
                    # columns out unless listed, and the whole point of the
                    # per-row rework is that the row's freshness moves with
                    # the transition (pinned in tests).
                    order.save(
                        update_fields=["status", "fulfilment_status", "updated_at"]
                    )
                    # [R-10.16] SPEC-10-05: the side-effect hook rides the
                    # same atomic block, after the transition + its audit
                    # row; skips never reach it.
                    notify_transition(order, previous_status, new_status)
                    count += 1
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
        if precondition_skipped:
            distinct_reasons = "; ".join(
                sorted({reason for row in precondition_skipped for reason in row})
            )
            self.message_user(
                request,
                f"{len(precondition_skipped)} order(s) skipped — '{new_status}' "
                f"preconditions unmet: {distinct_reasons}.",
                messages.WARNING,
            )
        if skipped - len(precondition_skipped):
            self.message_user(
                request,
                f"{skipped - len(precondition_skipped)} order(s) skipped — "
                f"their current status does not allow moving to '{new_status}'.",
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
        count = 0
        if unpaid_pks:
            with transaction.atomic():
                # [R-10.12] SPEC-10-02: per-row saves in one atomic block,
                # each row's status re-checked at write time under the row
                # lock (same final-authority pattern as _bulk_set_status) —
                # a row that flipped pending→confirmed between the snapshot
                # and this write is a PAID order now, and cancelling paid
                # orders is impossible by design, so it is skipped, never
                # swept. Per-row saves also fire auto_now (no stale
                # updated_at) and carry the audit event per row.
                for order in (
                    queryset.filter(pk__in=unpaid_pks)
                    .select_for_update()
                    .order_by("pk")
                ):
                    if order.status != "pending":
                        continue  # flipped between snapshot and save: skip
                    _append_status_event(
                        order,
                        from_status=order.status,
                        to_status="cancelled",
                        actor=request.user,
                        trigger=TRIGGER_ADMIN_BULK_ACTION,
                    )
                    previous_status = order.status
                    order.status = "cancelled"
                    # [R-8.16] The stamp rides the same save as the status
                    # transition. A pending order's cancelled_at is
                    # necessarily NULL (only a cancel writes it), so the
                    # is-none guard never overwrites an existing stamp, and
                    # a re-run skips already-cancelled rows.
                    order.cancelled_at = order.cancelled_at or timezone.now()
                    # [R-10.1] SPEC-10-01b: the fulfilment dimension rides
                    # the same save (a cancelled order was never fulfilled).
                    order.fulfilment_status = fulfilment_for_status("cancelled")
                    # updated_at rides update_fields explicitly (see
                    # _bulk_set_status): the freshness must move with the
                    # transition.
                    order.save(
                        update_fields=[
                            "status",
                            "cancelled_at",
                            "fulfilment_status",
                            "updated_at",
                        ]
                    )
                    # [R-12.8] SPEC-12-02 §12.1 step 6: a cancelled checkout
                    # releases its holds in this same per-row transaction —
                    # the admin surface must behave exactly like the API
                    # twin (views.admin_order_cancel); the active-only
                    # filter is a no-op on already-released holds.
                    order.stock_reservations.filter(
                        status=StockReservation.Status.ACTIVE
                    ).update(status=StockReservation.Status.RELEASED)
                    # [R-10.16] SPEC-10-05: the side-effect hook rides the
                    # same atomic block, after the transition + its audit
                    # row; skipped rows never reach it.
                    notify_transition(order, previous_status, "cancelled")
                    count += 1
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


@admin.register(OrderStatusEvent)
class OrderStatusEventAdmin(RoleAwareModelAdmin):
    """[R-10.18] SPEC-10-02: view-only surface for the append-only trail.

    Staff holding ``orders.read`` (support/finance/admin roles) can read
    the trail; no staff role gets add/change/delete (all capability-less),
    and even the superuser bypass is refused at add/delete — audit history
    is never creatable or deletable through the admin. The change form
    renders every field read-only, so it is a view, not an editor; the
    model save guard is the second immutable layer behind this one.
    """

    capability_map = {
        "view": "orders.read",
        "add": None,
        "change": None,
        "delete": None,
    }
    list_display = (
        "order",
        "from_status",
        "to_status",
        "actor",
        "trigger",
        "created_at",
    )
    list_filter = ("trigger", "to_status")
    search_fields = ("order__order_number", "order__id", "actor__username")
    readonly_fields = (
        "order",
        "from_status",
        "to_status",
        "actor",
        "trigger",
        "created_at",
    )

    def has_add_permission(self, request):
        return False  # append-only: nobody hand-writes audit rows

    def has_delete_permission(self, request, obj=None):
        return False  # audit history is never deletable


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

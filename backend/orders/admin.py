import csv
from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from common.admin import (
    CONFIRMATION_NOTE_FIELD,
    ConfirmationNoteForm,
    RoleAwareModelAdmin,
)
from common.audit import log_mutation, model_field_changes
from common.models import AuditEvent
from common.saved_filters import SavedFilterMixin

# The admin cancel writers release the checkout's stock holds with the same
# vocabulary the API twin uses.
from products.models import StockReservation

# The ONE issuance core, bound as a module because ``issue_refund`` is also the
# action's name on OrderAdmin; the API seam and this admin action call the same
# locks, balance gate and gateway call.
from . import refunds as refunds_core

# The per-transition side-effect contract, shared with the JSON seam.
from .events import notify_transition

# The order machine lives in orders.state; the return row and its own machine
# stay here, out of orders.state on purpose (that module is audited and holds
# the order dimensions only).
from .models import (
    Coupon,
    Order,
    OrderItem,
    OrderStatusEvent,
    Refund,
    RefundRequest,
    ReturnRequest,
    _allowed_from,
    return_transition_allowed,
)

# The fulfilment-dimension mapping and the trigger vocabulary for the writers.
from .state import (
    ALLOWED_TRANSITIONS,
    FULFILMENT_QUEUE_STATUSES,
    TRIGGER_ADMIN_BULK_ACTION,
    TRIGGER_ADMIN_CHANGE_FORM,
    fulfilment_for_status,
    precondition_failures,
    transition_allowed,
)

# The amount a request asks for is derived beside the customer seam that writes
# ``items``, so the staff decision cannot read the list by a drifting second
# rule.
from .views.refund import refund_request_amount

# The grid shows enough of the destination to identify a parcel; named once so
# the display and its test cannot drift to different widths.
SHORT_ADDRESS_MAX_LENGTH = 40


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    # The frozen sku/variant_name snapshots render beside the product name they
    # were taken from; the overrides below keep every column read-only, since an
    # admin edit would falsify purchase history.
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


class OrderItemPackingInline(OrderItemInline):
    """The item lines as the PACKING operator needs them.

    Packing does not hold ``orders.read``, so the per-line money is withheld.
    ``has_view_permission`` is the one thing it adds: Django skips an inline
    whose permissions are all false, and "what is in this parcel" is the point
    of the surface - it stays a read.
    """

    fields = ("product_name", "sku", "variant_name", "quantity")
    verbose_name = "Item to pack"
    verbose_name_plural = "Items to pack"

    def has_view_permission(self, request, obj=None):
        return True


def _append_status_event(order, *, from_status, to_status, actor, trigger):
    """One immutable audit row per transition.

    Callers MUST run this inside the transaction that persists the transition,
    so the two commit and roll back together."""
    return OrderStatusEvent.objects.create(
        order=order,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        trigger=trigger,
    )


class IssueRefundForm(forms.Form):
    """The operator's input on the refund interstitial.

    Neither field carries a business rule: blank ``amount`` means the remaining
    balance, and ``reason`` is passed through so the shared core is the ONE
    authority on whether a refund may be issued and what a refusal says.
    """

    amount = forms.DecimalField(
        required=False,
        label="Amount",
        help_text="Leave blank to refund the order's remaining balance.",
    )
    reason = forms.CharField(
        required=False,
        label="Reason",
        help_text="Required. Recorded verbatim on the refund row.",
        widget=forms.Textarea(attrs={"rows": 2}),
    )


@admin.register(Order)
class OrderAdmin(SavedFilterMixin, RoleAwareModelAdmin):
    # The saved-view bar rides first in the bases so it wraps whichever
    # changelist_view branch runs.
    # Least privilege: support fulfils and cancels, finance reads. Add/delete
    # stay capability-less because orders originate from checkout and a hard
    # delete would bypass the legal status flow.
    capability_map = {
        "view": "orders.read",
        "add": None,
        "change": "orders.fulfill",
        "delete": None,
    }
    # Packing holds ``orders.fulfill`` and deliberately not ``orders.read``;
    # without this second door the operator's grid answers 200 on a URL it can
    # only guess. The scoped viewer is admitted to the FULFILMENT queue below -
    # a narrowed listing, not a wider capability, so ``export_csv`` stays
    # refused.
    scoped_view_capability = "orders.fulfill"
    # The statuses the fulfilment walk can still advance, read from orders.state
    # so the listing cannot drift from the transitions it offers.
    scoped_queryset = FULFILMENT_QUEUE_STATUSES
    # Only what packing needs: no total, discount, currency, coupon or payment
    # reference.
    scoped_list_display = (
        "id",
        "order_number",
        # Destination only: enough to sort a courier run, never a lookup key
        # (see scoped_search_fields below).
        "short_address",
        "city",
        "status",
        "created_at",
    )
    # Reference-only search: rendering the address as a destination column is
    # not authorization to search it.
    scoped_search_fields = ("id", "order_number")
    # The change form, narrowed the same way: the user, phone, money and gateway
    # columns stay out.
    scoped_fieldsets = (
        (
            "Shipping",
            {"fields": ("order_number", "address", "city", "state", "pincode")},
        ),
        ("Fulfilment", {"fields": ("status",)}),
        ("Timestamps", {"fields": ("created_at", "shipped_at", "delivered_at")}),
    )
    # What this door may WRITE: declared, not derived, and deny-by-default in
    # the base, so a scoped fieldset can never hand out a write by growing a
    # field. Fulfilment state is the one editable field; everything else is
    # read-only, so a packer cannot redirect a parcel.
    scoped_writable_fields = frozenset({"status"})
    # Cancelling is destructive authority on a financial record, gated on
    # ``orders.cancel`` which this role does not hold; named with the same
    # capability the bulk ``cancel_pending`` action answers on, so the change
    # form, the list-edit cell and the bulk action cannot drift.
    scoped_value_capabilities = {"status": {"cancelled": "orders.cancel"}}
    action_capabilities = {
        "mark_confirmed": "orders.fulfill",
        "mark_shipped": "orders.fulfill",
        "mark_delivered": "orders.fulfill",
        "cancel_pending": "orders.cancel",
        "export_csv": "orders.read",
        # Money movement is finance's call: absent from support's dropdown and a
        # directly POSTed name is refused.
        "issue_refund": "refunds.create",
    }
    # Irreversible status change on a financial record, so it must be explicitly
    # confirmed.
    confirmation_required_actions = frozenset({"cancel_pending"})
    # The only confirmed action that asks why, mirroring AdjustStockForm's
    # reason capture so a cancellation is audited with the operator's words.
    confirmation_reason_actions = frozenset({"cancel_pending"})
    list_display = (
        "id",
        "order_number",
        "user",
        "full_name",
        # The destination at a glance; the change form keeps the full value.
        "short_address",
        "status",
        "total_amount",
        "discount_amount",
        "currency",
        "coupon",
        "payment_ref",
        "created_at",
        # The two live business-event stamps; the still-unwritten events stay
        # off the changelist until their writers land.
        "paid_at",
        "cancelled_at",
    )
    list_editable = ("status",)
    list_filter = ("status", "created_at")
    # guest_email and address join the customer search terms, but deliberately
    # stay out of scoped_search_fields: the packing queue may read the
    # destination, never use it as a customer probe.
    search_fields = (
        "id",
        "user__username",
        "user__email",
        "guest_email",
        "full_name",
        "phone",
        "address",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    inlines = (OrderItemInline,)
    actions = (
        "mark_confirmed",
        "mark_shipped",
        "mark_delivered",
        "cancel_pending",
        "export_csv",
        "issue_refund",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        # Admin edits would falsify the lifecycle record, so every event stamp
        # renders read-only.
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
        (
            "Customer",
            # guest_email rides the Customer group so staff read the same field
            # that identifies a guest order, next to the account it replaces.
            {"fields": ("user", "guest_email", "full_name", "phone")},
        ),
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

    @admin.display(description="Address")
    def short_address(self, obj):
        """A one-line preview of the delivery address for the grid.

        Newlines and whitespace runs are collapsed for the single table cell,
        and the length is bounded so one long address cannot crowd out the
        money columns beside it.
        """
        address = " ".join((obj.address or "").split())
        if len(address) <= SHORT_ADDRESS_MAX_LENGTH:
            return address
        return f"{address[: SHORT_ADDRESS_MAX_LENGTH - 1].rstrip()}…"

    @admin.display(description="Payment ref")
    def payment_ref(self, obj):
        return obj.razorpay_payment_id or "—"

    # Each hook narrows ONE thing for a scoped viewer and defers to Django
    # otherwise, so every other role's behaviour is byte-identical. What the
    # door may WRITE is declared above and enforced by the base.

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not self.is_scoped_viewer(request):
            return queryset
        return queryset.filter(status__in=self.scoped_queryset)

    def get_list_display(self, request):
        if not self.is_scoped_viewer(request):
            return super().get_list_display(request)
        return self.scoped_list_display

    def get_search_fields(self, request):
        if not self.is_scoped_viewer(request):
            return super().get_search_fields(request)
        return self.scoped_search_fields

    def get_fieldsets(self, request, obj=None):
        if not self.is_scoped_viewer(request):
            return super().get_fieldsets(request, obj)
        return self.scoped_fieldsets

    def get_inlines(self, request, obj=None):
        if self.is_scoped_viewer(request):
            # The money half of the snapshot is withheld too; get_inlines
            # yields CLASSES, so the swap is a class substitution.
            return [OrderItemPackingInline]
        return super().get_inlines(request, obj)

    def get_urls(self):
        """The refund action's own input page.

        The action opens this page instead of executing because a refund needs
        amount and reason - the page IS the confirmation step; the capability is
        re-checked in the view because "is_staff" is not the authority on money
        movement.
        """
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/issue-refund/",
                self.admin_site.admin_view(self.issue_refund_view),
                name="orders_order_issue_refund",
            )
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        """The grid a scoped viewer lands on IS the fulfilment queue.

        ``get_queryset`` already restricts the rows, and every surface reading
        through this admin inherits that narrowing. This override only skips
        ``SavedFilterMixin``, whose write endpoints gate on a view permission
        this role deliberately lacks.
        """
        if not self.is_scoped_viewer(request):
            return super().changelist_view(request, extra_context)
        return RoleAwareModelAdmin.changelist_view(self, request, extra_context)

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
                        "Cancelling a paid order needs a refund — issue "
                        "one via POST /api/admin/orders/<id>/refund/."
                        if old in ("confirmed", "shipped", "delivered")
                        else ""
                    ),
                    messages.ERROR,
                )
                return  # abort the save; status unchanged
            # The machine gate says the edge exists; the preconditions say the
            # row qualifies. An unmet precondition aborts the save like an
            # illegal edge.
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
            # The is-none guard keeps an existing value: a set event time is
            # never mutated.
            if obj.status == "cancelled" and obj.cancelled_at is None:
                obj.cancelled_at = timezone.now()
        # The fulfilment dimension rides every legal status change; admin never
        # touches payment_status, which moves only with payment events.
        obj.fulfilment_status = fulfilment_for_status(obj.status)
        # The transition and its audit row commit together; the explicit block
        # keeps rollback-together local even if a future caller invokes
        # save_model outside the admin's own atomic. Self-transitions are
        # replays, not transitions - no event.
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
                # A cancelled checkout releases its holds in this same
                # transaction, so units return to available-to-sell immediately
                # rather than at the TTL sweep; the active-only filter is a
                # no-op on already-released holds.
                if obj.status == "cancelled":
                    obj.stock_reservations.filter(
                        status=StockReservation.Status.ACTIVE
                    ).update(status=StockReservation.Status.RELEASED)
                # The side-effect hook rides the same atomic block, after the
                # transition and its audit row.
                notify_transition(obj, old, obj.status)

    @staticmethod
    def _locked_orders(queryset, pks):
        """The bulk writers' locked read of the snapshot pks.

        The joins are stripped with ``select_related(None)`` because the
        changelist's select_related adds a join for every FK in
        ``list_display``, and ``user``/``coupon`` are nullable, so those are
        LEFT OUTER JOINs: PostgreSQL rejects FOR UPDATE over the nullable side
        of an outer join and SQLite silently drops it. Stripping costs no query
        (the loop reads Order columns, ``order.items`` and scalar preconditions
        only).
        """
        return (
            queryset.filter(pk__in=pks)
            .select_related(None)
            .select_for_update()
            .order_by("pk")
        )

    def _bulk_set_status(self, request, queryset, new_status):
        allowed_from = [
            s for s, targets in ALLOWED_TRANSITIONS.items() if new_status in targets
        ]
        matched_pks = list(
            queryset.filter(status__in=allowed_from).values_list("pk", flat=True)
        )
        count = 0
        # Rows skipped for unmet preconditions, kept separate from status skips
        # so each message reports its own true reason.
        precondition_skipped = []
        if matched_pks:
            with transaction.atomic():
                # The pk snapshot and the writes are two steps, so each row's
                # status is re-checked at write time under the row lock: an
                # out-of-set row can never be written, only counted as skipped.
                # Per-row saves also append one audit event each and fire
                # auto_now.
                for order in self._locked_orders(queryset, matched_pks):
                    if order.status not in allowed_from:
                        continue
                    # The precondition check under the row lock: a row that
                    # qualified at snapshot time but not now is skipped.
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
                    # updated_at rides update_fields explicitly: this Django
                    # leaves auto_now columns out unless listed.
                    order.save(
                        update_fields=["status", "fulfilment_status", "updated_at"]
                    )
                    # The side-effect hook rides the same atomic block; skips
                    # never reach it.
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

    def confirmation_details(self, request, action_name, objects):
        """What cancelling this selection actually costs, before it happens.

        The amount and currency, the lines affected and the state each row ends
        in; rows the action will NOT touch are spelled out too, because "3
        orders, only 1 cancelled" is the surprise this payload prevents.
        """
        if action_name != "cancel_pending":
            return {}
        orders = list(objects)
        cancelable = [order for order in orders if order.status == "pending"]
        # Money stays Decimal end to end and is quantized before rendering;
        # currencies are listed rather than assumed (the denomination is a
        # setting, not a constant).
        total = sum(
            (order.total_amount for order in cancelable), Decimal("0")
        ).quantize(Decimal("0.01"))
        currencies = sorted({order.currency for order in orders})
        return {
            "summary": (
                f"{len(cancelable)} of {len(orders)} selected order(s) will be "
                f"cancelled, totalling {total} {'/'.join(currencies)}"
            ),
            "rows": [self._cancel_confirmation_row(order) for order in orders],
        }

    def _cancel_confirmation_row(self, order):
        pending = order.status == "pending"
        # A row is skipped because it is NOT pending, and only one of those
        # reasons is "it is paid": an already-cancelled order must not be told
        # it was paid.
        unchanged_reason = (
            "(already cancelled)"
            if order.status == "cancelled"
            else "(paid, never cancelled here)"
        )
        return {
            "label": str(order),
            "fields": [
                ("Amount", order.total_amount),
                ("Currency", order.currency),
                (
                    "Resulting state",
                    f"{order.status} → cancelled"
                    if pending
                    else f"{order.status} → unchanged {unchanged_reason}",
                ),
            ],
            "items": [str(item) for item in order.items.all()],
        }

    def _cancel_change_message(self, request):
        """The audit change message for a cancelled selection.

        The operator's optional reason from the interstitial is merged in so the
        trail carries WHY; with no reason typed the message is the original one.
        """
        message = "Bulk action: order cancelled."
        note = self.confirmation_note(request, "cancel_pending")
        if note:
            message = f"{message} Reason: {note}"
        return message

    @admin.action(description="Cancel selected (unpaid only)")
    def cancel_pending(self, request, queryset):
        unpaid_pks = list(
            queryset.filter(status="pending").values_list("pk", flat=True)
        )
        count = 0
        if unpaid_pks:
            with transaction.atomic():
                # Per-row saves in one atomic block, each status re-checked
                # under the row lock: a row that flipped pending->confirmed is a
                # PAID order now, and cancelling paid orders is impossible by
                # design, so it is skipped, never swept.
                for order in self._locked_orders(queryset, unpaid_pks):
                    if order.status != "pending":
                        continue
                    _append_status_event(
                        order,
                        from_status=order.status,
                        to_status="cancelled",
                        actor=request.user,
                        trigger=TRIGGER_ADMIN_BULK_ACTION,
                    )
                    previous_status = order.status
                    order.status = "cancelled"
                    # The stamp rides the same save; a pending order's
                    # cancelled_at is NULL, so the is-none guard never
                    # overwrites one and a re-run skips already-cancelled rows.
                    order.cancelled_at = order.cancelled_at or timezone.now()
                    # A cancelled order was never fulfilled.
                    order.fulfilment_status = fulfilment_for_status("cancelled")
                    # updated_at rides update_fields explicitly (see
                    # _bulk_set_status).
                    order.save(
                        update_fields=[
                            "status",
                            "cancelled_at",
                            "fulfilment_status",
                            "updated_at",
                        ]
                    )
                    # A cancelled checkout releases its holds in this same
                    # transaction, exactly like the API twin; the active-only
                    # filter is a no-op on already-released holds.
                    order.stock_reservations.filter(
                        status=StockReservation.Status.ACTIVE
                    ).update(status=StockReservation.Status.RELEASED)
                    # The side-effect hook rides the same atomic block; skipped
                    # rows never reach it.
                    notify_transition(order, previous_status, "cancelled")
                    count += 1
        skipped = queryset.count() - count
        if count:
            self.log_bulk_action(
                request,
                self.get_queryset(request).filter(pk__in=unpaid_pks),
                self._cancel_change_message(request),
            )
            self.message_user(
                request, f"{count} unpaid order(s) cancelled.", messages.SUCCESS
            )
        if skipped:
            self.message_user(
                request,
                f"{skipped} order(s) skipped — paid orders cannot be "
                f"cancelled (refund them via "
                f"POST /api/admin/orders/<id>/refund/).",
                messages.WARNING,
            )

    @admin.action(description="Issue a refund for the selected order")
    def issue_refund(self, request, queryset):
        """Route the selection to the amount/reason page.

        One order at a time on purpose: a bulk entry point would invite a slip
        of the selection to move money on an order nobody looked at.
        """
        pks = list(queryset.values_list("pk", flat=True))
        if len(pks) != 1:
            self.message_user(
                request,
                "Select exactly one order to refund.",
                messages.ERROR,
            )
            return None
        return HttpResponseRedirect(
            reverse(
                f"{self.admin_site.name}:orders_order_issue_refund",
                args=[pks[0]],
            )
        )

    def issue_refund_view(self, request, object_id):
        """The intermediate page: collect the amount and reason, then issue.

        ``refunds.create`` is the authority here, not the model's change
        capability; issuance is the shared core the API seam calls, so a
        refusal leaves the order exactly as it was.
        """
        if not self._holds_capability(request, "refunds.create"):
            raise PermissionDenied
        order = self.get_object(request, object_id)
        if order is None:
            raise Http404("Order not found")

        if request.method == "POST":
            form = IssueRefundForm(request.POST)
            if form.is_valid():
                try:
                    receipt = refunds_core.issue_refund(
                        request,
                        order_id=order.pk,
                        amount=form.cleaned_data["amount"],
                        reason=form.cleaned_data["reason"],
                        source="admin",
                    )
                except refunds_core.RefundRefused as exc:
                    self.message_user(
                        request,
                        f"Order #{order.pk}: {exc.message}.",
                        messages.ERROR,
                    )
                    return HttpResponseRedirect(request.path)
                except refunds_core.RefundGatewayError:
                    self.message_user(
                        request,
                        "The payment gateway could not complete this refund",
                        messages.ERROR,
                    )
                    return HttpResponseRedirect(request.path)
                self.message_user(
                    request,
                    f"Refund #{receipt.refund.id} of {receipt.refund.amount} "
                    f"{receipt.order.currency} issued for order #{order.pk}.",
                    messages.SUCCESS,
                )
                return HttpResponseRedirect(
                    reverse(
                        f"{self.admin_site.name}:orders_order_change",
                        args=[order.pk],
                    )
                )
        else:
            form = IssueRefundForm()

        return TemplateResponse(
            request,
            "admin/orders/order/issue_refund.html",
            {
                **self.admin_site.each_context(request),
                "title": f"Issue refund for order #{order.pk}",
                "order": order,
                "form": form,
                "opts": self.model._meta,
                "cancel_url": reverse(
                    f"{self.admin_site.name}:orders_order_change",
                    args=[order.pk],
                ),
            },
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
                    # `customer_name` resolves the guest's email, so exporting a
                    # mixed queue cannot raise on guest orders.
                    order.customer_name,
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
    """View-only surface for the append-only trail.

    Staff holding ``orders.read`` can read it; no role gets add/change/delete
    and even the superuser bypass is refused there. Every field renders
    read-only; the model save guard is the second immutable layer.
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
    # Promotions are marketing's domain and the map has no read-only split, so
    # every permission rides ``discounts.write``; least privilege means no other
    # role gets a coupon surface.
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

    def save_model(self, request, obj, form, change):
        """Record the real before -> after value of a promotion edit.

        Django's own changelist-edit LogEntry names the column but not what it
        changed from or to. The snapshot is read from the DATABASE, not off the
        instance, because ``save_form`` has already applied the submitted values
        by the time ``save_model`` runs.
        """
        if not change:
            return
        stored = (
            type(obj).objects.filter(pk=obj.pk).values_list(*self.list_editable).first()
        )
        before = dict(zip(self.list_editable, stored or ()))
        super().save_model(request, obj, form, change)
        changes = model_field_changes(obj, self.list_editable, before=before)
        if changes:
            log_mutation(
                request,
                obj,
                AuditEvent.EventType.CATALOGUE_UPDATED,
                "coupon_updated",
                AuditEvent.Source.ADMIN,
                changes=changes,
            )

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


@admin.register(Refund)
class RefundAdmin(RoleAwareModelAdmin):
    """A read-only window onto issued refunds.

    ``refunds.create`` gates it, so finance reconciles the money and support
    gets no page; no role gets add/change/delete (the superuser bypass
    included), because a refund is issued only through the shared writer that
    calls the gateway. Every field renders read-only.
    """

    capability_map = {
        "view": "refunds.create",
        "add": None,
        "change": None,
        "delete": None,
    }
    list_display = (
        "id",
        "order",
        "amount",
        "kind",
        "status",
        "gateway_refund_id",
        "actor",
        "created_at",
    )
    list_filter = ("status", "kind", "created_at")
    search_fields = (
        "id",
        "order__id",
        "order__order_number",
        "gateway_refund_id",
        "actor__username",
    )
    readonly_fields = (
        "order",
        "amount",
        "reason",
        "kind",
        "status",
        "gateway_refund_id",
        "actor",
        "idempotency_key",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False  # issued by the refund seam, never hand-written

    def has_delete_permission(self, request, obj=None):
        return False  # money that moved is never deleted


class RefundRequestDecisionNoteForm(ConfirmationNoteForm):
    """The decision note is REQUIRED on the refund-request surface.

    The shared interstitial treats a note as optional, but a refund decision is
    a financial act that has to explain itself later, so this surface tightens
    only its own field; the base stays optional for every other action.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields[CONFIRMATION_NOTE_FIELD]
        field.required = True
        field.help_text = "Required. Recorded verbatim in the admin audit trail."


@admin.register(RefundRequest)
class RefundRequestAdmin(RoleAwareModelAdmin):
    """The staff decision over a customer's refund request.

    The ask is never money: approval turns it into money only by calling the
    shared refund writer, and it locks the order and the request, derives the
    amount, issues the refund and only then marks the request approved - so a
    refusal or gateway failure leaves it pending with no partial state.
    ``refunds.create`` gates view and change; no role gets add or delete, every
    field is read-only, and the only writes are the confirmation-gated actions.
    """

    capability_map = {
        "view": "refunds.create",
        "add": None,
        "change": "refunds.create",
        "delete": None,
    }
    actions = ("approve_requests", "reject_requests")
    action_capabilities = {
        "approve_requests": "refunds.create",
        "reject_requests": "refunds.create",
    }
    confirmation_required_actions = frozenset(actions)
    confirmation_reason_actions = frozenset(actions)
    list_display = (
        "id",
        "order",
        "requester",
        "status",
        "item_count",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = (
        "id",
        "order__id",
        "order__order_number",
        "requester__username",
    )
    readonly_fields = (
        "order",
        "requester",
        "reason",
        "items",
        "status",
        "decided_by",
        "decided_at",
        "created_at",
    )
    date_hierarchy = "created_at"

    @admin.display(description="Items")
    def item_count(self, refund_request):
        # An empty list is the whole order, so "0" would misread the ask.
        return len(refund_request.items) if refund_request.items else "All"

    def has_add_permission(self, request):
        return False  # asked for by the customer, never hand-written

    def has_delete_permission(self, request, obj=None):
        return False  # the decision trail is never erased

    def confirmation_reason_form(self, action_name, data=None):
        if action_name not in self.confirmation_reason_actions:
            return None
        return RefundRequestDecisionNoteForm(data)

    @admin.action(description="Approve selected refund requests")
    def approve_requests(self, request, queryset):
        """Turn pending requests into refunds through the ONE money writer.

        One note covers the selection - it is recorded as the reason on every
        refund issued. A request no longer pending is skipped, and the
        idempotency key makes a second approval collapse onto the refund it
        already produced.
        """
        note = self.confirmation_note(request, "approve_requests")
        if not note:
            self.message_user(
                request,
                "A decision reason/note is required to decide a refund request.",
                messages.ERROR,
            )
            return

        for refund_request in queryset:
            try:
                self._approve_refund_request(request, refund_request, note)
            except refunds_core.RefundRefused as exc:
                self.message_user(
                    request,
                    f"Refund request #{refund_request.pk}: {exc.message}.",
                    messages.ERROR,
                )
            except refunds_core.RefundGatewayError:
                self.message_user(
                    request,
                    f"Refund request #{refund_request.pk}: the payment "
                    "gateway could not complete this refund.",
                    messages.ERROR,
                )

    @admin.action(description="Reject selected refund requests")
    def reject_requests(self, request, queryset):
        """Decline pending requests without touching money or the order."""
        note = self.confirmation_note(request, "reject_requests")
        if not note:
            self.message_user(
                request,
                "A decision reason/note is required to decide a refund request.",
                messages.ERROR,
            )
            return

        for refund_request in queryset:
            with transaction.atomic():
                locked = (
                    RefundRequest.objects.select_for_update()
                    .filter(
                        pk=refund_request.pk,
                        status=RefundRequest.Status.PENDING,
                    )
                    .first()
                )
                if locked is None:
                    self._already_decided(request, refund_request.pk)
                    continue
                locked.status = RefundRequest.Status.REJECTED
                locked.decided_by = request.user
                locked.decided_at = timezone.now()
                locked.save(update_fields=["status", "decided_by", "decided_at"])
                self.log_change(
                    request,
                    locked,
                    f"Rejected refund request #{locked.pk}. Note: {note}",
                )

    def _approve_refund_request(self, request, refund_request, note):
        """One approval, atomic from the locks to the audit record.

        The lock order is order-then-request, the same order the customer
        filing seam takes, so a decision and a concurrent filing serialize
        instead of deadlocking. A gateway failure escapes this block too, so
        the request status change below is never reached.
        """
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=refund_request.order_id)
            locked = (
                RefundRequest.objects.select_for_update()
                .filter(
                    pk=refund_request.pk,
                    status=RefundRequest.Status.PENDING,
                )
                .first()
            )
            if locked is None:
                self._already_decided(request, refund_request.pk)
                return

            amount = refund_request_amount(order, locked.items)
            receipt = refunds_core.issue_refund(
                request,
                order_id=order.pk,
                amount=amount,
                reason=note,
                # Deterministic per request, so a retry - or a request an
                # operator re-opened - collapses onto the refund it already
                # produced instead of paying twice.
                idempotency_key=f"refund-request-{locked.pk}",
                source="admin",
            )

            locked.status = RefundRequest.Status.APPROVED
            locked.decided_by = request.user
            locked.decided_at = timezone.now()
            locked.save(update_fields=["status", "decided_by", "decided_at"])

            # The request's own half of the privileged-action trail;
            # issue_refund records the money's half against the Refund row.
            self.log_change(
                request,
                locked,
                f"Approved refund request #{locked.pk}: refund "
                f"#{receipt.refund.id} of {receipt.refund.amount} "
                f"{receipt.order.currency} issued for order #{order.pk}. "
                f"Note: {note}",
            )

    def _already_decided(self, request, request_pk):
        self.message_user(
            request,
            f"Refund request #{request_pk} was already decided; nothing to do.",
            messages.WARNING,
        )


# The return-request surface: read is ``returns.read`` and every write is
# ``returns.write``, so a role can be given the queue without the decision.
RETURNS_READ = "returns.read"
RETURNS_WRITE = "returns.write"

RETURN_CAPABILITY_MAP = {
    "view": RETURNS_READ,
    # The CUSTOMER's ask, filed through the API seam; a hand-written row here
    # would be a return nobody asked for.
    "add": None,
    "change": RETURNS_WRITE,
    # Deleting one erases the review trail, so not even the superuser bypass
    # reaches the form.
    "delete": None,
}


class ReturnRequestAdminForm(forms.ModelForm):
    """The change form's machine check, and the operator's answer when it fails.

    Putting the check here rather than in ``save_model`` is deliberate: an
    aborted ``save_model`` still gets logged by Django's ``log_change``, which
    would leave a "Changed status" entry on a row that never moved. ``old`` is
    read under ``select_for_update()`` inside the view's transaction, and
    ``ReturnRequest.save`` re-checks the edge regardless, so the guarantee is
    engine-independent.
    """

    class Meta:
        model = ReturnRequest
        # The admin overrides this with its own field list; the declaration here
        # is what lets the form be built and unit-tested on its own.
        fields = ("status",)

    def clean_status(self):
        status = self.cleaned_data["status"]
        if not self.instance.pk:
            # The add form cannot be reached, so a row with no pk never gets
            # here; the guard keeps the lookup honest rather than raising
            # DoesNotExist.
            return status
        with transaction.atomic():
            old = (
                ReturnRequest.objects.select_for_update()
                .get(pk=self.instance.pk)
                .status
            )
        if not return_transition_allowed(old, status):
            raise forms.ValidationError(
                f"A return request cannot move from '{old}' to '{status}'. "
                f"Allowed from '{old}': {_allowed_from(old)}."
            )
        return status


@admin.register(ReturnRequest)
class ReturnRequestAdmin(RoleAwareModelAdmin):
    """The staff half of a return: review it, then walk it.

    The queue is the changelist and the decision is the ``status`` field,
    checked by ``ReturnRequestAdminForm.clean_status`` and again by
    ``ReturnRequest.save``; the customer's own input renders read-only. There is
    deliberately no scoped second door, and no ``list_editable`` cell or bulk
    action, so the change form is the ONE admin write path.
    """

    form = ReturnRequestAdminForm
    capability_map = RETURN_CAPABILITY_MAP
    list_display = ("id", "order", "status", "reason_code", "created_at", "updated_at")
    list_filter = ("status", "reason_code", "created_at")
    # The order reference is how staff find the request; the pk is here for the
    # same reason RefundAdmin carries it.
    search_fields = ("id", "order__id", "order__order_number", "reason_code")
    readonly_fields = (
        "order",
        "reason_code",
        "reason_note",
        "created_at",
        "updated_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False  # asked for by the customer, never hand-written

    def has_delete_permission(self, request, obj=None):
        return False  # the review trail is never erased

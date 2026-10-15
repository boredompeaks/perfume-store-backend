import csv
from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone

from common.admin import RoleAwareModelAdmin
from common.audit import log_mutation, model_field_changes
from common.models import AuditEvent
from common.saved_filters import SavedFilterMixin

# [R-12.8] SPEC-12-02: the admin cancel writers release the checkout's
# stock holds with the same vocabulary the API twin uses.
from products.models import StockReservation

# [R-10.16] SPEC-10-05: the per-transition side-effect contract (one
# dispatch point, shared with the JSON seam).
from .events import notify_transition

# [R-10.1] The order machine lives in orders.state (single source); this
# module only consumes it.
# [R-1.16] SPEC-1-B07a: the return-request row and its own machine. Kept on
# its own import line so every hunk above stays insertion-only, and kept out
# of orders.state on purpose (that module is capability-scoped and audited).
from .models import (
    Coupon,
    Order,
    OrderItem,
    OrderStatusEvent,
    Refund,
    ReturnRequest,
    _allowed_from,
    return_transition_allowed,
)

# [R-10.1] SPEC-10-01b: the fulfilment-dimension mapping for the writers.
# [R-10.12] SPEC-10-02: the trigger vocabulary for the audit writers.
from .state import (
    ALLOWED_TRANSITIONS,
    FULFILMENT_QUEUE_STATUSES,
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


class OrderItemPackingInline(OrderItemInline):
    """The item lines as the PACKING operator needs them.

    Spec 1.1 line 110 gives packing and shipping to the inventory/fulfilment
    operator, which does not hold ``orders.read`` (order visibility) — so the
    per-line ``price``/``subtotal`` are the store's money, not its packing
    list, and this inline shows what to pick and how many of it.

    ``has_view_permission`` is the one thing it adds: Django skips an inline
    whose view, change, add and delete are all false (which is why the
    money-bearing parent never renders), and "what is in this parcel" is the
    whole point of the fulfilment surface. It stays a READ — the parent's
    add/change/delete denials are inherited unchanged.
    """

    fields = ("product_name", "sku", "variant_name", "quantity")
    verbose_name = "Item to pack"
    verbose_name_plural = "Items to pack"

    def has_view_permission(self, request, obj=None):
        return True


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
class OrderAdmin(SavedFilterMixin, RoleAwareModelAdmin):
    # SPEC-20-6 [R-20.11]: the saved-view bar rides first in the bases so it
    # wraps whichever changelist_view branch runs (this admin's transition
    # guards are on save_model, not on the view, so the merge is the only
    # thing in front of the grid).
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
    # [R-1-B03] Spec 1.1 line 110 puts packing and shipping on the
    # inventory/fulfilment operator, which holds ``orders.fulfill`` and
    # deliberately NOT ``orders.read`` (least privilege: it is not "see every
    # order"). Without this second door the operator's grid answers 200 on a
    # URL it can only guess, because its change capability is what Django
    # gates the changelist on, while the admin index hid the module and no
    # page listed the orders awaiting packing. The scoped viewer is therefore
    # admitted to the FULFILMENT queue below — a narrowed listing, not a wider
    # capability: ``orders.read`` itself stays where spec 1.1 puts it, so
    # ``export_csv`` (gated on it) is still refused.
    scoped_view_capability = "orders.fulfill"
    # The queue: statuses the fulfilment walk can still advance (spec line
    # 110's "packing, shipping"), read from orders.state so the listing can
    # never drift from the transitions it offers. Anything else (delivered,
    # cancelled) is not this operator's work and is not listed.
    scoped_queryset = FULFILMENT_QUEUE_STATUSES
    # Columns packing needs and nothing else. No total, discount, currency,
    # coupon or payment reference: those are the money columns orders.read
    # withholds, and a packer has no use for them.
    scoped_list_display = (
        "id",
        # [R-8.5] the customer-facing reference beside the internal pk
        "order_number",
        # Destination only: enough to sort a courier run, not a customer
        # record. Name, phone and email are not on this grid.
        "city",
        "status",
        "created_at",
    )
    # No phone / email / customer-name search: the operator may find an order
    # to pack by its reference, not by probing customer records
    # (``customers.read`` is a capability it does not hold).
    scoped_search_fields = ("id", "order_number")
    # The change form, narrowed the same way: what is going where, and what
    # state it is in. The user, phone, money and gateway columns stay out.
    scoped_fieldsets = (
        (
            "Shipping",
            {"fields": ("order_number", "address", "city", "state", "pincode")},
        ),
        ("Fulfilment", {"fields": ("status",)}),
        ("Timestamps", {"fields": ("created_at", "shipped_at", "delivered_at")}),
    )
    # [R-1-B03] cycle 3: what this door may WRITE. Declared, not derived, and
    # deny-by-default in the base — the grid above decides what a packer can
    # read, these two decide what it can commit, so a scoped fieldset can
    # never hand out a write by growing a field.
    #
    # Fulfilment state is the one editable field: advancing a packed order is
    # the whole point of this surface. Everything else it renders — the
    # checkout-minted reference and the delivery address included — is
    # read-only, so a packer cannot redirect a parcel: re-pointing where a
    # parcel goes is not packing or shipping authority (spec line 110), and it
    # is the destructive kind of edit nobody on this surface is here to make.
    scoped_writable_fields = frozenset({"status"})
    # And of the statuses, the one that belongs to somebody else. Cancelling
    # is [6.12.4]'s destructive authority on a financial record — confirmation
    # interstitial plus a captured reason on the sanctioned path — and it is
    # gated on ``orders.cancel``, which this role deliberately does not hold.
    # It is named here with the same capability the bulk ``cancel_pending``
    # action answers on (pinned equal by the test suite), so the change form,
    # the list-edit cell and the bulk action are one authority rather than
    # three that can drift.
    scoped_value_capabilities = {"status": {"cancelled": "orders.cancel"}}
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
    # SPEC-20-5 [R-20.28]: the only confirmed action that asks why. The
    # reason rides the interstitial and is merged into the cancel's change
    # message, mirroring the inventory path's reason/note capture
    # (AdjustStockForm) so a cancellation is audited with the operator's own
    # words instead of only the fact that it happened.
    confirmation_reason_actions = frozenset({"cancel_pending"})
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
        # [R-8.16] the two live business-event stamps beside the row;
        # refunded_at (written by the SPEC-1-05 refund seam) and the
        # still-unwritten fulfilled/shipped/delivered events stay off the
        # changelist until their writers land.
        "paid_at",
        "cancelled_at",
    )
    list_editable = ("status",)
    list_filter = ("status", "created_at")
    # [R-1.13] guest_email joins the customer search terms so a guest order is
    # findable by the same handle a customer order is (guest_email is the
    # only identity a guest row has). It is deliberately NOT in
    # scoped_search_fields: the packing operator may find an order to pack by
    # its reference, not by probing customer records (see B03 above).
    search_fields = (
        "id",
        "user__username",
        "user__email",
        "guest_email",
        "full_name",
        "phone",
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
    )
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
        (
            "Customer",
            # [R-1.13] guest_email rides the Customer group so staff read the
            # same field that identifies a guest order, next to the account it
            # replaces (empty on a customer order, the address on a guest's).
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

    @admin.display(description="Payment ref")
    def payment_ref(self, obj):
        return obj.razorpay_payment_id or "—"

    # ——— the scoped fulfilment surface (a scoped viewer, see above) ———

    # Each hook narrows ONE thing for a scoped viewer and defers to Django's
    # own accessor otherwise, so the full-surface behaviour of every other
    # role (and the superuser bypass) is byte-identical to before: the same
    # method, not a second code path. Declared state, never derived from the
    # request's role mid-flight. What the door may WRITE is not narrowed here
    # at all — it is declared once, above (``scoped_writable_fields`` /
    # ``scoped_value_capabilities``) and enforced by the base, which is where
    # the change form and the list-edit cell are both built from.

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
            # The money half of the item snapshot is withheld with the rest of
            # it. get_inlines yields CLASSES (Django instantiates them), so
            # the swap is a class substitution, not an instance.
            return [OrderItemPackingInline]
        return super().get_inlines(request, obj)

    def changelist_view(self, request, extra_context=None):
        """The grid a scoped viewer lands on IS the fulfilment queue.

        ``get_queryset`` above already restricts the rows to the statuses the
        packing walk can advance, and every surface that reads rows through
        this admin (``get_actions``' querysets, the saved-filter merge, the
        change view's ``get_object``) inherits that narrowing — so the actions
        cannot be pointed at an order outside the queue either. This override
        exists only to skip ``SavedFilterMixin`` for a scoped viewer: its write
        endpoints gate on ``has_view_permission``, which this role deliberately
        does not hold, so the bar would offer a form that can only ever 404.
        """
        if not self.is_scoped_viewer(request):
            return super().changelist_view(request, extra_context)
        return RoleAwareModelAdmin.changelist_view(self, request, extra_context)

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
                        "Cancelling a paid order needs a refund — issue "
                        "one via POST /api/admin/orders/<id>/refund/."
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

    @staticmethod
    def _locked_orders(queryset, pks):
        """The bulk writers' locked read of the snapshot pks.

        Both bulk writers hand their snapshot to this so the locked
        statement is built in one place: the writes below touch Order rows
        and nothing else, so the Order row is the only row that needs
        locking, and the locked query carries no join.

        The joins are stripped with ``select_related(None)`` because the
        queryset arrives from the changelist, where Django 6.1's
        ``ChangeList.get_select_related_fields`` adds a join for every
        ForeignKey named in ``list_display`` - and ``user`` and ``coupon``
        are both nullable here, so those joins are LEFT OUTER JOINs.
        FOR UPDATE over the nullable side of an outer join is rejected by
        PostgreSQL ("FOR UPDATE cannot be applied to the nullable side of
        an outer join") and silently dropped by SQLite, so without this
        every bulk action raised on Postgres and none of them could fail
        on SQLite.

        Stripping the join costs no extra query: the loop reads Order
        columns, ``order.items`` (a reverse FK, never covered by
        ``select_related``) and the transition preconditions, which are
        ``payment_method`` / ``payment_status`` scalars - the lifecycle
        notifications these writers fire have no registered handler, so
        nothing here dereferences ``order.user`` or ``order.coupon``.
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
                for order in self._locked_orders(queryset, matched_pks):
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

    # ——— the confirmation detail payload (SPEC-20-1 [R-20.20]) ———

    def confirmation_details(self, request, action_name, objects):
        """What cancelling this selection actually costs, before it happens.

        The amount and its currency, the lines affected and the state each
        selected row ends in. Rows the action will *not* touch (paid orders
        — cancelling those is a separate, refund-backed decision, see the
        refund seam) are spelled out too: skipping them is part of what the
        operator is confirming, and "3 orders, only 1 cancelled" is exactly
        the surprise this payload exists to prevent.
        """
        if action_name != "cancel_pending":
            return {}
        orders = list(objects)
        cancelable = [order for order in orders if order.status == "pending"]
        # Money stays Decimal end to end (conventions.md:15) and the
        # roll-up is quantized before it is rendered. Currencies are listed
        # rather than assumed: the denomination is a setting, not a constant.
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
        # SPEC-20-1b: a row is skipped because it is NOT pending, and only
        # one of those reasons is "it is paid". An already-cancelled order
        # used to be told "paid, never cancelled here", which is false on
        # screen and in the operator's decision. The resulting state
        # ("-> unchanged") was always right; only the reason clause moves.
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

        SPEC-20-5 [R-20.28]: the operator's optional reason from the
        interstitial is merged in, so the trail carries WHY an order was
        cancelled and not only that it was. With no reason typed the message
        is exactly the one shipped since SPEC-6-04 — the capture is additive
        and never rewrites the audit contract.
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
                # [R-10.12] SPEC-10-02: per-row saves in one atomic block,
                # each row's status re-checked at write time under the row
                # lock (same final-authority pattern as _bulk_set_status) —
                # a row that flipped pending→confirmed between the snapshot
                # and this write is a PAID order now, and cancelling paid
                # orders is impossible by design, so it is skipped, never
                # swept. Per-row saves also fire auto_now (no stale
                # updated_at) and carry the audit event per row.
                for order in self._locked_orders(queryset, unpaid_pks):
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
                    # [R-1.13] `customer_name` resolves the guest's email when
                    # the row has no account, so an export of a mixed queue
                    # cannot raise on the guest orders in it.
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

    def save_model(self, request, obj, form, change):
        """Record the real before -> after value of a promotion edit.

        SPEC-20-4 [R-20.27]: Django's own changelist-edit LogEntry names the
        column ("Changed Active.") but not what it changed FROM or TO, so a
        deactivated coupon cannot be reconstructed from the trail. This is
        the ``list_editable`` ``active`` toggle, the only field editable
        outside the change form; the change form's own structured LogEntry
        already carries its old/new values.

        The snapshot is read from the DATABASE, not off the instance:
        ``save_form`` has already applied the submitted values by the time
        ``save_model`` runs, so the instance holds the new state and would
        report no change at all.
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
    """[R-1.14] SPEC-1-05: a read-only window onto issued refunds.

    ``refunds.create`` gates the surface, so it is finance who reconciles the
    money (spec 1.1's finance operator) plus admin — support, which may read
    and fulfil orders, gets no refund page at all. No staff role gets
    add/change/delete, and the superuser bypass is refused on add and delete
    too: a refund is issued by the API seam, the one writer that calls the
    gateway, so a hand-written row here would be money movement with no
    payment behind it. Every field renders read-only, so even the change view
    is a read, not an editor.
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


# [R-1.16] SPEC-1-B07a: the return-request surface (spec 6.8 line 1949's
# `/admin/returns`). Read is ``returns.read`` and every write is
# ``returns.write``, so a role can be given the queue without the decision.
RETURNS_READ = "returns.read"
RETURNS_WRITE = "returns.write"

RETURN_CAPABILITY_MAP = {
    "view": RETURNS_READ,
    # A return request is the CUSTOMER's ask (spec 4 line 1083), filed through
    # the API seam; a hand-written row here would be a return nobody asked for.
    "add": None,
    "change": RETURNS_WRITE,
    # Deleting one erases the review trail spec 6.8 line 1983 requires, so no
    # staff role gets it - not even the superuser bypass reaches the form.
    "delete": None,
}


class ReturnRequestAdminForm(forms.ModelForm):
    """The change form's machine check, and the operator's answer when it fails.

    Validation is the right layer for "this value is not a legal value FOR THIS
    ROW", and putting it here rather than in ``save_model`` is deliberate: an
    aborted ``save_model`` still gets logged by Django's own ``log_change``
    (it runs after ``save_model`` returns, unconditionally), so a refusal that
    way would leave a "Changed status" entry on a row whose status never moved.
    A field error is refused before ``save_model`` is reached at all, so the
    refusal leaves the row untouched AND writes no misleading audit entry.

    ``old`` is read under ``select_for_update()`` inside an explicit atomic
    block - and Django already wraps the whole change-form view in a
    transaction, so the lock is held for the check AND the subsequent write: two
    operators editing the same request cannot both pass against a status the
    database has already left. (SQLite emits no FOR UPDATE, so the lock is inert
    there; ``ReturnRequest.save`` re-checks the edge against the stored status
    regardless, which is what makes the guarantee engine-independent.)
    """

    class Meta:
        model = ReturnRequest
        # ``status`` is the only editable field on this surface. The admin
        # overrides this with its own field list (built from ``readonly_fields``
        # below), so the declaration here is what lets the form be built and
        # unit-tested on its own.
        fields = ("status",)

    def clean_status(self):
        status = self.cleaned_data["status"]
        if not self.instance.pk:
            # The add form cannot be reached (has_add_permission is False below),
            # so a row with no pk never gets here; the guard keeps the lookup
            # honest if that ever changes rather than raising DoesNotExist.
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
    """[R-1.16] The staff half of a return: review it, then walk it.

    This is the whole of spec 6.8 line 1957's "Return request review" for this
    task: the queue is the changelist, and the decision is the ``status`` field
    on the change form, checked against the return machine by
    ``ReturnRequestAdminForm.clean_status`` and again by ``ReturnRequest.save``
    behind it. ``order``, ``reason_code`` and ``reason_note`` are the customer's
    own input and render read-only, so an operator acts on a request by walking
    its status and can never rewrite what was asked for.

    That leaves this surface with no place for an operator's own words - the
    spec's "Manual exception workflow" and the decision note have nowhere to
    live yet. It is a named gap, not an oversight: SPEC-1-B07b owns the
    returns serializer and the return/refund audit trail (spec 6.8 line 1983),
    and a staff note belongs with that work rather than as a second column
    invented here.

    NO ``scoped_view_capability``: this surface has one door. The narrow second
    door SPEC-1-B03 built for the packing operator has no business here, and
    declaring one would also be the way to widen a role's capability set that
    ``tests/test_superadmin_tier.py`` pins exactly.

    ONE write path. There is no ``list_editable`` cell and no bulk action on
    this surface, so the change form is the only admin path that can move a
    status - which is what makes ``ReturnRequestQuerySet`` refusing
    ``update(status=...)`` a statement about the whole surface rather than
    about one of two doors. ``ReturnRequestAdminForm`` is what that one path
    asks before it writes.
    """

    form = ReturnRequestAdminForm
    capability_map = RETURN_CAPABILITY_MAP
    list_display = ("id", "order", "status", "reason_code", "created_at", "updated_at")
    list_filter = ("status", "reason_code", "created_at")
    # The order reference is how staff find the request (spec 6.8 pairs every
    # returns surface with its order); the pk is here for the same reason
    # RefundAdmin carries it.
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

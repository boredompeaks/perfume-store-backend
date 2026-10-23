import copy
import csv
from functools import partial

from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.forms import modelformset_factory
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.html import format_html, mark_safe

from common.admin import DEFAULT_CONFIRM_WARNING, RoleAwareModelAdmin
from common.saved_filters import SavedFilterMixin

from .models import ProductVariant, StockMovement, products

# SPEC-20-11 [R-20.35]: the ledger stamp carried by a changelist inline
# stock edit. An absolute count typed into a grid cell is a correction of
# the recorded on-hand figure, which is what Reason.CORRECTION means; the
# note names the surface so the ledger reads self-explanatory.
INLINE_STOCK_FIELD = "stock"
INLINE_STOCK_REASON = StockMovement.Reason.CORRECTION
INLINE_STOCK_NOTE = "changelist inline stock edit"


class AdjustStockForm(forms.Form):
    delta = forms.IntegerField(
        label="Change in stock",
        help_text="Positive to add units, negative to remove (e.g. 20 or -3).",
    )
    reason = forms.ChoiceField(choices=StockMovement.Reason.choices)
    note = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class ChangelistStockForm(forms.ModelForm):
    """The changelist's inline-edit form (SPEC-20-11 [R-20.35]).

    ``stock`` rides the same grid as ``price`` — the spec asks for the two
    to be editable the same way — but this form is only the *input* surface:
    ``ProductAdmin.save_model`` narrows the formset's own UPDATE to the
    non-stock columns and routes the count to ``products.adjust_stock``, so
    the submitted number never lands on the column without its
    StockMovement ledger row ([6.5.17] — no silent inventory edits).

    ``Meta.fields`` is overridden by the changelist formset factory
    (``fields=list_editable``); ``InlineStockSurfaceTests`` pins the two to
    the same set so a new ``list_editable`` column cannot be added here
    without its form field.
    """

    class Meta:
        model = products
        fields = ("price", "stock")


class StockMovementInline(admin.TabularInline):
    model = StockMovement
    extra = 0
    can_delete = False
    fields = ("delta", "stock_after", "reason", "note", "created_by", "created_at")
    readonly_fields = fields
    verbose_name = "Inventory adjustment"
    verbose_name_plural = "Inventory history (all mutations)"

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(products)
class ProductAdmin(SavedFilterMixin, RoleAwareModelAdmin):
    # SPEC-20-6 [R-20.11]: the saved-view bar rides first in the bases so
    # the saved-filter merge happens before BOTH branches of the
    # changelist_view below — including the view-only copy this admin hands
    # to a role without inventory.adjust.
    # Role-aware least privilege (spec 6.12): the catalogue team owns the
    # product lifecycle; hard delete rides ``products.publish`` because it
    # is at least as sensitive as unpublishing (a later narrowing of
    # publish would narrow delete with it).
    capability_map = {
        "view": "products.read",
        "add": "products.write",
        "change": "products.write",
        "delete": "products.publish",
    }
    action_capabilities = {
        # adjust_stock is the sanctioned inventory mutation, so it needs
        # ``inventory.adjust`` — not merely a products permission. The
        # changelist's inline stock cell reads its gate from this entry
        # (``inline_stock_capability``): same mutation, same capability.
        "adjust_stock": "inventory.adjust",
        "export_csv": "products.read",
        # MERCHANT-DELIST: taking a product off sale (and putting it back)
        # rides ``products.publish`` — the capability the delete above
        # already leans on "because it is at least as sensitive as
        # unpublishing", which names this exact power. Deliberately NOT
        # ``products.write``: that capability gates ordinary row edits,
        # while this one decides whether the store sells the row at all —
        # a narrower set of roles (catalogue + admin) may make that call.
        "delist_products": "products.publish",
        "relist_products": "products.publish",
    }
    # adjust_stock renders its own deliberate input form (delta/reason +
    # Apply) — that form is the explicit confirmation [6.12.4], so it stays
    # off confirmation_required_actions.
    #
    # MERCHANT-DELIST-C2 (spec 4814: confirmation dialogs on
    # publishing/unpublishing large batches): delist and relist are that
    # batch publish/unpublish, so both directions of the same flip are
    # behind the shared interstitial the mixin already owns. Cycle 1's
    # comment declined the interstitial because the action is reversible
    # — but the confirmation is not about destructiveness, it is about a
    # batch that changes what the storefront sells, and the superuser
    # keeps the mixin's direct-execution bypass either way.
    confirmation_required_actions = frozenset({"delist_products", "relist_products"})
    # The shared interstitial text says the action "cannot be undone" —
    # true of cancel_pending, FALSE of a reversible state flip whose undo
    # is the sibling action in the same dropdown. render_confirmation
    # takes the warning as a parameter for exactly this: the dialog says
    # what THIS action does instead of inheriting a sentence about a
    # different action's permanence. No new mechanism — the parameter is
    # the existing seam; the map only exists because two actions here
    # need a sentence the default cannot truthfully carry.
    ACTION_WARNINGS = {
        "delist_products": (
            "takes the selected products off the public catalogue until "
            "they are put back on sale. Confirm to continue."
        ),
        "relist_products": (
            "puts the selected products back on the public catalogue. "
            "Confirm to continue."
        ),
    }

    def render_action_confirmation(self, request, action, queryset):
        return super().render_confirmation(
            request,
            action_name=action.name,
            description=action.description,
            objects=queryset,
            warning=self.ACTION_WARNINGS.get(action.name, DEFAULT_CONFIRM_WARNING),
        )

    list_display = (
        "thumb",
        "name",
        "category",
        "price",
        "size",
        "stock",
        "stock_flag",
        "on_sale_flag",
        "created_at",
    )
    # SPEC-20-11 [R-20.35]: `stock` is inline-editable next to `price`. The
    # affordance is not the contract — every inline change is turned into a
    # delta and written through adjust_stock, so each one still lands a
    # StockMovement ledger row ([6.5.17]). Without the capability the field
    # is display-only again (changelist_view) and the column stays visible.
    list_editable = ("price", "stock")
    list_filter = ("category", "is_active", "created_at")
    search_fields = ("name", "slug", "description")
    ordering = ("-created_at",)
    readonly_fields = ("slug", "created_at", "image_preview")
    list_per_page = 25
    actions = ("adjust_stock", "export_csv", "delist_products", "relist_products")
    inlines = (StockMovementInline,)

    @property
    def inline_stock_capability(self):
        """Capability the inline stock cell rides.

        Read off ``action_capabilities`` rather than repeated as a literal:
        the inline affordance and the sanctioned adjust-stock action are the
        same mutation, so one declared gate keeps them from drifting apart.
        """
        return self.action_capabilities["adjust_stock"]

    def get_readonly_fields(self, request, obj=None):
        # The change form's twin of ProductSerializer's update-time read-only
        # `stock`: a change-page save is a stock edit on an existing row, so it
        # must not bypass the StockMovement ledger ([6.5.17]). The changelist
        # grid is the one place an existing row's count is editable, and
        # ProductAdmin.save_model is what routes that edit to the ledger;
        # this form has no ledger route, so it stays display-only. `stock`
        # stays editable on the add form: creation sets the opening balance,
        # which is not an edit.
        fields = super().get_readonly_fields(request, obj)
        if obj is None:
            return fields
        return fields + ("stock",)

    # slug stays out of prepopulated_fields: it is readonly (the model
    # generates it) and prepopulation on a readonly field crashes the template.
    fieldsets = (
        ("Product", {"fields": ("name", "slug", "category", "description")}),
        (
            "Pricing & inventory",
            {"fields": ("price", "size", "stock", "free_shipping")},
        ),
        ("Image", {"fields": ("image", "image_preview")}),
    )

    def changelist_view(self, request, extra_context=None):
        if self._holds_capability(request, self.inline_stock_capability):
            return super().changelist_view(request, extra_context)
        # No adjust capability -> the inline stock cell is withdrawn, matching
        # how the adjust-stock action disappears from the dropdown (get_actions
        # gate). The registered ModelAdmin is a process-wide singleton, so the
        # narrowed list_editable lives on a throwaway shallow copy; writing it
        # on ``self`` would leak one request's role into the next one's grid.
        view_only = copy.copy(self)
        view_only.list_editable = tuple(
            field for field in self.list_editable if field != INLINE_STOCK_FIELD
        )
        return super(ProductAdmin, view_only).changelist_view(request, extra_context)

    def get_changelist_formset(self, request, **kwargs):
        """Changelist formset whose form is ``ChangelistStockForm``.

        Same construction as ``ModelAdmin.get_changelist_formset`` (form
        built from ``list_editable``, callback bound to the request), with the
        inline form in the base slot so ``save_model`` can recognise the
        surface. This replaces the super call instead of wrapping it: Django
        passes both the base form and ``fields=list_editable`` positionally
        and by keyword, so a second ``form=`` would be a duplicate argument.
        """
        return modelformset_factory(
            self.model,
            self.get_changelist_form(request, form=ChangelistStockForm),
            extra=0,
            fields=self.list_editable,
            formfield_callback=partial(self.formfield_for_dbfield, request=request),
            **kwargs,
        )

    def save_model(self, request, obj, form, change):
        # Shared by three surfaces: the add form, the change form, and the
        # changelist formset (Django's _save_formset calls this per changed
        # row). Only the changelist's inline form needs the ledger route.
        if not isinstance(form, ChangelistStockForm):
            return super().save_model(request, obj, form, change)
        # The formset hands us the submitted count on `obj`; an ordinary save
        # would write it straight to the column — the ledger-free mutation
        # [6.5.17] forbids. So the formset's own UPDATE is narrowed to the
        # inline columns the editor actually touched, minus `stock`, and the
        # count is applied as a delta through adjust_stock, which re-reads the
        # row under select_for_update (conventions.md:16) and writes the
        # StockMovement row naming the acting staff user.
        inline_columns = [
            name
            for name in form.changed_data
            if name not in (obj._meta.pk.name, INLINE_STOCK_FIELD)
        ]
        if inline_columns:
            obj.save(update_fields=inline_columns)
        # A grid rendered without the cell (no adjust capability) has nothing
        # to route, and an untouched cell yields delta 0: no mutation, no
        # ledger row. Either way `requested` is absent, never a silent write.
        loaded = form.initial.get(INLINE_STOCK_FIELD)
        requested = form.cleaned_data.get(INLINE_STOCK_FIELD)
        if requested is not None and requested != loaded:
            obj.adjust_stock(
                request.user,
                requested - loaded,
                INLINE_STOCK_REASON,
                INLINE_STOCK_NOTE,
            )

    @admin.display(description="Image")
    def thumb(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="height:36px;width:auto;border-radius:3px;" />',
                obj.image.url,
            )
        return "—"

    @admin.display(description="Stock", ordering="stock")
    def stock_flag(self, obj):
        if obj.stock == 0:
            # no interpolation -> mark_safe (format_html requires args/kwargs)
            return mark_safe('<strong style="color:#c62828;">out of stock</strong>')
        if obj.stock <= 5:
            return format_html(
                '<strong style="color:#e6a700;">low ({})</strong>', obj.stock
            )
        return obj.stock

    @admin.display(description="On sale", ordering="is_active", boolean=True)
    def on_sale_flag(self, obj):
        # MERCHANT-DELIST: the one staff-visible marker of a delisted row.
        # The public catalogue 404s delisted products, so without this
        # column (and the ``is_active`` list filter) there would be no way
        # to find a delisted row to relist it.
        return obj.is_active

    @admin.display(description="Preview")
    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height:200px;border-radius:6px;" />',
                obj.image.url,
            )
        return "No image uploaded."

    @admin.action(description="Adjust stock of selected products…")
    def adjust_stock(self, request, queryset):
        if "apply" in request.POST:
            form = AdjustStockForm(request.POST)
            if form.is_valid():
                delta = form.cleaned_data["delta"]
                reason = form.cleaned_data["reason"]
                note = form.cleaned_data["note"]
                adjusted, failed = [], []
                for product in queryset:
                    try:
                        product.adjust_stock(request.user, delta, reason, note)
                        adjusted.append(product)
                    except ValueError as exc:
                        failed.append(str(exc))
                if adjusted:
                    self.log_bulk_action(
                        request,
                        adjusted,
                        f"Bulk action: stock adjusted ({delta:+d}).",
                    )
                    self.message_user(
                        request,
                        f"Stock adjusted ({delta:+d}) for {len(adjusted)} product(s).",
                        messages.SUCCESS,
                    )
                for message in failed:
                    self.message_user(request, message, messages.ERROR)
                return None  # back to the changelist with messages
        else:
            form = AdjustStockForm(
                initial={"delta": 10, "reason": StockMovement.Reason.RESTOCK}
            )

        return render(
            request,
            "admin/adjust_stock.html",
            {
                "title": "Adjust stock",
                "form": form,
                "products": queryset,
                "opts": self.model._meta,
            },
        )

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="products.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["id", "name", "slug", "category", "price", "size", "stock", "created_at"]
        )
        for product in queryset:
            writer.writerow(
                [
                    product.id,
                    product.name,
                    product.slug,
                    product.category,
                    product.price,
                    product.size,
                    product.stock,
                    product.created_at,
                ]
            )
        return response

    # ==================================
    # MERCHANT-DELIST: delist / relist
    # ==================================

    # One wording for both surfaces of the same outcome (the LogEntry and
    # the flash message), so the audit trail and the UI cannot drift apart.
    DELIST_LOG_MESSAGE = "Bulk action: taken off sale (delisted)."
    RELIST_LOG_MESSAGE = "Bulk action: put back on sale (relisted)."

    def _set_on_sale(self, request, queryset, on_sale):
        """MERCHANT-DELIST's one writer: flip ``is_active`` on the selected
        rows that do not already hold the target state, and nothing else.

        Delist is reversible state, not an inventory mutation: the stock
        column and the StockMovement ledger are deliberately untouched
        ([6.5.17] is about inventory changes; a state flip writes no
        movement row). ``update()`` over the captured pks rather than
        per-row ``save()``: the flip is an absolute value, not a
        read-modify-write like a stock delta, so there is nothing to
        serialize and one statement is enough. The targets are captured
        BEFORE the update (re-filtering afterwards would match nothing --
        the rows just left that state), so the LogEntry names exactly the
        rows the action actually moved, matching the adjust-stock bulk
        action's audit contract ([6.12.6]: a privileged action is never
        unlogged).
        """
        targets = list(queryset.filter(is_active=not on_sale))
        if not targets:
            # Idempotent rest state rather than an error: selecting only
            # already-delisted rows for delist costs nothing and changes
            # nothing, and the no-op writes no LogEntry (an entry would
            # claim a mutation that did not happen).
            self.message_user(
                request,
                "The selected products are already "
                + ("on sale." if on_sale else "off sale."),
                messages.INFO,
            )
            return None
        log_message = self.RELIST_LOG_MESSAGE if on_sale else self.DELIST_LOG_MESSAGE
        with transaction.atomic():
            products.objects.filter(pk__in=[target.pk for target in targets]).update(
                is_active=on_sale
            )
            self.log_bulk_action(request, targets, log_message)
            if on_sale:
                # MERCHANT-DELIST-C2: the release half of the deferred
                # back-in-stock notification. A restock that landed while
                # the row was off sale was suppressed (the mail would link
                # a 404 detail page), and its waiting opt-ins were never
                # claimed -- so relist is the moment the promise "you can
                # buy it again" becomes true, and the SAME fan-out runs
                # then (registered on this commit hook, exactly like the
                # stock crossing's; the claim machinery dedupes). Only
                # rows actually in stock register: a relist at stock 0
                # has nothing to announce, and the row's next 0 ->
                # positive crossing -- now that it is active again --
                # fires the usual way. The fan-out re-checks is_active
                # from the row at fire time, so a product re-delisted
                # between here and the hook cannot mail either.
                for target in products.objects.filter(
                    pk__in=[t.pk for t in targets], is_active=True, stock__gt=0
                ):
                    products.register_back_in_stock_release(target)
        self.message_user(
            request,
            f"{len(targets)} product(s) "
            + ("put back on sale." if on_sale else "taken off sale."),
            messages.SUCCESS,
        )
        return None

    @admin.action(description="Take selected products off sale")
    def delist_products(self, request, queryset):
        # Reversible and non-destructive (nothing is deleted, stock and the
        # ledger are untouched), and the relist action is the undo -- but
        # it still confirms first (see confirmation_required_actions): the
        # interstitial is about a batch that changes what the storefront
        # sells, not about whether the row survives [spec 4814].
        return self._set_on_sale(request, queryset, on_sale=False)

    @admin.action(description="Put selected products back on sale")
    def relist_products(self, request, queryset):
        return self._set_on_sale(request, queryset, on_sale=True)


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    """Bare registration (SPEC-8-02a): the variant entity is visible with
    default ModelAdmin behaviour, gated by Django's default per-model
    permissions — no role holds them until SPEC-6-08 wires the capability
    map. Fieldsets/actions/role matrix are deliberately absent."""

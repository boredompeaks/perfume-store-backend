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
    """The changelist's inline-edit form.

    ``stock`` is only the input surface: ``save_model`` routes it to
    ``products.adjust_stock`` so the column never changes without its ledger
    row; ``Meta.fields`` is overridden by the changelist formset factory and
    pinned by a test.
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
    # SavedFilterMixin rides first so the saved-filter merge precedes both
    # changelist_view branches. Delete rides ``products.publish``.
    capability_map = {
        "view": "products.read",
        "add": "products.write",
        "change": "products.write",
        "delete": "products.publish",
    }
    action_capabilities = {
        # adjust_stock is the sanctioned inventory mutation, so it needs
        # ``inventory.adjust``; the inline stock cell reads its gate here.
        "adjust_stock": "inventory.adjust",
        "export_csv": "products.read",
        # Delist/relist decide whether the store sells the row, so they ride
        # ``products.publish``, not ``products.write``.
        "delist_products": "products.publish",
        "relist_products": "products.publish",
    }
    # adjust_stock renders its own confirm form, so it stays off
    # confirmation_required_actions; delist/relist confirm as batch publishes.
    confirmation_required_actions = frozenset({"delist_products", "relist_products"})
    # The shared interstitial says "cannot be undone", false for a reversible
    # flip; ``render_confirmation`` takes the warning as a parameter for this.
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
    # ``stock`` is inline-editable, but each edit is routed to adjust_stock and
    # lands a ledger row; without the capability it is display-only.
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
        """Capability the inline stock cell rides, read off
        ``action_capabilities`` so the affordance and the sanctioned action
        cannot drift apart.
        """
        return self.action_capabilities["adjust_stock"]

    def get_readonly_fields(self, request, obj=None):
        # A change-page save must not bypass the ledger; the grid routes via
        # save_model. On the add form stock is the opening balance, not an edit.
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
        # No adjust capability -> no inline stock cell. The admin is a
        # process-wide singleton: narrow list_editable on a shallow copy.
        view_only = copy.copy(self)
        view_only.list_editable = tuple(
            field for field in self.list_editable if field != INLINE_STOCK_FIELD
        )
        return super(ProductAdmin, view_only).changelist_view(request, extra_context)

    def get_changelist_formset(self, request, **kwargs):
        """Changelist formset whose form is ``ChangelistStockForm``.

        Replaces rather than wraps the super call: Django passes ``form`` both
        positionally and by keyword, so a second one would be a duplicate
        argument.
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
        # Shared by add/change forms and the changelist formset (Django calls
        # this per row); only the inline form needs the ledger route.
        if not isinstance(form, ChangelistStockForm):
            return super().save_model(request, obj, form, change)
        # Writing the count directly would be ledger-free: narrow the UPDATE to
        # non-stock columns, then apply the delta through adjust_stock.
        inline_columns = [
            name
            for name in form.changed_data
            if name not in (obj._meta.pk.name, INLINE_STOCK_FIELD)
        ]
        if inline_columns:
            obj.save(update_fields=inline_columns)
        # A grid without the cell has nothing to route, and an untouched cell
        # yields delta 0: no mutation, no ledger row, never a silent write.
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
        # The one staff-visible marker of a delisted row: the public catalogue
        # 404s them, so this column and the is_active filter are how they are found.
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
                return None
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

    # One wording for both the LogEntry and the flash message.
    DELIST_LOG_MESSAGE = "Bulk action: taken off sale (delisted)."
    RELIST_LOG_MESSAGE = "Bulk action: put back on sale (relisted)."

    def _set_on_sale(self, request, queryset, on_sale):
        """MERCHANT-DELIST's one writer: flip ``is_active`` on the selected rows.

        Delist is a reversible state flip, not inventory: stock and the
        StockMovement ledger stay untouched. Targets are captured before the
        bulk ``update()`` so the LogEntry names exactly the rows that moved.
        """
        targets = list(queryset.filter(is_active=not on_sale))
        if not targets:
            # Idempotent rest state, not an error: a no-op writes no LogEntry,
            # which would claim a mutation that did not happen.
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
                # Relist makes "you can buy it again" true: register the same
                # fan-out for in-stock rows (the claim machinery dedupes).
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
        # Reversible and non-destructive, but it still confirms: the
        # interstitial is about a batch that changes what the storefront sells.
        return self._set_on_sale(request, queryset, on_sale=False)

    @admin.action(description="Put selected products back on sale")
    def relist_products(self, request, queryset):
        return self._set_on_sale(request, queryset, on_sale=True)


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    """Bare registration with one deliberate omission: ``stock`` is excluded.

    ``ProductVariant.stock`` is inert by design — checkout never reads it — so
    an editable box would present a number as real inventory; default
    ModelAdmin behaviour applies otherwise.
    """

    exclude = ("stock",)

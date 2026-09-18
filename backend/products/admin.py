import csv

from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.html import format_html, mark_safe
from django import forms

from .models import StockMovement, products


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


class StockMovementInline(admin.TabularInline):
    model = StockMovement
    extra = 0
    can_delete = False
    fields = ("delta", "stock_after", "reason", "note", "created_by", "created_at")
    readonly_fields = fields
    verbose_name = "Inventory adjustment"
    verbose_name_plural = "Inventory history (manual adjustments)"

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(products)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "thumb",
        "name",
        "category",
        "price",
        "size",
        "stock",
        "stock_flag",
        "created_at",
    )
    list_editable = ("price", "stock")
    list_filter = ("category", "created_at")
    search_fields = ("name", "slug", "description")
    ordering = ("-created_at",)
    readonly_fields = ("slug", "created_at", "image_preview")
    list_per_page = 25
    actions = ("adjust_stock", "export_csv")
    inlines = (StockMovementInline,)
    # slug stays out of prepopulated_fields: it is readonly (the model
    # generates it) and prepopulation on a readonly field crashes the template.
    fieldsets = (
        ("Product", {"fields": ("name", "slug", "category", "description")}),
        ("Pricing & inventory", {"fields": ("price", "size", "stock")}),
        ("Image", {"fields": ("image", "image_preview")}),
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
            return format_html('<strong style="color:#e6a700;">low ({})</strong>', obj.stock)
        return obj.stock

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
                ok, failed = 0, []
                for product in queryset:
                    try:
                        product.adjust_stock(request.user, delta, reason, note)
                        ok += 1
                    except ValueError as exc:
                        failed.append(str(exc))
                if ok:
                    self.message_user(
                        request,
                        f"Stock adjusted ({delta:+d}) for {ok} product(s).",
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

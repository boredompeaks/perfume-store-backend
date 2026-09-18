from django.contrib import admin

from .models import SiteSettings

admin.site.site_header = "Maison Aurel — store admin"
admin.site.site_title = "Maison Aurel admin"
admin.site.index_title = "Store operations"


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Singleton settings row — no add/delete, only edit."""

    list_display = ("support_email", "support_phone", "whatsapp_number", "instagram_url", "updated_at")
    fieldsets = (
        (
            "Customer contact channels (blank = hidden on the storefront)",
            {
                "fields": (
                    "support_email",
                    "support_phone",
                    "whatsapp_number",
                    "whatsapp_message",
                    "instagram_url",
                )
            },
        ),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).filter(pk=1)

    def changelist_view(self, request, extra_context=None):
        # Singleton: the changelist would show a single row — send editors
        # straight to the change form.
        from django.shortcuts import redirect

        obj = self.get_queryset(request).first()
        if obj:
            return redirect("admin:ops_sitesettings_change", object_id=str(obj.pk))
        return super().changelist_view(request, extra_context)

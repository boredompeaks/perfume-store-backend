from django.contrib import admin

from common.admin import RoleAwareModelAdmin
from .models import SiteSettings

admin.site.site_header = "Maison Aurel — store admin"
admin.site.site_title = "Maison Aurel admin"
admin.site.index_title = "Store operations"


@admin.register(SiteSettings)
class SiteSettingsAdmin(RoleAwareModelAdmin):
    """Singleton settings row — no add/delete, only edit."""

    # Store settings are the sensitive surface ``settings.manage`` guards
    # (spec 6.12), so both seeing and editing them is privilege-tier
    # territory: spec 1.1 gives settings to Admin (line 137) and to the
    # superadmin tier above it (line 146), and to no one else.
    capability_map = {
        "view": "settings.manage",
        "change": "settings.manage",
        # add/delete: the subclass overrides below keep them impossible for
        # everyone (singleton), as before.
    }
    list_display = (
        "support_email",
        "support_phone",
        "whatsapp_number",
        "instagram_url",
        "return_window_days",
        "updated_at",
    )
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
        (
            # SPEC-1-B07d. Its own fieldset rather than a sixth field on the
            # contact row: this is store POLICY and the gate reads it, while
            # every field above is a channel the storefront merely displays.
            # ``fieldsets`` is explicit here, so without this entry the column
            # would exist, be writable through the ORM, and be UNREACHABLE in
            # the one surface the merchant has for editing settings.
            "Store policy (read by the returns gate; blank = store default)",
            {"fields": ("return_window_days",)},
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

from django.contrib import admin

from common.admin import RoleAwareModelAdmin
from .models import (
    CLOSED_RETURN_WINDOW_DAYS,
    DEFAULT_RETURN_WINDOW_DAYS,
    SiteSettings,
)

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
            {
                "fields": ("return_window_days",),
                # SPEC-1-B07f-a: what 0 DOES, in the one form the merchant
                # reads. The fieldset DESCRIPTION rather than the field's
                # ``help_text`` because ``help_text`` is a model field
                # attribute and restating it writes a migration - a poor price
                # for prose. Both render on this same change page and this one
                # sits directly above the input, so the merchant who types 0 is
                # not left to infer it from the field's name.
                "description": (
                    "<p><strong>Return window.</strong> The days a customer has "
                    "to request a return, counted from delivery where the goods "
                    "have arrived and from the order date otherwise, with the "
                    "last day counted as inside the window. Leave blank to "
                    "publish the store default of "
                    f"{DEFAULT_RETURN_WINDOW_DAYS} days. <strong>Set it to "
                    f"{CLOSED_RETURN_WINDOW_DAYS} to close returns entirely:"
                    " every return request is then refused, whatever the age of "
                    "the order and including one made at the very moment of "
                    "delivery, and the customer is told returns are not "
                    "available rather than that the window expired.</p>"
                ),
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

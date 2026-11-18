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

    # Settings are the sensitive surface ``settings.manage`` guards:
    # Admin and the superadmin tier above it, and no one else.
    capability_map = {
        "view": "settings.manage",
        "change": "settings.manage",
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
            # ``fieldsets`` is explicit: a field not named here is unreachable
            # in the merchant's only editing surface.
            "Store identity",
            {
                "fields": ("name", "logo", "locale"),
                "description": (
                    "<p>The name and mark the storefront shows. A blank name is "
                    "published as blank, and the frontend falls back to its "
                    "configured name; a logo is published to "
                    "<code>/api/settings/</code> as an absolute URL.</p>"
                ),
            },
        ),
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
            # Store POLICY the returns gate reads, not a storefront display
            # channel; without its own fieldset the column would be unreachable.
            "Store policy (read by the returns gate; blank = store default)",
            {
                "fields": ("return_window_days",),
                # The fieldset description, not the field's ``help_text``:
                # editing ``help_text`` would write a migration for prose.
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
        (
            # Its own fieldset because it changes what every customer sees;
            # the gating that acts on the flag lives elsewhere.
            "Store availability",
            {
                "fields": ("maintenance_mode",),
                "description": (
                    "<p><strong>Maintenance mode</strong> publishes "
                    "<code>maintenance_mode</code> on <code>/api/settings/</code>. "
                    "The storefront's maintenance page and the gating that sends "
                    "customers to it are SPEC-3-06; this switch only publishes "
                    "the flag.</p>"
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
        # Singleton: send editors straight to the change form.
        from django.shortcuts import redirect

        obj = self.get_queryset(request).first()
        if obj:
            return redirect("admin:ops_sitesettings_change", object_id=str(obj.pk))
        return super().changelist_view(request, extra_context)

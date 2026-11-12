"""Admin site that enforces mandatory TOTP for privileged staff and exposes the
enrollment URL to the login template.
"""

from django.conf import settings
from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig


class MFAAdminSite(AdminSite):
    @property
    def login_form(self):
        # Lazy import: this module loads as an AppConfig before the app registry is
        # ready, and accounts.admin imports models.
        from accounts.admin import MFAAdminAuthenticationForm

        return MFAAdminAuthenticationForm

    def each_context(self, request):
        context = super().each_context(request)
        context["mfa_enroll_url"] = settings.MFA_ENROLL_URL
        return context

    def login(self, request, extra_context=None):
        response = super().login(request, extra_context)
        # The mandatory-MFA block arrives as a NON-field form error, so the template
        # keys off this flag rather than matching message text.
        context = getattr(response, "context_data", None)
        if context is None:
            return response
        from accounts.models import MFA_ENROLLMENT_REQUIRED

        form = context.get("form")
        context["mfa_enrollment_blocked"] = any(
            MFA_ENROLLMENT_REQUIRED in error for error in form.non_field_errors()
        )
        return response


class MFAAdminConfig(AdminConfig):
    default_site = "config.admin.MFAAdminSite"

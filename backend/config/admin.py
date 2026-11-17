"""Admin site wiring for SPEC-17-05 [R-17.9]: staff MFA at the admin door.

``MFAAdminConfig`` replaces ``django.contrib.admin`` in INSTALLED_APPS and
swaps the (lazily built) default site for ``MFAAdminSite``, whose login
form demands a valid TOTP code from the MFA population
(:func:`common.permissions.requires_mfa`). Registration, URLs and the
``admin:`` namespace are untouched — every ``@admin.register`` and
``admin.site._registry`` pin in the suite keeps working because this
subclass IS the default site now.

SPEC-20-13 adds the enrollment REACH the same page was missing: mandatory
MFA refuses an unenrolled privileged login, so the login template has to
be able to send that account somewhere to enroll. The two hooks below only
put the configured enrollment URL (and whether MFA is the reason the last
attempt failed) into the template context — the enforcement half of this
feature is untouched.
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

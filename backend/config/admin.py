"""Admin site wiring for SPEC-17-05 [R-17.9]: staff MFA at the admin door.

``MFAAdminConfig`` replaces ``django.contrib.admin`` in INSTALLED_APPS and
swaps the (lazily built) default site for ``MFAAdminSite``, whose login
form demands a valid TOTP code from privileged roles
(:func:`common.permissions.is_privileged`). Registration, URLs and the
``admin:`` namespace are untouched — every ``@admin.register`` and
``admin.site._registry`` pin in the suite keeps working because this
subclass IS the default site now.
"""
from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig


class MFAAdminSite(AdminSite):
    @property
    def login_form(self):
        # Lazy import, deliberately not an attribute set in __init__: this
        # module loads as an AppConfig before the app registry is ready,
        # and accounts.admin imports models. The site instance itself is
        # built lazily post-setup, but the first access to admin.site is
        # accounts.admin's own `admin.site.unregister(User)` — resolving
        # the form only when the login view actually needs it is the one
        # ordering that can never re-enter a half-initialized module.
        from accounts.admin import MFAAdminAuthenticationForm

        return MFAAdminAuthenticationForm


class MFAAdminConfig(AdminConfig):
    default_site = "config.admin.MFAAdminSite"

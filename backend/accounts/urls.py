from django.urls import path

from .views import (
    register, username_available, verify_email, resend_verification,
    forgot_username, request_password_reset, reset_password, LoginView,
    LogoutView, RefreshView, StorefrontLoginView,
    MFAStatusView, MFASetupView, MFAConfirmView, MFADisableView, MFATrustDeviceView,
)


urlpatterns = [

    path('verify-email/', verify_email, name='verify-email'),
    path('resend-verification/', resend_verification, name='resend-verification'),
    path('forgot-username/', forgot_username, name='forgot-username'),
    path('password-reset/', request_password_reset, name='password-reset'),
    path('password-reset/confirm/', reset_password, name='password-reset-confirm'),

    path(
        'username-available/',
        username_available,
        name='username-available'
    ),

    path(
        'register/',
        register,
        name='register'
    ),

    path(
        'login/',
        LoginView.as_view(),
        name='login'
    ),

    # SPEC-20-10: the customer door. `login/` above is the STAFF door (it
    # carries the R-17.9 TOTP requirement), so customers get a surface with
    # no totp field at all; a privileged account is refused here and sent to
    # the staff door rather than authenticated without the factor.
    path(
        'storefront/login/',
        StorefrontLoginView.as_view(),
        name='storefront-login'
    ),

    path(
        'token/refresh/',
        RefreshView.as_view(),
        name='token-refresh'
    ),

    path(
        'logout/',
        LogoutView.as_view(),
        name='logout'
    ),

    # SPEC-17-05 [R-17.9]: TOTP enrollment for privileged roles. Mounted
    # under both /api/accounts/ and /api/v1/account/ via this shared
    # urlconf; the enrollment path named in the login-block error is the
    # /api/accounts/ form.
    path(
        'mfa/status/',
        MFAStatusView.as_view(),
        name='mfa-status'
    ),

    path(
        'mfa/setup/',
        MFASetupView.as_view(),
        name='mfa-setup'
    ),

    path(
        'mfa/confirm/',
        MFAConfirmView.as_view(),
        name='mfa-confirm'
    ),

    path(
        'mfa/disable/',
        MFADisableView.as_view(),
        name='mfa-disable'
    ),

    # SPEC-20-8: the opt-in "trust this device for MFA_TRUST_DAYS days",
    # beside the enrollment surface it belongs to.
    path(
        'mfa/trust/',
        MFATrustDeviceView.as_view(),
        name='mfa-trust'
    ),

]

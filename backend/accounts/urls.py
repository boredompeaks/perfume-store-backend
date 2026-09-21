from django.urls import path

from .views import (
    register, username_available, verify_email, resend_verification,
    forgot_username, request_password_reset, reset_password, LoginView,
    LogoutView, RefreshView,
    MFAStatusView, MFASetupView, MFAConfirmView, MFADisableView,
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

]

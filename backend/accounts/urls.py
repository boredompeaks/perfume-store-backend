from django.urls import path

from .views import (
    AddressViewSet,
    LoginView,
    LogoutView,
    MFAConfirmView,
    MFADisableView,
    MFASetupView,
    MFAStatusView,
    MFATrustDeviceView,
    RefreshView,
    StorefrontLoginView,
    forgot_username,
    register,
    request_password_reset,
    resend_verification,
    reset_password,
    username_available,
    verify_email,
)

urlpatterns = [
    path("verify-email/", verify_email, name="verify-email"),
    path("resend-verification/", resend_verification, name="resend-verification"),
    path("forgot-username/", forgot_username, name="forgot-username"),
    path("password-reset/", request_password_reset, name="password-reset"),
    path("password-reset/confirm/", reset_password, name="password-reset-confirm"),
    path("username-available/", username_available, name="username-available"),
    path("register/", register, name="register"),
    path("login/", LoginView.as_view(), name="login"),
    # The customer door: `login/` is the STAFF door and carries the TOTP
    # requirement, so customers get a surface with no totp field at all.
    path("storefront/login/", StorefrontLoginView.as_view(), name="storefront-login"),
    path("token/refresh/", RefreshView.as_view(), name="token-refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    # TOTP enrollment for privileged roles. This urlconf is mounted at both
    # /api/accounts/ and /api/v1/account/; the login-block message names the former.
    path("mfa/status/", MFAStatusView.as_view(), name="mfa-status"),
    path("mfa/setup/", MFASetupView.as_view(), name="mfa-setup"),
    path("mfa/confirm/", MFAConfirmView.as_view(), name="mfa-confirm"),
    path("mfa/disable/", MFADisableView.as_view(), name="mfa-disable"),
    path("mfa/trust/", MFATrustDeviceView.as_view(), name="mfa-trust"),
    path(
        "addresses/",
        AddressViewSet.as_view({"get": "list", "post": "create"}),
        name="address-list",
    ),
    path(
        "addresses/<int:pk>/",
        AddressViewSet.as_view(
            {
                "get": "retrieve",
                "put": "update",
                "patch": "partial_update",
                "delete": "destroy",
            }
        ),
        name="address-detail",
    ),
]

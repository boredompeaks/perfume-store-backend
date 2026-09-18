from django.urls import path

from .views import (
    register, username_available, verify_email, resend_verification,
    forgot_username, request_password_reset, reset_password, LoginView,
)
from rest_framework_simplejwt.views import (
    TokenRefreshView,
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
        TokenRefreshView.as_view(),
        name='token-refresh'
    ),

]

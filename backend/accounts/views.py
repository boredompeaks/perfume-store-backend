from rest_framework.decorators import api_view, throttle_scope
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth.models import User
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode

from common import notifications
from common.models import AuditEvent
from .serializers import RegisterSerializer


class LoginView(TokenObtainPairView):
    """JWT login behind the 'auth' throttle scope.

    Throttling here bounds credential stuffing (V-04). The response contract
    is TokenObtainPairView's, unchanged. [R-7.20] successful (200) and
    rejected (400/401) attempts each land an audit event; 429 refusals are
    raised by throttling before this view runs, so they are the rate limit
    doing its job, not an authentication outcome, and write nothing."""

    throttle_scope = 'auth'

    def post(self, request, *args, **kwargs):
        try:
            response = super().post(request, *args, **kwargs)
        except (AuthenticationFailed, DRFValidationError):
            # super().post signals every rejection by raising; record the
            # failed attempt, then re-raise so the response is unchanged.
            self._record_login(request, succeeded=False)
            raise
        self._record_login(request, succeeded=True)
        return response

    @staticmethod
    def _record_login(request, succeeded):
        # request.data may be any parsed JSON (a list body has no .get);
        # the attempted username always rides in detail, while the actor FK
        # resolves only an exact username match — the same lookup
        # authenticate() just performed.
        payload = request.data if isinstance(request.data, dict) else {}
        username = str(payload.get("username", "") or "")
        AuditEvent.record(
            AuditEvent.EventType.AUTH_LOGIN
            if succeeded
            else AuditEvent.EventType.AUTH_LOGIN_FAILED,
            actor=User.objects.filter(username=username).first(),
            detail={"username": username},
        )


def _encoded_user_id(user):
    return urlsafe_base64_encode(force_bytes(user.pk))


def _get_user(uid):
    # Guard against None/empty uid: urlsafe_base64_decode(None) would raise
    # AttributeError (500) instead of the documented 400 (test-gaps #8).
    if not uid:
        return None
    try:
        return User.objects.get(pk=force_str(urlsafe_base64_decode(uid)))
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None


def _send_verification_email(user):
    uid = _encoded_user_id(user)
    token = default_token_generator.make_token(user)
    notifications.send_email(
        "verification_email",
        {
            "base_url": f"{settings.FRONTEND_URL}/verify-email",
            "uid": uid,
            "token": token,
        },
        "Verify your Perfume Store email",
        user.email,
    )


@api_view(['POST'])
@throttle_scope('auth')
def register(request):

    serializer = RegisterSerializer(
        data=request.data
    )

    if serializer.is_valid():

        # [R-7.20] The account creation and its trail row commit together:
        # no user without its registration event, no event without a user.
        with transaction.atomic():
            user = serializer.save()
            AuditEvent.record(
                AuditEvent.EventType.AUTH_REGISTERED,
                actor=user,
                detail={"username": user.username},
            )

        try:
            _send_verification_email(user)
        except Exception:
            return Response(
                {'error': 'Account created, but verification email could not be sent. Check SMTP settings.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "message": "User registered. Check your email to verify the account.",
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                }
            },
            status=status.HTTP_201_CREATED
        )

    return Response(
        serializer.errors,
        status=status.HTTP_400_BAD_REQUEST
    )


@api_view(['GET'])
@throttle_scope('auth')
def username_available(request):
    """The scope also throttles GET (ScopedRateThrottle has no safe-method
    exemption): this endpoint is a public username-existence oracle, so
    without a rate limit it enables cheap username enumeration."""
    username = request.query_params.get('username', '').strip()

    if len(username) < 3:
        return Response({
            'available': False,
            'message': 'Username must be at least 3 characters.'
        })

    exists = User.objects.filter(username__iexact=username).exists()
    return Response({
        'available': not exists,
        'message': 'Username is available.' if not exists else 'This username is already taken.'
    })


# Token-carrying account mutations (no mail): the 'auth' budget already
# bounds identity flows (register/login/refresh), and verify/confirm are
# bounded further by the entropy of their one-time tokens.
@api_view(['POST'])
@throttle_scope('auth')
def verify_email(request):
    user = _get_user(request.data.get('uid'))
    token = request.data.get('token', '')
    if user is None or not default_token_generator.check_token(user, token):
        return Response({'error': 'This verification link is invalid or expired.'}, status=status.HTTP_400_BAD_REQUEST)

    if not user.is_active:
        # [R-7.20] The activation and its trail row commit together; a
        # re-verify of an already-active account is a no-op and writes
        # nothing.
        with transaction.atomic():
            user.is_active = True
            user.save(update_fields=['is_active'])
            AuditEvent.record(
                AuditEvent.EventType.AUTH_EMAIL_VERIFIED,
                actor=user,
                detail={"username": user.username},
            )
    return Response({'message': 'Email verified. You can now log in.'})


# The three email-sending recovery endpoints get their own 'recovery'
# scope, tighter than 'auth' (see THROTTLE_RECOVERY_RATE): every accepted
# request triggers an outbound email, so the budget *is* the mail-bomb
# bound. The uniform 200 bodies below are untouched by throttling — only
# the extra 429 refusal is added.
@api_view(['POST'])
@throttle_scope('recovery')
def resend_verification(request):
    email = request.data.get('email', '').strip()
    user = User.objects.filter(email__iexact=email, is_active=False).first()
    if user:
        try:
            _send_verification_email(user)
        except Exception:
            return Response({'error': 'Verification email could not be sent. Check SMTP settings.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({'message': 'If an unverified account exists, a verification email has been sent.'})


@api_view(['POST'])
@throttle_scope('recovery')
def forgot_username(request):
    email = request.data.get('email', '').strip()
    user = User.objects.filter(email__iexact=email).first()
    if user:
        try:
            notifications.send_email(
                "username_reminder",
                {"username": user.username},
                "Your Perfume Store username",
                user.email,
            )
        except Exception:
            return Response({'error': 'Username email could not be sent. Check SMTP settings.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({'message': 'If an account exists for this email, the username has been sent.'})


@api_view(['POST'])
@throttle_scope('recovery')
def request_password_reset(request):
    email = request.data.get('email', '').strip()
    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user:
        uid = _encoded_user_id(user)
        token = default_token_generator.make_token(user)
        try:
            notifications.send_email(
                "password_reset",
                {
                    "base_url": f"{settings.FRONTEND_URL}/reset-password",
                    "uid": uid,
                    "token": token,
                },
                "Reset your Perfume Store password",
                user.email,
            )
        except Exception:
            return Response({'error': 'Password-reset email could not be sent. Check SMTP settings.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({'message': 'If an active account exists for this email, a password-reset link has been sent.'})


@api_view(['POST'])
@throttle_scope('auth')
def reset_password(request):
    user = _get_user(request.data.get('uid'))
    token = request.data.get('token', '')
    password = request.data.get('password', '')
    if user is None or not default_token_generator.check_token(user, token):
        return Response({'error': 'This password-reset link is invalid or expired.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        validate_password(password, user)
    except ValidationError as error:
        return Response({'password': list(error.messages)}, status=status.HTTP_400_BAD_REQUEST)
    # [R-7.20] The credential change and its trail row commit together. The
    # one-time token is deliberately not stored in detail: it is single-use
    # evidence, not audit data.
    with transaction.atomic():
        user.set_password(password)
        user.save(update_fields=['password'])
        AuditEvent.record(
            AuditEvent.EventType.AUTH_PASSWORD_RESET,
            actor=user,
            detail={"username": user.username},
        )
    return Response({'message': 'Password reset successfully. You can now log in.'})

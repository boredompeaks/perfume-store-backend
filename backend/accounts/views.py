from rest_framework.decorators import api_view, throttle_scope
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth.models import User
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.password_validation import validate_password
from django.core.mail import send_mail
from django.core.exceptions import ValidationError
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode

from .serializers import RegisterSerializer


class LoginView(TokenObtainPairView):
    """JWT login behind the 'auth' throttle scope.

    Throttling here bounds credential stuffing (V-04). The response contract
    is TokenObtainPairView's, unchanged."""

    throttle_scope = 'auth'


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


def _send_email(subject, message, recipient):
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [recipient], fail_silently=False)


def _send_verification_email(user):
    uid = _encoded_user_id(user)
    token = default_token_generator.make_token(user)
    url = f'{settings.FRONTEND_URL}/verify-email?uid={uid}&token={token}'
    _send_email(
        'Verify your Perfume Store email',
        f'Welcome! Verify your email by opening this link:\n\n{url}\n\nIf you did not create this account, ignore this email.',
        user.email,
    )


@api_view(['POST'])
@throttle_scope('auth')
def register(request):

    serializer = RegisterSerializer(
        data=request.data
    )

    if serializer.is_valid():

        user = serializer.save()

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
def username_available(request):
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


@api_view(['POST'])
def verify_email(request):
    user = _get_user(request.data.get('uid'))
    token = request.data.get('token', '')
    if user is None or not default_token_generator.check_token(user, token):
        return Response({'error': 'This verification link is invalid or expired.'}, status=status.HTTP_400_BAD_REQUEST)

    if not user.is_active:
        user.is_active = True
        user.save(update_fields=['is_active'])
    return Response({'message': 'Email verified. You can now log in.'})


@api_view(['POST'])
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
def forgot_username(request):
    email = request.data.get('email', '').strip()
    user = User.objects.filter(email__iexact=email).first()
    if user:
        try:
            _send_email('Your Perfume Store username', f'Your username is: {user.username}', user.email)
        except Exception:
            return Response({'error': 'Username email could not be sent. Check SMTP settings.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({'message': 'If an account exists for this email, the username has been sent.'})


@api_view(['POST'])
def request_password_reset(request):
    email = request.data.get('email', '').strip()
    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user:
        uid = _encoded_user_id(user)
        token = default_token_generator.make_token(user)
        url = f'{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}'
        try:
            _send_email(
                'Reset your Perfume Store password',
                f'Use this one-time link to choose a new password:\n\n{url}\n\nIf you did not request this, ignore this email.',
                user.email,
            )
        except Exception:
            return Response({'error': 'Password-reset email could not be sent. Check SMTP settings.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({'message': 'If an active account exists for this email, a password-reset link has been sent.'})


@api_view(['POST'])
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
    user.set_password(password)
    user.save(update_fields=['password'])
    return Response({'message': 'Password reset successfully. You can now log in.'})

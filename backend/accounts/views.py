from rest_framework.decorators import api_view, throttle_scope
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode

from common import notifications, totp
from common.models import AuditEvent
from common.permissions import IsPrivilegedRole, is_privileged
from ops import alerts
from .models import (
    MFA_CODE_INVALID,
    TOTPDevice,
)
from .serializers import MFATokenObtainPairSerializer, RegisterSerializer


class LoginView(TokenObtainPairView):
    """JWT login behind the 'auth' throttle scope.

    Throttling here bounds credential stuffing (V-04). The response contract
    is TokenObtainPairView's, except that the refresh token never enters the
    JSON body [R-17.12]: it is moved into an HttpOnly cookie, so browser JS
    cannot read it (spec §17.1). [R-7.20] successful (200) and rejected
    (400/401) attempts each land an audit event; 429 refusals are raised by
    throttling before this view runs, so they are the rate limit doing its
    job, not an authentication outcome, and write nothing.

    SPEC-17-05 [R-17.9]: the serializer is the MFA-aware subclass —
    privileged-role users must present a valid TOTP code (or enroll first),
    and those rejections raise here too, so they are audited as failed
    logins without any code path that mints tokens skipping the factor.
    """

    throttle_scope = 'auth'
    serializer_class = MFATokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        try:
            response = super().post(request, *args, **kwargs)
        except (AuthenticationFailed, DRFValidationError):
            # super().post signals every rejection by raising; record the
            # failed attempt, then re-raise so the response is unchanged.
            self._record_login(request, succeeded=False)
            raise
        self._record_login(request, succeeded=True)
        return _set_refresh_cookie(response)

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


def _set_refresh_cookie(response):
    """Move the freshly minted refresh token out of the response body and
    into the HttpOnly cookie [R-17.12]. A refresh token delivered as JSON is
    readable by any injected script — the cookie is the only carrier the
    browser client gets. The cookie's max-age is synced to the token
    lifetime so the browser never presents a cookie older than the token it
    holds."""
    token = response.data.get('refresh')
    if token:
        response.set_cookie(
            settings.JWT_REFRESH_COOKIE_NAME,
            token,
            max_age=int(
                settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds()
            ),
            httponly=True,
            secure=not settings.DEBUG,
            samesite=settings.JWT_REFRESH_COOKIE_SAMESITE,
            path=settings.JWT_REFRESH_COOKIE_PATH,
        )
        del response.data['refresh']
    return response


def _clear_refresh_cookie(response):
    """Expire the refresh cookie in place. Name + Path must match the
    cookie set by _set_refresh_cookie or the browser keeps the stale one."""
    response.set_cookie(
        settings.JWT_REFRESH_COOKIE_NAME,
        '',
        max_age=0,
        expires=0,
        httponly=True,
        secure=not settings.DEBUG,
        samesite=settings.JWT_REFRESH_COOKIE_SAMESITE,
        path=settings.JWT_REFRESH_COOKIE_PATH,
    )
    return response


class RefreshView(TokenRefreshView):
    """JWT refresh behind the 'auth' throttle scope.

    Throttling here bounds refresh-token brute forcing (V-04 family). The
    refresh token is read from the HttpOnly cookie set at login [R-17.12];
    an explicit body token still works for non-browser API clients. On
    success the rotated token re-enters the cookie (never the body), and a
    rejected refresh sweeps the cookie so a dead token cannot pin the
    browser into retrying it."""

    throttle_scope = 'auth'

    def post(self, request, *args, **kwargs):
        body = request.data if isinstance(request.data, dict) else None
        if body is not None and not body.get('refresh'):
            cookie = request.COOKIES.get(settings.JWT_REFRESH_COOKIE_NAME)
            # JSON bodies parse to a plain mutable dict, so the cookie token
            # merges in place — DRF 3.18 exposes no data setter. Form-encoded
            # bodies (immutable QueryDict) are not a browser-client surface
            # and stay untouched.
            if cookie and type(body) is dict:
                body['refresh'] = cookie
        return _set_refresh_cookie(super().post(request, *args, **kwargs))

    def handle_exception(self, exc):
        # super().post signals rejection by raising (InvalidToken -> 401
        # here), so a rejected refresh can only sweep its cookie in the
        # exception handler. 429 throttle refusals are NOT a verdict on the
        # token — the cookie must survive them (parallel tabs share one
        # budget) — so only an authentication verdict sweeps.
        response = super().handle_exception(exc)
        if response.status_code == 401:
            response = _clear_refresh_cookie(response)
        return response


class LogoutView(APIView):
    """Secure logout [R-17.8]: blacklist the presented refresh token.

    POST /api/accounts/logout/ with a valid access token blacklists the
    refresh token and clears its HttpOnly cookie, so the browser session
    cannot outlive the logout — a stolen refresh token can no longer mint
    access tokens. The cookie is the token source for browser clients
    [R-17.12]; an explicit body token still works for non-browser API
    clients. Authentication is required (an anonymous caller has nothing to
    revoke and must not learn anything about token validity); the 'auth'
    throttle scope covers the endpoint like its sibling token routes.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = 'auth'

    def post(self, request):
        raw = request.COOKIES.get(settings.JWT_REFRESH_COOKIE_NAME)
        if not raw and isinstance(request.data, dict):
            raw = request.data.get('refresh')
        if not isinstance(raw, str) or not raw:
            # No cookie and no body token: nothing was presented, and no
            # cookie can exist to sweep (raw would have come from it).
            raise DRFValidationError({'refresh': ['This field is required.']})
        try:
            # RefreshToken() verifies signature and expiry: an invalid or
            # already-expired token is rejected rather than recorded.
            token = RefreshToken(raw)
        except TokenError:
            # A dead cookie must not pin the browser: sweep it even while
            # rejecting, or every future logout retries the same dead token.
            return _clear_refresh_cookie(
                Response(
                    {'refresh': ['Invalid or expired token.']},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            )
        token.blacklist()
        return _clear_refresh_cookie(Response({'message': 'Logged out.'}))


def _blacklist_user_refresh_tokens(user):
    """Session invalidation after critical account changes [R-17.10].

    Blacklists every refresh token still outstanding for ``user`` so a
    pre-change session (stolen device, forgotten tab, attacker) cannot
    mint new access tokens after the account's credentials changed. The
    caller runs this inside the same transaction as the credential
    change, so the sessions die exactly when the change lands. Idempotent
    via get_or_create: re-blacklisting an already-blacklisted token is a
    no-op, so concurrent changes cannot collide.
    """
    for outstanding in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=outstanding)


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
        # [R-17.10] the password change is a critical account change: every
        # outstanding session (refresh token) dies with it, in the same
        # transaction — no window where old sessions outlive new
        # credentials.
        _blacklist_user_refresh_tokens(user)
        AuditEvent.record(
            AuditEvent.EventType.AUTH_PASSWORD_RESET,
            actor=user,
            detail={"username": user.username},
        )
        # [SPEC-19-2] Security-sensitive account change ([R-19.27]
        # alerting half) beside the audit hook, in the same atomic block.
        # The identity line carries the username only — the same [R-17.32]
        # allowlist the audit trail's log mirror already governs. Log-only
        # on failure: an alert outage must not fail a completed password
        # reset. The alert does not name the recipient inbox (the customer
        # reset mail already went out) — it tells the staff the event
        # happened, catching a mailbox compromise.
        alerts.notify_security_change(
            f"Password reset completed for user {user.username} (id {user.pk})."
        )
    return Response({'message': 'Password reset successfully. You can now log in.'})


# --- SPEC-17-05 [R-17.9]: TOTP enrollment for privileged roles ------------
# All four endpoints are IsPrivilegedRole-gated: exactly the population
# enforcement blocks at login may enroll/status/disable. The 'auth' scope
# bounds brute-forcing the 6-digit code space like every other identity
# flow. Secrets leave the server exactly once (setup response) and never
# again; nothing below returns or logs one.


def _body_value(request, key):
    # request.data may be any parsed JSON (a list body has no .get) — the
    # same defensive shape LoginView._record_login already uses.
    payload = request.data if isinstance(request.data, dict) else {}
    return payload.get(key)


def _authorize_enrollment(request):
    """Shared setup/confirm gate: privileged JWT caller, or the credential
    bootstrap while no device is active (see MFASetupView). Returns the
    target user, or an error response."""
    user = request.user
    if not user.is_authenticated:
        payload = request.data if isinstance(request.data, dict) else {}
        user = authenticate(
            request=request,
            username=str(payload.get("username", "") or ""),
            password=str(payload.get("password", "") or ""),
        )
        if user is None:
            # Uniform rejection: no signal whether username, password or
            # both were wrong.
            return None, Response(
                {"error": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if TOTPDevice.active_for(user) is not None:
            # The bootstrap window is over once a device is active:
            # credentials alone must never reach an enrolled account.
            return None, Response(
                {
                    "error": "A device is already enrolled. Sign in and use "
                    "the authenticated setup path."
                },
                status=status.HTTP_403_FORBIDDEN,
            )
    if not is_privileged(user):
        return None, Response(
            {"error": IsPrivilegedRole.message},
            status=status.HTTP_403_FORBIDDEN,
        )
    return user, None


def _consume_code(device, code):
    """Verify `code` against `device` and persist the replay watermark.

    Returns the matched counter, or None when the code is wrong/expired/
    replayed — in which case nothing is written.
    """
    counter = totp.verify_code(
        device.secret,
        code,
        at_time=totp.now(),
        last_used_counter=device.last_used_counter,
    )
    if counter is not None:
        device.last_used_counter = counter
        device.save(update_fields=["last_used_counter"])
    return counter


class MFAStatusView(APIView):
    """GET /api/accounts/mfa/status/ — {enabled} only, never the secret."""

    permission_classes = [IsPrivilegedRole]
    # The accounts URLConf wiring guard requires a scope on every route;
    # 'auth' matches the sibling MFA endpoints (this one is read-only, but
    # the budget also bounds enabled-state probing).
    throttle_scope = 'auth'

    def get(self, request):
        return Response({"enabled": TOTPDevice.active_for(request.user) is not None})


class MFASetupView(APIView):
    """POST /api/accounts/mfa/setup/ — mint a fresh secret, shown once.

    Returns the base32 secret and the otpauth:// URI for the user's
    authenticator app (a URI string, not a QR image — R-17.9 demands the
    factor, not an artefact). Two deliberate trust paths:

    - JWT session (privileged): steady-state path. Re-enrolling while a
      device is enabled must also re-prove the second factor with ``code``
      — a session thief who could re-enroll freely would own the new
      secret.
    - Credential bootstrap (anonymous): the rollout path that keeps
      blocked-at-login enforcement reachable. A privileged account with no
      active device can never obtain a JWT (login demands the factor it
      does not have yet), so setup additionally accepts username+password
      — exactly the first-factor-enrollment window every mainstream
      authenticator flow grants a password-proven identity. Once a device
      is active this path closes: credentials alone never touch an
      enrolled account (that is the whole point of the factor), and the
      'auth' throttle bounds the credential attempts.

    The fresh row starts unconfirmed and disabled; nothing changes until a
    valid confirm.
    """

    permission_classes = []
    throttle_scope = 'auth'

    def post(self, request):
        user, error = _authorize_enrollment(request)
        if error is not None:
            return error
        current = TOTPDevice.objects.filter(user=user).first()
        if current is not None and current.enabled:
            if _consume_code(current, _body_value(request, "code")) is None:
                return Response(
                    {"code": [MFA_CODE_INVALID]}, status=status.HTTP_400_BAD_REQUEST
                )
        with transaction.atomic():
            # get_or_create (IntegrityError-retrying) then overwrite: the
            # row is keyed by the OneToOne user, and setup always mints a
            # fresh secret — never reuses a stale one.
            device, _ = TOTPDevice.objects.get_or_create(user=user)
            device.secret = totp.generate_secret()
            device.enabled = False
            device.confirmed_at = None
            device.last_used_counter = None
            device.save(
                update_fields=[
                    "secret",
                    "enabled",
                    "confirmed_at",
                    "last_used_counter",
                ]
            )
        return Response(
            {
                "secret": device.secret,
                "otpauth_uri": totp.otpauth_uri(device.secret, user.username),
            }
        )


class MFAConfirmView(APIView):
    """POST /api/accounts/mfa/confirm/ {code} — verify + enable the device.

    Shares the setup view's dual trust path: the bootstrapped user still
    holds no JWT (their device is unconfirmed, so login keeps refusing),
    so confirm accepts the same credential proof while no device is
    active. Only a pending (unconfirmed, disabled) device can be
    confirmed, so a valid code flips enabled for exactly the secret the
    user just enrolled and never for a still-active previous one.
    """

    permission_classes = []
    throttle_scope = 'auth'

    def post(self, request):
        user, error = _authorize_enrollment(request)
        if error is not None:
            return error
        device = TOTPDevice.objects.filter(
            user=user, enabled=False, confirmed_at__isnull=True
        ).first()
        if device is None:
            return Response(
                {"code": ["No pending MFA enrollment. Request a setup first."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        counter = totp.verify_code(
            device.secret,
            _body_value(request, "code"),
            at_time=totp.now(),
            last_used_counter=device.last_used_counter,
        )
        if counter is None:
            return Response(
                {"code": [MFA_CODE_INVALID]}, status=status.HTTP_400_BAD_REQUEST
            )
        with transaction.atomic():
            device.confirmed_at = timezone.now()
            device.enabled = True
            device.last_used_counter = counter
            device.save(
                update_fields=["confirmed_at", "enabled", "last_used_counter"]
            )
        return Response({"enabled": True})


class MFADisableView(APIView):
    """POST /api/accounts/mfa/disable/ {code} — turn MFA off.

    Spec text is silent on the disable guard; the second factor is chosen
    and declared: disabling requires a valid code from the enabled device,
    so a stolen session or password alone cannot strip the factor (the
    lost-device recovery path is an ops concern R-17.9 does not name).
    The row survives (enrollment history) but the secret is dead for
    verification; re-enrollment mints a fresh one.
    """

    permission_classes = [IsPrivilegedRole]
    throttle_scope = 'auth'

    def post(self, request):
        device = TOTPDevice.objects.filter(
            user=request.user, enabled=True, confirmed_at__isnull=False
        ).first()
        if device is None:
            return Response(
                {"code": ["Multi-factor authentication is not enabled."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if _consume_code(device, _body_value(request, "code")) is None:
            return Response(
                {"code": [MFA_CODE_INVALID]}, status=status.HTTP_400_BAD_REQUEST
            )
        with transaction.atomic():
            device.enabled = False
            device.save(update_fields=["enabled"])
        return Response({"enabled": False})

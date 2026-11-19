import hashlib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.signing import BadSignature, TimestampSigner
from django.db import transaction
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import status, viewsets
from rest_framework.decorators import (
    api_view,
    permission_classes,
    throttle_classes,
    throttle_scope,
)
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings as jwt_api_settings
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from cart.services import merge_session_cart_into_user_cart
from common import notifications, qr, totp
from common.models import AuditEvent
from common.permissions import requires_mfa
from ops import alerts

from . import mfa_trust
from .models import (
    MFA_CODE_INVALID,
    Address,
    TOTPDevice,
)
from .serializers import (
    AddressSerializer,
    MFATokenObtainPairSerializer,
    RegisterSerializer,
    StorefrontTokenObtainPairSerializer,
)
from .services import (
    AccountAlreadyErased,
    UnsafeErasureReason,
    erase_account,
)


def _lock_user_addresses(user):
    """Lock one owner's rows before a default-flag write.

    The rows alone cannot serialize the first default (there is nothing to lock
    yet), so the owner row is locked first; every writer takes locks in this same
    order, making the flip race- and deadlock-free. Caller must be in a transaction.
    """
    User.objects.select_for_update().get(pk=user.pk)
    return Address.objects.select_for_update().filter(user=user, is_default=True)


class AddressViewSet(viewsets.ModelViewSet):
    """CRUD for the caller's saved addresses.

    The queryset is the authorization boundary: filtering by ``request.user``
    turns a foreign id into the repo's uniform 404 rather than a 403, so the
    endpoint reveals nothing about addresses the caller does not own.
    """

    serializer_class = AddressSerializer
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth"

    def get_queryset(self):
        return Address.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        if serializer.validated_data.get("is_default"):
            with transaction.atomic():
                _lock_user_addresses(self.request.user).update(is_default=False)
                serializer.save(user=self.request.user)
        else:
            serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        if serializer.validated_data.get("is_default"):
            with transaction.atomic():
                _lock_user_addresses(self.request.user).exclude(
                    pk=serializer.instance.pk
                ).update(is_default=False)
                serializer.save()
        else:
            serializer.save()


# Self-service erasure and export (AUD-005). task: T-20261119-013
#
# Both routes read their subject from the authenticated request only; a body
# or query value can name no other account, so a scoping mistake cannot become
# a mass-erasure primitive. There is deliberately no bulk or staff variant.

_ERASURE_REFUSED = "Account erasure refused."


def _erasure_reason(request):
    # The service validates the reason against the identity being removed.
    # A non-object body or non-string reason means "no reason", never a 500
    # from the validator's string operations.
    payload = request.data if isinstance(request.data, dict) else {}
    reason = payload.get("reason", "")
    return reason if isinstance(reason, str) else ""


class AccountEraseView(APIView):
    """POST /api/accounts/account/erase/ — tombstone the CALLER in place.

    ``IsAuthenticated`` plus ``request.user`` is the whole authorization
    boundary: ``user``/``id`` fields in the body are ignored, and no query
    parameter can redirect the operation. ``auth``-throttled at the project
    default of 10/min (``THROTTLE_AUTH_RATE``), like the other account routes.

    Responses: 200 when erased; 409 when the caller is already erased; 400 when
    the service refuses the reason as identity-bearing. Both refusals carry the
    same constant, non-identifying body.

    The device row is deleted inside the same transaction as the service call:
    a TOTP secret is a credential and must not outlive the erased identity. The
    service is shared with staff tooling and is intentionally unchanged, so the
    request surface owns this extra deletion.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "auth"

    def post(self, request):
        try:
            with transaction.atomic():
                erase_account(
                    request.user,
                    actor=request.user,
                    reason=_erasure_reason(request),
                )
                TOTPDevice.objects.filter(user_id=request.user.pk).delete()
        except AccountAlreadyErased:
            return Response(
                {"error": _ERASURE_REFUSED}, status=status.HTTP_409_CONFLICT
            )
        except UnsafeErasureReason:
            return Response(
                {"error": _ERASURE_REFUSED}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response({"message": "Account erased."})


def _export_order(order):
    # Only the customer-facing reference and the caller's own rows: no Order
    # pk, no gateway references, no staff actor/decider ids. Money stays a
    # decimal string instead of a binary float.
    return {
        "order_number": order.order_number,
        "status": order.status,
        "payment_status": order.payment_status,
        "fulfilment_status": order.fulfilment_status,
        "full_name": order.full_name,
        "phone": order.phone,
        "address": order.address,
        "city": order.city,
        "state": order.state,
        "pincode": order.pincode,
        "currency": order.currency,
        "discount_amount": str(order.discount_amount),
        "shipping_amount": str(order.shipping_amount),
        "total_amount": str(order.total_amount),
        "created_at": order.created_at.isoformat(),
        "items": [
            {
                "product_name": item.product_name,
                "sku": item.sku,
                "variant_name": item.variant_name,
                "price": str(item.price),
                "quantity": item.quantity,
                "subtotal": str(item.subtotal),
                "currency": item.currency,
            }
            for item in order.items.all()
        ],
        "refunds": [
            {
                "amount": str(refund.amount),
                "kind": refund.kind,
                "status": refund.status,
                "created_at": refund.created_at.isoformat(),
            }
            for refund in order.refunds.all()
        ],
        "returns": [
            {
                "status": return_request.status,
                "reason_code": return_request.reason_code,
                "created_at": return_request.created_at.isoformat(),
            }
            for return_request in order.return_requests.all()
        ],
    }


class AccountExportView(APIView):
    """GET /api/accounts/account/export/ — the CALLER's own data only.

    ``request.user`` scopes every query (profile, addresses, orders); no path
    or query value can select another account's rows. The projection carries
    no password hash, TOTP secret, token, staff flag or group membership, and
    no other user's rows. ``auth``-throttled at 10/min like the rest of the
    account API. Orders are read through the reverse relation, so this module
    gains no accounts-to-orders import edge.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "auth"

    def get(self, request):
        user = request.user
        orders = user.orders.prefetch_related(
            "items", "refunds", "return_requests"
        ).order_by("-created_at", "-id")
        return Response(
            {
                # After erasure these are the tombstone values; the old
                # identity is never re-read from anywhere.
                "profile": {
                    "username": user.username,
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "date_joined": user.date_joined.isoformat(),
                },
                "addresses": AddressSerializer(user.addresses.all(), many=True).data,
                "orders": [_export_order(order) for order in orders],
            }
        )


class LoginView(TokenObtainPairView):
    """JWT login behind the 'auth' throttle scope — the STAFF door.

    Uses the MFA-aware serializer, keeps the refresh token out of the JSON body
    and in an HttpOnly cookie, and audits both successful and rejected attempts;
    throttling bounds credential stuffing.
    """

    # Public: a login door cannot require the credential it issues.
    permission_classes = [AllowAny]
    throttle_scope = "auth"
    serializer_class = MFATokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        try:
            response = super().post(request, *args, **kwargs)
        except (AuthenticationFailed, DRFValidationError, PermissionDenied):
            # super().post signals every rejection by raising; record the failed
            # attempt, then re-raise so the response is unchanged.
            self._record_login(request, succeeded=False)
            raise
        self._record_login(request, succeeded=True)
        # The guest basket follows the account it just signed into; with none,
        # nothing is minted and no key is added.
        user = self._logged_in_user(request)
        if user is not None:
            cart = merge_session_cart_into_user_cart(request, user)
            if cart is not None:
                response.data["cart_id"] = cart.pk
        return _set_refresh_cookie(response)

    @staticmethod
    def _logged_in_user(request):
        # The serializer validated an exact username, so this is the row
        # authenticate() just proved.
        payload = request.data if isinstance(request.data, dict) else {}
        username = str(payload.get("username", "") or "")
        return User.objects.filter(username=username).first()

    @staticmethod
    def _record_login(request, succeeded):
        # request.data may be any parsed JSON (a list body has no .get); the
        # actor FK resolves only an exact username match.
        payload = request.data if isinstance(request.data, dict) else {}
        username = str(payload.get("username", "") or "")
        AuditEvent.record(
            AuditEvent.EventType.AUTH_LOGIN
            if succeeded
            else AuditEvent.EventType.AUTH_LOGIN_FAILED,
            actor=User.objects.filter(username=username).first(),
            detail={"username": username},
        )


class StorefrontLoginView(LoginView):
    """POST /api/accounts/storefront/login/ — the CUSTOMER door.

    Inherited wholesale from LoginView, so the throttle, audit trail and HttpOnly
    refresh cookie are the same code; only the serializer differs — no ``totp``
    field, and privileged accounts are refused rather than authenticated without
    the factor.
    """

    # Public: the customer door, the same credential-free surface as above.
    permission_classes = [AllowAny]
    serializer_class = StorefrontTokenObtainPairSerializer


def _set_refresh_cookie(response):
    """Move the freshly minted refresh token out of the response body and
    into the HttpOnly cookie. A token delivered as JSON is readable by injected
    script; the cookie's max-age is synced to the token lifetime."""
    token = response.data.get("refresh")
    if token:
        response.set_cookie(
            settings.JWT_REFRESH_COOKIE_NAME,
            token,
            max_age=int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()),
            httponly=True,
            secure=not settings.DEBUG,
            samesite=settings.JWT_REFRESH_COOKIE_SAMESITE,
            path=settings.JWT_REFRESH_COOKIE_PATH,
        )
        del response.data["refresh"]
    return response


def _clear_refresh_cookie(response):
    """Expire the refresh cookie in place. Name + Path must match
    _set_refresh_cookie or the browser keeps the stale one."""
    response.set_cookie(
        settings.JWT_REFRESH_COOKIE_NAME,
        "",
        max_age=0,
        expires=0,
        httponly=True,
        secure=not settings.DEBUG,
        samesite=settings.JWT_REFRESH_COOKIE_SAMESITE,
        path=settings.JWT_REFRESH_COOKIE_PATH,
    )
    return response


def _refresh_origin_is_trusted(request):
    """Origin gate for the refresh seam.

    The refresh credential rides a cookie, and SameSite is registered-domain
    scoped, so a hostile sibling subdomain would still attach it; Django's CSRF
    middleware cannot check DRF APIViews (csrf_exempt, no session auth), so the
    Origin header is checked explicitly. An absent Origin is not a cross-site
    claim — non-browser clients send none and hold no cookie jar.
    """
    origin = request.META.get("HTTP_ORIGIN")
    if origin is None:
        return True
    allowed = {entry.lower() for entry in settings.CSRF_TRUSTED_ORIGINS}
    allowed.update(entry.lower() for entry in settings.CORS_ALLOWED_ORIGINS)
    allowed.add(f"{request.scheme}://{request.get_host()}".lower())
    return origin.lower() in allowed


def _legacy_body_refresh_allowed():
    """Is the legacy body carrier still inside its dual-accept window?

    The window lets the frontend migrate at its own pace: closing it early logs
    out clients still holding a localStorage token, while leaving it open keeps
    an XSS-readable carrier alive. Env-driven and read per call, so a deployment
    or test can move the window without a restart.
    """
    if not settings.JWT_REFRESH_BODY_ACCEPT_ENABLED:
        return False
    deadline = settings.JWT_REFRESH_BODY_ACCEPT_START + timedelta(
        days=settings.JWT_REFRESH_BODY_ACCEPT_DAYS
    )
    return timezone.now().date() < deadline


def _resolve_refresh_carrier(request, body):
    """Decide which carrier supplies the refresh token; return the raw token
    the rotation will consume (None when neither carrier presented one).

    Window open: an explicit body token wins, the legacy behaviour. Window
    closed: a cookie refill takes precedence and a body-only presentation is
    refused by name, not with the generic "field is required" a legacy client
    could not interpret. A non-dict body is never merged with the cookie.
    """
    if body is None:
        return None
    cookie = request.COOKIES.get(settings.JWT_REFRESH_COOKIE_NAME)
    presented = body.get("refresh")
    if not presented:
        if cookie and type(body) is dict:
            body["refresh"] = cookie
            return cookie
        return None
    if _legacy_body_refresh_allowed():
        return presented
    if cookie and type(body) is dict:
        body["refresh"] = cookie
        return cookie
    raise DRFValidationError(
        {"refresh": ["Legacy body refresh tokens are no longer accepted."]}
    )


def _lock_presented_refresh(raw):
    """Serialize concurrent rotations of the SAME presented refresh token.

    simplejwt's check-then-blacklist is not atomic: two simultaneous POSTs
    carrying one token could both pass and mint replacements. The
    OutstandingToken row keyed by the token's jti is locked FOR UPDATE inside the
    rotation transaction, so the loser re-validates against the committed
    blacklist and dies 401. Only a token that verifies is locked (a forged jti
    must not buy a lock on a stranger's row); SQLite no-ops FOR UPDATE, so the
    real enforcement is PostgreSQL.
    """
    if not raw:
        return
    try:
        claims = RefreshToken(raw)
    except TokenError:
        return
    jti = claims.get(jwt_api_settings.JTI_CLAIM)
    if jti:
        OutstandingToken.objects.select_for_update().filter(jti=jti).first()


class RefreshView(TokenRefreshView):
    """JWT refresh behind the 'auth' throttle scope.

    The refresh token is read from the HttpOnly cookie (a body token still works
    while the dual-accept window is open), rotation runs in one transaction under
    the presented token's row lock, and a rejected refresh sweeps the cookie so a
    dead token cannot pin the browser into retrying it.
    """

    # Public: a caller whose access token expired has no credential to present.
    permission_classes = [AllowAny]
    throttle_scope = "auth"

    def post(self, request, *args, **kwargs):
        if not _refresh_origin_is_trusted(request):
            # 403 before the body is read: a cross-origin probe must never sweep
            # or mint from the victim's cookie.
            raise PermissionDenied("Cross-origin refresh requests are not allowed.")
        raw = _resolve_refresh_carrier(
            request, request.data if isinstance(request.data, dict) else None
        )
        with transaction.atomic():
            _lock_presented_refresh(raw)
            response = super().post(request, *args, **kwargs)
        return _set_refresh_cookie(response)

    def handle_exception(self, exc):
        # super().post signals rejection by raising, so the sweep can only happen
        # here. 429 throttle refusals are not a verdict on the token (parallel tabs
        # share one budget), so only an authentication verdict sweeps.
        response = super().handle_exception(exc)
        if response.status_code == 401:
            response = _clear_refresh_cookie(response)
        return response


class LogoutView(APIView):
    """Secure logout: blacklist the presented refresh token.

    POST /api/accounts/logout/ with a valid access token blacklists the refresh
    token and clears its HttpOnly cookie, so a stolen token can no longer mint
    access tokens. The cookie is the browser source; an explicit body token works
    for non-browser clients. Authentication is required and 'auth'-throttled.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "auth"

    def post(self, request):
        raw = request.COOKIES.get(settings.JWT_REFRESH_COOKIE_NAME)
        if not raw and isinstance(request.data, dict):
            raw = request.data.get("refresh")
        if not isinstance(raw, str) or not raw:
            # Nothing presented; no cookie can exist to sweep either.
            raise DRFValidationError({"refresh": ["This field is required."]})
        try:
            # RefreshToken() verifies signature and expiry: an invalid or expired
            # token is rejected rather than recorded.
            token = RefreshToken(raw)
        except TokenError:
            # A dead cookie must not pin the browser: sweep it even while rejecting,
            # or every future logout retries the same dead token.
            return _clear_refresh_cookie(
                Response(
                    {"refresh": ["Invalid or expired token."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            )
        token.blacklist()
        return _clear_refresh_cookie(Response({"message": "Logged out."}))


def _blacklist_user_refresh_tokens(user):
    """Session invalidation after critical account changes.

    Blacklists every refresh token still outstanding for ``user`` so a pre-change
    session cannot mint new access tokens after the credentials changed; the
    caller runs it in the same transaction, and ``get_or_create`` makes it
    idempotent under concurrent changes.
    """
    for outstanding in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=outstanding)


def _encoded_user_id(user):
    return urlsafe_base64_encode(force_bytes(user.pk))


def _get_user(uid):
    # Guard against None/empty uid: urlsafe_base64_decode(None) would raise
    # AttributeError (500) instead of the documented 400.
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


# Domain separation and payload for the signed access challenge. Literals on
# purpose, not settings: the owner-side edge gate must reproduce them
# character-for-character; only the ACCOUNT_CHALLENGE_* settings vary per deployment.
CHALLENGE_SALT = "accounts.access-challenge"
CHALLENGE_VALUE = "accounts-access"
CHALLENGE_REFUSAL = "Access challenge required."
# A valid challenge earns the truthful boolean; the message stays constant so
# the two outcomes differ in that value only, never in shape or length.
_PROBE_MESSAGE = "Username availability checked."
_PROBE_REPLAY_PREFIX = "accounts.username-probe.challenge"


def _enforce_access_challenge(request):
    """Optional signed-challenge hook for the access gate.

    With ``ACCOUNT_CHALLENGE_SECRET`` unset the hook is disabled; otherwise the
    request must carry a ``TimestampSigner`` token minted with that secret and the
    ``CHALLENGE_SALT``/``CHALLENGE_VALUE`` domain, refused identically when
    missing, foreign, tampered or stale. It runs after DRF throttling, so token
    guessing is bounded by the endpoint's budget too.
    """
    secret = settings.ACCOUNT_CHALLENGE_SECRET
    if not secret:
        return
    token = request.query_params.get("challenge")
    if token is None and isinstance(request.data, dict):
        token = request.data.get("challenge")
    if not token or not isinstance(token, str):
        raise PermissionDenied(CHALLENGE_REFUSAL)
    max_age = settings.ACCOUNT_CHALLENGE_MAX_AGE
    try:
        # SignatureExpired subclasses BadSignature, so one catch covers both.
        payload = TimestampSigner(key=secret, salt=CHALLENGE_SALT).unsign(
            token, max_age=max_age if max_age > 0 else None
        )
    except BadSignature:
        raise PermissionDenied(CHALLENGE_REFUSAL)
    if payload != CHALLENGE_VALUE:
        raise PermissionDenied(CHALLENGE_REFUSAL)


def _probe_challenge_ttl():
    # A zero window disables the age check, not the single-use record, so the
    # replay key must outlive the token instead of expiring with it.
    max_age = settings.ACCOUNT_CHALLENGE_MAX_AGE
    return max_age if max_age > 0 else None


def _enforce_probe_challenge(request):
    """Fail-closed challenge gate for the username probe.

    Registration keeps the optional gate so a zero-config deployment can still
    sign users up; the probe has no safe zero-config mode, so an unset secret
    refuses every call instead of restoring the public oracle. A token admitted
    here is single-use for the probe: the replay record is scoped to this
    endpoint so the same token may still be spent on another endpoint that
    shares the signed domain.
    """
    if not settings.ACCOUNT_CHALLENGE_SECRET:
        raise PermissionDenied(CHALLENGE_REFUSAL)
    _enforce_access_challenge(request)
    token = request.query_params.get("challenge")
    if token is None and isinstance(request.data, dict):
        token = request.data.get("challenge")
    digest = hashlib.sha256(token.encode()).hexdigest()
    if cache.add(f"{_PROBE_REPLAY_PREFIX}.{digest}", 1, timeout=_probe_challenge_ttl()):
        return
    raise PermissionDenied(CHALLENGE_REFUSAL)


class _UsernameProbeHardThrottle(SimpleRateThrottle):
    """Tighter per-IP cap on top of the view's ``username-probe`` scope.

    Challenge issuance lives outside this service (the owner-side edge gate
    mints the signed token), so minting cannot be throttled here; 3/min bounds
    the oracle's volume even when a caller holds a generous mint budget. The
    scoped 5/min default still applies, so the effective probe budget is the
    tighter 3/min.
    """

    scope = "username-probe-hard"
    rate = "3/min"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


@api_view(["POST"])
# Public: the account does not exist yet, so there is no credential to present.
@permission_classes([AllowAny])
@throttle_scope("register")
def register(request):
    """POST /api/accounts/register/ — the access gate.

    Throttled on its own ``register`` scope and gated by the signed-challenge
    hook when configured. The response carries no numeric ``user.id``: an
    auto-incrementing id in an anonymous response leaks sign-up ordering.
    """
    _enforce_access_challenge(request)

    serializer = RegisterSerializer(data=request.data)

    if serializer.is_valid():
        # Account creation and its audit row commit together.
        with transaction.atomic():
            user = serializer.save()
            AuditEvent.record(
                AuditEvent.EventType.AUTH_REGISTERED,
                actor=user,
                detail={"username": user.username},
            )

        # The pre-signup guest basket follows the new account; no basket mints
        # nothing and adds no key.
        cart = merge_session_cart_into_user_cart(request, user)
        body = {
            "message": "User registered. Check your email to verify the account.",
            "user": {
                "username": user.username,
                "email": user.email,
            },
        }
        if cart is not None:
            body["cart_id"] = cart.pk

        try:
            _send_verification_email(user)
        except Exception:
            return Response(
                {
                    "error": "Account created, but verification email could not be sent. Check SMTP settings.",
                    **({"cart_id": cart.pk} if cart is not None else {}),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(body, status=status.HTTP_201_CREATED)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
# Public: availability is asked before the account exists.
@permission_classes([AllowAny])
@throttle_scope("username-probe")
@throttle_classes([ScopedRateThrottle, _UsernameProbeHardThrottle])
def username_available(request):
    """The username probe behind a server-issued challenge. task: T-20261119-008

    The route stays public because the caller has no account yet, but only a
    fresh, unspent token earns the truthful boolean. The message and keys stay
    constant on both outcomes, so the body differs in that value only. A
    missing, foreign, stale or replayed token is refused identically, and an
    unset secret fails closed. The 'username-probe' scope (5/min) plus a hard
    3/min layer bound volume while issuance lives outside this service.
    """
    _enforce_probe_challenge(request)
    username = request.query_params.get("username", "").strip()

    if len(username) < 3:
        return Response(
            {"available": False, "message": "Username must be at least 3 characters."}
        )

    # One lookup feeds both answers; neither side short-circuits the work.
    exists = User.objects.filter(username__iexact=username).exists()
    return Response({"available": not exists, "message": _PROBE_MESSAGE})


# Token-carrying account mutations (no mail): the 'auth' budget and the entropy
# of their one-time tokens bound them.
@api_view(["POST"])
# Public: the emailed code is the credential this endpoint presents.
@permission_classes([AllowAny])
@throttle_scope("auth")
def verify_email(request):
    # Non-object JSON bodies have no .get; {} routes them into the invalid-link
    # 400 (the _body_value shape) instead of a 500.
    payload = request.data if isinstance(request.data, dict) else {}
    user = _get_user(payload.get("uid"))
    token = payload.get("token", "")
    if user is None or not default_token_generator.check_token(user, token):
        return Response(
            {"error": "This verification link is invalid or expired."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not user.is_active:
        # Activation and its audit row commit together; re-verifying an active
        # account is a no-op and writes nothing.
        with transaction.atomic():
            user.is_active = True
            user.save(update_fields=["is_active"])
            AuditEvent.record(
                AuditEvent.EventType.AUTH_EMAIL_VERIFIED,
                actor=user,
                detail={"username": user.username},
            )
    return Response({"message": "Email verified. You can now log in."})


# The three email-sending recovery endpoints get their own tighter 'recovery'
# scope: every accepted request sends mail, so the budget is the mail-bomb bound.
@api_view(["POST"])
# Public: recovery for a caller who cannot sign in yet.
@permission_classes([AllowAny])
@throttle_scope("recovery")
def resend_verification(request):
    payload = request.data if isinstance(request.data, dict) else {}
    email = payload.get("email", "").strip()
    user = User.objects.filter(email__iexact=email, is_active=False).first()
    if user:
        try:
            _send_verification_email(user)
        except Exception:
            return Response(
                {"error": "Verification email could not be sent. Check SMTP settings."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    return Response(
        {
            "message": "If an unverified account exists, a verification email has been sent."
        }
    )


@api_view(["POST"])
# Public: recovery starts from a caller with no usable credential.
@permission_classes([AllowAny])
@throttle_scope("recovery")
def forgot_username(request):
    payload = request.data if isinstance(request.data, dict) else {}
    email = payload.get("email", "").strip()
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
            return Response(
                {"error": "Username email could not be sent. Check SMTP settings."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    return Response(
        {"message": "If an account exists for this email, the username has been sent."}
    )


@api_view(["POST"])
# Public: the reset flow starts from a caller who cannot sign in.
@permission_classes([AllowAny])
@throttle_scope("recovery")
def request_password_reset(request):
    payload = request.data if isinstance(request.data, dict) else {}
    email = payload.get("email", "").strip()
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
            return Response(
                {
                    "error": "Password-reset email could not be sent. Check SMTP settings."
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    return Response(
        {
            "message": "If an active account exists for this email, a password-reset link has been sent."
        }
    )


@api_view(["POST"])
# Public: the single-use reset token is the credential being presented.
@permission_classes([AllowAny])
@throttle_scope("auth")
def reset_password(request):
    payload = request.data if isinstance(request.data, dict) else {}
    user = _get_user(payload.get("uid"))
    token = payload.get("token", "")
    password = payload.get("password", "")
    if user is None or not default_token_generator.check_token(user, token):
        return Response(
            {"error": "This password-reset link is invalid or expired."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        validate_password(password, user)
    except ValidationError as error:
        return Response(
            {"password": list(error.messages)}, status=status.HTTP_400_BAD_REQUEST
        )
    # The credential change and its audit row commit together; the one-time token
    # is deliberately not stored in detail (single-use evidence, not audit data).
    with transaction.atomic():
        user.set_password(password)
        user.save(update_fields=["password"])
        # Every outstanding session dies with the password change, in the same
        # transaction — no window where old sessions outlive new credentials.
        _blacklist_user_refresh_tokens(user)
        AuditEvent.record(
            AuditEvent.EventType.AUTH_PASSWORD_RESET,
            actor=user,
            detail={"username": user.username},
        )
        # Security alert beside the audit hook, in the same atomic block. It
        # carries the username only and is log-only on failure, so an alert outage
        # cannot undo a completed reset; the inbox is omitted deliberately.
        alerts.notify_security_change(
            f"Password reset completed for user {user.username} (id {user.pk})."
        )
    return Response({"message": "Password reset successfully. You can now log in."})


# --- SPEC-17-05 [R-17.9]: TOTP enrollment for the MFA-required roles -------
# Every endpoint here serves exactly the population enforcement blocks at
# login (PRISTINE-13b asks requires_mfa at all three doors):
# status/disable/trust are IsMfaRequired-gated, and setup/confirm carry
# their own gate (a JWT session, or the credential bootstrap while no
# device is active). The 'auth' scope bounds brute-forcing the 6-digit
# code space like every other identity flow. Secrets leave the server
# exactly once (setup response) and never again; nothing below returns or
# logs one.


class IsMfaRequired(BasePermission):
    """The enrollment gate: exactly the accounts the login doors challenge.

    ``common.permissions.IsPrivilegedRole`` answers the pre-13a question
    (superuser / ``staff.manage``) and lives in a module this task may not
    edit. Pinning the adapter here, over the same ``requires_mfa`` the
    doors call, keeps enrollment and enforcement on ONE predicate -- an
    account can never be blocked at login with no route to the factor that
    blocks it. Denials raise ``PermissionDenied`` for the staff API's
    uniform 403 rather than returning False, which DRF would render as a
    401 for a guest (same reasoning as ``CapabilityPermission``).
    """

    message = "Multi-factor authentication management requires a role subject to MFA."

    def has_permission(self, request, view):
        if requires_mfa(request.user):
            return True
        raise PermissionDenied(self.message)


def _body_value(request, key):
    # request.data may be any parsed JSON (a list body has no .get).
    payload = request.data if isinstance(request.data, dict) else {}
    return payload.get(key)


def _authorize_enrollment(request):
    """Shared setup/confirm gate: an MFA-required JWT caller, or the
    credential bootstrap while no device is active (see MFASetupView).
    Returns the target user, or an error response."""
    user = request.user
    if not user.is_authenticated:
        payload = request.data if isinstance(request.data, dict) else {}
        user = authenticate(
            request=request,
            username=str(payload.get("username", "") or ""),
            password=str(payload.get("password", "") or ""),
        )
        if user is None:
            # Uniform rejection: no signal whether username, password or both
            # were wrong.
            return None, Response(
                {"error": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if TOTPDevice.active_for(user) is not None:
            # Credentials alone must never reach an account with an active device.
            return None, Response(
                {
                    "error": "A device is already enrolled. Sign in and use "
                    "the authenticated setup path."
                },
                status=status.HTTP_403_FORBIDDEN,
            )
    if not requires_mfa(user):
        return None, Response(
            {"error": IsMfaRequired.message},
            status=status.HTTP_403_FORBIDDEN,
        )
    return user, None


def _consume_code(device, code):
    """Verify `code` against `device` and persist the replay watermark.

    Returns the matched counter, or None when wrong/expired/replayed (in which
    case nothing is written).
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

    permission_classes = [IsMfaRequired]
    # The accounts URLConf wiring guard requires a scope on every route;
    # 'auth' matches the sibling MFA endpoints (this one is read-only, but
    # the budget also bounds enabled-state probing).
    throttle_scope = "auth"

    def get(self, request):
        return Response({"enabled": TOTPDevice.active_for(request.user) is not None})


class MFASetupView(APIView):
    """Mint a fresh secret, otpauth URI and scannable QR in one response.

    Returns the base32 secret, the otpauth:// URI for the user's
    authenticator app, and (SPEC-20-9) a scannable QR of that same URI so
    nobody has to transcribe a secret by hand. All three ride ONE response:
    the secret leaves the server exactly once, so a separate QR endpoint
    would have to re-disclose it. The QR is presentation only — every
    decision about the factor still reads the secret and the URI, never the
    picture. Two deliberate trust paths:

    - JWT session (MFA-required account): steady-state path. Re-enrolling
      while a device is enabled must also re-prove the second factor with
      ``code`` — a session thief who could re-enroll freely would own the new
      secret.
    - Credential bootstrap (anonymous): the rollout path that keeps
      blocked-at-login enforcement reachable. An MFA-required account with
      no active device can never obtain a JWT (login demands the factor it
      does not have yet), so setup additionally accepts username+password
      — exactly the first-factor-enrollment window every mainstream
      authenticator flow grants a password-proven identity. Once a device
      is active this path closes: credentials alone never touch an
      enrolled account (that is the whole point of the factor), and the
      'auth' throttle bounds the credential attempts.

    The fresh row starts unconfirmed and disabled; nothing changes until a
    valid confirm.
    """

    # Public: an unconfirmed device holds no JWT; the enrollment proof is the
    # credential (_authorize_enrollment refuses everything else).
    permission_classes = [AllowAny]
    throttle_scope = "auth"

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
            # get_or_create retries the IntegrityError; setup always mints a fresh
            # secret, never reuses a stale one.
            device, _ = TOTPDevice.objects.get_or_create(user=user)
            device.secret = totp.generate_secret()
            device.enabled = False
            device.confirmed_at = None
            device.last_used_counter = None
            # A rotated secret is a new factor, so any prior trust lapses rather
            # than carrying over to a secret nobody has vetted.
            device.trusted_until = None
            device.save(
                update_fields=[
                    "secret",
                    "enabled",
                    "confirmed_at",
                    "last_used_counter",
                    "trusted_until",
                ]
            )
        uri = totp.otpauth_uri(device.secret, user.username)
        return Response(
            {
                "secret": device.secret,
                "otpauth_uri": uri,
                "qr_data_uri": qr.qr_data_uri(uri),
            }
        )


class MFAConfirmView(APIView):
    """POST /api/accounts/mfa/confirm/ {code} — verify and enable the device.

    Shares the setup view's dual trust path: a bootstrapped user still holds no
    JWT, so confirm accepts the same credential proof while no device is active.
    Only a pending device can be confirmed, so the code flips enabled for exactly
    the secret just enrolled.
    """

    # Public: a pending device cannot hold a JWT yet, so the same enrollment
    # proof is the credential.
    permission_classes = [AllowAny]
    throttle_scope = "auth"

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
            device.save(update_fields=["confirmed_at", "enabled", "last_used_counter"])
        return Response({"enabled": True})


class MFADisableView(APIView):
    """POST /api/accounts/mfa/disable/ {code} — turn MFA off.

    Disabling requires a valid code from the enabled device, so a stolen session
    or password alone cannot strip the factor. The row survives (enrollment
    history) but the secret is dead for verification; re-enrollment mints a new one.
    """

    permission_classes = [IsMfaRequired]
    throttle_scope = "auth"

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
            # A factor that is off has nothing to trust; clearing the TTL means a
            # later re-enrollment starts from the challenge.
            device.trusted_until = None
            device.save(update_fields=["enabled", "trusted_until"])
        return Response({"enabled": False})


class MFATrustDeviceView(APIView):
    """POST /api/accounts/mfa/trust/ {code} — trust THIS device for N days.

    SPEC-20-8: the opt-in half of "trust this device for 30 days". It is
    never reached implicitly — the MFA-required user asks for it, from a
    session that already holds a JWT (which itself required the factor), and
    this call re-proves the factor with a FRESH code. That second proof is
    deliberate: a stolen session must not be able to convert itself into
    thirty days of challenge-free logins, and the fresh code goes through
    the same replay-guarded ``_consume_code`` as every other code, so
    granting trust spends a one-time code exactly once.

    Gated and throttled exactly like its siblings (IsMfaRequired, 'auth'),
    and it can only ever write to the caller's own device row.
    """

    permission_classes = [IsMfaRequired]
    throttle_scope = "auth"

    def post(self, request):
        device = TOTPDevice.active_for(request.user)
        if device is None:
            # No factor to make trustworthy; this names the missing precondition
            # rather than the login-path enrollment message.
            return Response(
                {"code": ["Multi-factor authentication is not enabled."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if _consume_code(device, _body_value(request, "code")) is None:
            return Response(
                {"code": [MFA_CODE_INVALID]}, status=status.HTTP_400_BAD_REQUEST
            )
        with transaction.atomic():
            deadline = mfa_trust.grant_trust(device)
        response = Response(
            {
                "trusted": True,
                "trusted_until": deadline,
                "trust_days": settings.MFA_TRUST_DAYS,
            }
        )
        return mfa_trust.bind_trust_cookie(response, request.user, device)

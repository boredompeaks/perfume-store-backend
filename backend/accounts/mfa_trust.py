"""Opt-in "trust this device" skip for the privileged MFA door.

This is the only place that decides whether a login may skip the challenge:
trust is opt-in, bound to the (user, device) browser cookie, bounded by the
server-side TTL, and never substitutes for the one-time code. ``MFA_TRUST_DAYS``
is env-driven; 0 is a deployment-wide kill switch.
"""

from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone

SECONDS_PER_DAY = 86400
# Own salt: a token minted for another purpose can never be replayed as a trust cookie.
TRUST_SALT = "accounts.mfa.trusted-device"


def trust_deadline():
    """The instant a freshly granted trust stops covering its device."""
    return timezone.now() + timedelta(days=settings.MFA_TRUST_DAYS)


def grant_trust(device):
    """Stamp the TTL on ``device`` and return the deadline.

    The caller owns the surrounding transaction; ``update_fields`` leaves the
    secret, replay watermark and enrollment timestamps untouched.
    """
    deadline = trust_deadline()
    device.trusted_until = deadline
    device.save(update_fields=["trusted_until"])
    return deadline


def bind_trust_cookie(response, user, device):
    """Attach the signed (user, device) trust marker to ``response``.

    HttpOnly so injected script cannot read or forge it, and SameSite=Strict so
    it is not sent on a cross-site navigation; ``max_age`` is the device's
    remaining trust, so a shortened TTL expires the cookie immediately.
    """
    remaining = int((device.trusted_until - timezone.now()).total_seconds())
    response.set_cookie(
        settings.MFA_TRUST_COOKIE_NAME,
        signing.dumps({"user": user.pk, "device": device.pk}, salt=TRUST_SALT),
        max_age=max(remaining, 0),
        httponly=True,
        secure=not settings.DEBUG,
        samesite=settings.MFA_TRUST_COOKIE_SAMESITE,
    )
    return response


def is_trusted_device(request, user, device):
    """True only when THIS request comes from a device ``user`` trusted.

    Both halves must hold: the server-side grant has not lapsed, and the caller
    presents an unexpired, untampered marker naming this user and this very
    device row. It is the only skip decision in the system, shared by the staff
    API and the Django admin form.
    """
    if device.trusted_until is None or device.trusted_until <= timezone.now():
        return False
    # No request means no browser to vouch for it: challenge rather than 500.
    cookies = getattr(request, "COOKIES", None) or {}
    raw = cookies.get(settings.MFA_TRUST_COOKIE_NAME)
    if not raw:
        return False
    try:
        claimed = signing.loads(
            raw, salt=TRUST_SALT, max_age=settings.MFA_TRUST_DAYS * SECONDS_PER_DAY
        )
    except signing.BadSignature:
        # Tampered, wrong salt, or older than the TTL (SignatureExpired is a
        # BadSignature): all of them mean "challenge".
        return False
    return claimed.get("user") == user.pk and claimed.get("device") == device.pk

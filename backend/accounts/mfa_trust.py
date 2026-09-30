"""SPEC-20-8: "trust this device for N days" on the privileged MFA door.

R-17.9 makes the second factor mandatory for privileged roles, and a code
demanded on EVERY login is a support burden the user directive explicitly
rejects. This module is the whole opt-in: it is the only place that decides
whether a login may skip the challenge, and it is built so that skipping is
the narrowest thing the system can do:

- Opt-in only. ``TOTPDevice.trusted_until`` starts NULL and is written by
  exactly one call (:func:`grant_trust`), which the trust endpoint reaches
  only from an authenticated privileged session that has just re-proved the
  factor with a fresh code. Nothing in the login path can grant it.
- Bound to a browser, not to the account. A signed cookie carries the
  (user, device) pair the trust was granted for, so a stolen password alone
  still earns the challenge from any other browser: the DB row alone would
  have degraded MFA to password-only for the whole TTL.
- Bounded twice. The server-side ``trusted_until`` is authoritative; the
  cookie's own max_age and the signature's timestamp age are the belt to its
  braces, so the client artifact cannot outlive the grant even if the TTL
  setting is raised later.
- Never a substitute for the code. Trust cannot make a one-time code
  replayable: the challenge path (serializer -> totp.verify_code ->
  last_used_counter) is unchanged, and trust only decides whether that path
  runs at all.

Both enforcement surfaces consult :func:`is_trusted_device` and nothing else,
so "don't challenge me every login" means the same thing at the staff API door
and at ``/admin/`` (SPEC-20-8b).

The TTL is env-driven (``MFA_TRUST_DAYS``, default 30); 0 is a safe
deployment-wide kill switch that puts every privileged login straight back
behind the challenge.
"""
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone

SECONDS_PER_DAY = 86400
# A signing salt of its own: a token minted for any other purpose can never
# be replayed as a trust cookie, even by code that signs something.
TRUST_SALT = "accounts.mfa.trusted-device"


def trust_deadline():
    """The instant a freshly granted trust stops covering its device."""
    return timezone.now() + timedelta(days=settings.MFA_TRUST_DAYS)


def grant_trust(device):
    """Stamp the TTL on ``device`` and return the deadline.

    The caller owns the surrounding transaction; the explicit
    ``update_fields`` write keeps the secret, the replay watermark and the
    enrollment timestamps untouched by a trust grant.
    """
    deadline = trust_deadline()
    device.trusted_until = deadline
    device.save(update_fields=["trusted_until"])
    return deadline


def bind_trust_cookie(response, user, device):
    """Attach the signed (user, device) trust marker to ``response``.

    HttpOnly so injected script cannot read or forge it, and SameSite=Strict
    so it is not even sent on a cross-site navigation. The max_age is the
    device's REMAINING trust, so a shortened or already-expired TTL expires
    the client artifact immediately instead of leaving a live cookie behind.
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

    Both halves must hold: the server-side grant has not lapsed, and the
    caller presents an unexpired, untampered marker naming this user and
    this very device row (a shared browser therefore never carries one
    user's trust into another's login).

    This is the ONLY skip decision in the system, shared by both privileged
    doors: the staff login serializer (DRF) and the Django admin login form
    (SPEC-20-8b). All it needs of the caller is a cookie jar, so the plain
    ``HttpRequest`` the admin form carries works exactly like the DRF
    request — a second implementation would only be a second thing to drift.
    """
    if device.trusted_until is None or device.trusted_until <= timezone.now():
        return False
    # A caller with no request to read (a form built without one) has no
    # browser to vouch for, so it earns the challenge rather than a 500.
    cookies = getattr(request, "COOKIES", None) or {}
    raw = cookies.get(settings.MFA_TRUST_COOKIE_NAME)
    if not raw:
        return False
    try:
        claimed = signing.loads(
            raw, salt=TRUST_SALT, max_age=settings.MFA_TRUST_DAYS * SECONDS_PER_DAY
        )
    except signing.BadSignature:
        # Tampered, salted for something else, or older than the TTL
        # (SignatureExpired is a BadSignature): all of them mean "challenge".
        return False
    return claimed.get("user") == user.pk and claimed.get("device") == device.pk

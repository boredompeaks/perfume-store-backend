"""RFC 6238 TOTP (time-based one-time passwords) — stdlib-only.

SPEC-17-05 [R-17.9] ("Staff MFA, preferably mandatory for privileged
roles") names no mechanism and demands no artefact (no QR image, no
specific library) beyond the factor itself, so the helper is implemented
on the standard library alone (hmac/hashlib/base64/secrets): HOTP per
RFC 4226 with the SHA-1 algorithm every authenticator ships, wrapped in
the RFC 6238 time step, plus the ``otpauth://`` enrollment URI the user's
authenticator app accepts. Correctness is pinned against the public RFC
6238 SHA-1 test vectors in tests/test_mfa.py.

TOTP parameters are code constants, not env config: a drift in the step,
digit count or window between server and enrollment URI would silently
break every staff login, so they are deliberately not deployment-tunable.
"""
import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

# RFC 6238 defaults: 30-second time step, 6 digits, ±1 step of clock drift
# tolerance (a code is accepted for the previous, current and next step —
# RFC 6238 §5.2 recommends exactly this).
STEP = 30
DIGITS = 6
DRIFT_WINDOW = 1
# 160-bit secrets (RFC 4226 §4 recommends >= 128 bits): 20 random bytes
# render to 32 unpadded base32 characters.
SECRET_BYTES = 20


def now():
    """Unix time the callers verify against; the single time source so
    tests can patch one import point and stay hermetic (no sleeps)."""
    return int(time.time())


def generate_secret():
    """A fresh random base32 secret, unpadded (authenticator-app shape)."""
    raw = base64.b32encode(secrets.token_bytes(SECRET_BYTES))
    return raw.decode("ascii").rstrip("=")


def _b32decode(secret):
    # Secrets are stored without padding; restore it for the decoder and
    # accept lowercase (casefold) so a hand-typed secret still works.
    padding = "=" * ((8 - len(secret) % 8) % 8)
    return base64.b32decode(secret + padding, casefold=True)


def hotp(secret, counter, digits=DIGITS):
    """RFC 4226 HOTP: HMAC-SHA-1 over the 8-byte big-endian counter,
    dynamic truncation to `digits` decimal digits."""
    message = int(counter).to_bytes(8, "big")
    digest = hmac.new(_b32decode(secret), message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (
        int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    ) % (10**digits)
    return str(code).zfill(digits)


def totp(secret, at_time):
    """The TOTP code valid at Unix time `at_time`."""
    return hotp(secret, int(at_time) // STEP)


def otpauth_uri(secret, account_label, issuer="Perfume Store"):
    """The otpauth:// enrollment URI (Google authenticator-key URI format):
    the label is `issuer:account` so the app displays both, and the params
    pin exactly the algorithm hotp() implements. The colon between issuer
    and account stays literal (the apps' canonical delimiter); each side
    is percent-encoded separately."""
    label = f"{quote(issuer)}:{quote(account_label)}"
    return (
        f"otpauth://totp/{label}?"
        f"secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP}"
    )


def verify_code(secret, code, at_time, last_used_counter=None, window=DRIFT_WINDOW):
    """Return the matched TOTP counter, or None.

    Accepts codes for the current step and `window` steps on either side
    of `at_time`. `last_used_counter` is the replay guard (RFC 6238 §5.2):
    any counter at or below it is skipped, so a code already consumed for
    this device can never verify again — even inside its own validity
    window. Non-numeric, wrong-length or missing codes fail closed (None);
    comparison is constant-time via compare_digest.
    """
    if code is None:
        return None
    normalized = str(code).strip().replace(" ", "")
    if not normalized.isdigit() or len(normalized) != DIGITS:
        return None
    base = int(at_time) // STEP
    candidate = int(normalized)
    for offset in range(-window, window + 1):
        counter = base + offset
        if last_used_counter is not None and counter <= last_used_counter:
            continue
        if hmac.compare_digest(hotp(secret, counter), normalized):
            return counter
    return None

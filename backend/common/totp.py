"""RFC 6238 TOTP, stdlib-only.

HOTP/SHA-1 wrapped in the RFC time step, plus the ``otpauth://`` enrollment URI.
Parameters are code constants: drift between server and enrollment would silently
break every staff login.
"""

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

# RFC 6238 defaults: 30-second step, 6 digits, ±1 step drift tolerance (§5.2).
STEP = 30
DIGITS = 6
DRIFT_WINDOW = 1
# 20 random bytes (160 bits, RFC 4226 §4 asks >= 128) render to 32 base32 chars.
SECRET_BYTES = 20


def now():
    """Unix time; the single time source so tests can patch one point."""
    return int(time.time())


def generate_secret():
    """A fresh random base32 secret, unpadded (authenticator-app shape)."""
    raw = base64.b32encode(secrets.token_bytes(SECRET_BYTES))
    return raw.decode("ascii").rstrip("=")


def _b32decode(secret):
    # Stored secrets are unpadded; restore padding and accept lowercase.
    padding = "=" * ((8 - len(secret) % 8) % 8)
    return base64.b32decode(secret + padding, casefold=True)


def hotp(secret, counter, digits=DIGITS):
    """RFC 4226 HOTP: HMAC-SHA-1 over the counter, dynamically truncated."""
    message = int(counter).to_bytes(8, "big")
    digest = hmac.new(_b32decode(secret), message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF) % (
        10**digits
    )
    return str(code).zfill(digits)


def totp(secret, at_time):
    """The TOTP code valid at Unix time `at_time`."""
    return hotp(secret, int(at_time) // STEP)


def otpauth_uri(secret, account_label, issuer="Perfume Store"):
    """The otpauth:// enrollment URI: label ``issuer:account``, each side
    percent-encoded, and params pinning the algorithm ``hotp`` implements."""
    label = f"{quote(issuer)}:{quote(account_label)}"
    return (
        f"otpauth://totp/{label}?"
        f"secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP}"
    )


def verify_code(secret, code, at_time, last_used_counter=None, window=DRIFT_WINDOW):
    """Return the matched counter, or None.

    ``last_used_counter`` is the replay guard (RFC 6238 §5.2); malformed codes
    fail closed, and comparison is constant-time.
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

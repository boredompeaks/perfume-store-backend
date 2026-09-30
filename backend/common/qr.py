"""Provisioning URI to scannable QR code (SPEC-20-9).

``common/totp.py`` is stdlib-only on purpose — the RFC 6238 factor must not
drag a crypto dependency into the login path — but a user cannot type a
32-character base32 secret into an authenticator app, so enrollment has to
be scannable. Turning the ``otpauth://`` URI into a picture is presentation,
which is why it lives here: this module is the one place a QR encoder is
allowed. `segno <https://segno.readthedocs.io/>`_ is pure Python with no
runtime dependencies and emits SVG without an image library, so a
deployment gains a scannable code without adding a rasterizer (Pillow is
already present for uploads; enrollment deliberately does not depend on it).

The artifact is a ``data:`` URI carried IN the setup response, not a second
endpoint: the secret may leave the server exactly once, so a QR endpoint
would have to either re-disclose the secret or invent a token for it.
Rendering an SVG data URI into an ``<img src>`` executes nothing, and the
storefront CSP already admits ``data:`` images.

Rendering parameters are code constants, not env config (same reasoning as
the TOTP step/digits in totp.py): they change how big the picture is, and a
deployment that could mis-tune them could ship an unscannable code.
"""
import base64
import io

import segno

# Output pixels per QR module. 6 keeps a ~40-module code near 250px, which
# scans comfortably on a phone without turning into a huge response.
SVG_SCALE = 6
# Quiet zone in modules. The QR specification asks for at least 4; fewer
# makes a code that fills its frame unreliable to scan.
SVG_BORDER = 4
# ~15% recovery: a code shown on a scuffed or photographed screen still
# decodes, without inflating the module count for a value the user can also
# read as text.
ERROR_CORRECTION = "m"
# Base64 keeps the payload free of characters that would need escaping inside
# JSON, a CSS url() or an <img src>; the scheme pins it to an image so it can
# never be interpreted as markup.
SVG_DATA_URI_PREFIX = "data:image/svg+xml;base64,"


def qr_data_uri(uri):
    """``uri`` as an inline ``data:image/svg+xml;base64,...`` QR code.

    The caller owns disclosure: this only formats, so the "shown exactly
    once" discipline stays with the view that calls it (MFASetupView).
    """
    # segno's SVG serializer writes bytes, so the buffer is a BytesIO.
    buffer = io.BytesIO()
    segno.make(uri, error=ERROR_CORRECTION).save(
        buffer, kind="svg", scale=SVG_SCALE, border=SVG_BORDER, xmldecl=False
    )
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"{SVG_DATA_URI_PREFIX}{encoded}"
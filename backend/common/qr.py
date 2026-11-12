"""Provisioning URI to scannable QR code.

Rendered as an inline SVG data URI carried in the setup response, so the secret
leaves the server only once; parameters are constants so a deployment cannot ship
an unscannable code.
"""

import base64
import io

import segno

# ~250px for a typical code: scans on a phone without a huge response.
SVG_SCALE = 6
# The QR spec asks for at least 4 quiet-zone modules.
SVG_BORDER = 4
# ~15% recovery: survives a scuffed or photographed screen.
ERROR_CORRECTION = "m"
# Base64 avoids escaping inside JSON/CSS/<img>; the scheme pins it to an image.
SVG_DATA_URI_PREFIX = "data:image/svg+xml;base64,"


def qr_data_uri(uri):
    """Return ``uri`` as an inline SVG data URI; the caller owns disclosure."""
    buffer = io.BytesIO()
    segno.make(uri, error=ERROR_CORRECTION).save(
        buffer, kind="svg", scale=SVG_SCALE, border=SVG_BORDER, xmldecl=False
    )
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"{SVG_DATA_URI_PREFIX}{encoded}"

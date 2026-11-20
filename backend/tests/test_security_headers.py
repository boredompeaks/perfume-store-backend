"""Emitted browser-security headers on Django's own responses (AUD-010).

Django serves the admin and the API; the storefront's headers belong to Next.
The pins below read the response, so a setting that never reaches a header
cannot pass. The two header values that Django already ships by default are
also pinned in settings.py, because pinning them is the change under test.
"""

import re
from pathlib import Path

from django.test import TestCase

import config.settings as config_settings

_SETTINGS_SOURCE = Path(config_settings.__file__).read_text(encoding="utf-8")


def _directive(csp, name):
    for part in csp.split(";"):
        part = part.strip()
        if part.partition(" ")[0] == name:
            return part
    return ""


class ExplicitHeaderSettingTests(TestCase):
    def test_nosniff_policy_is_explicit(self):
        response = self.client.get("/health/")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("SECURE_CONTENT_TYPE_NOSNIFF = True", _SETTINGS_SOURCE)

    def test_frame_policy_is_explicit_deny(self):
        response = self.client.get("/health/")
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
        self.assertIn('X_FRAME_OPTIONS = "DENY"', _SETTINGS_SOURCE)

    def test_referrer_policy_is_explicit_strict_origin(self):
        response = self.client.get("/health/")
        self.assertEqual(
            response.headers.get("Referrer-Policy"),
            "strict-origin-when-cross-origin",
        )


# task: T-20261119-002
class ContentSecurityPolicyTests(TestCase):
    def test_admin_sets_csp(self):
        response = self.client.get("/admin/login/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Content-Security-Policy", response.headers)
        csp = response.headers["Content-Security-Policy"]
        script_src = _directive(csp, "script-src")
        self.assertIn("'self'", script_src)
        self.assertNotIn("'unsafe-inline'", script_src)
        nonce = re.search(r"'nonce-([^']+)'", script_src)
        self.assertIsNotNone(nonce, csp)
        # The admin page must carry the nonce the header allows, or its own
        # scripts and styles are blocked and the admin is unusable.
        self.assertIn(f'nonce="{nonce.group(1)}"', response.content.decode())
        self.assertIn("'unsafe-inline'", _directive(csp, "style-src-attr"))
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)

        api = self.client.get("/health/")
        self.assertIn("Content-Security-Policy", api.headers)
        self.assertIn("default-src 'self'", api.headers["Content-Security-Policy"])

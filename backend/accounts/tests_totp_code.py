"""``manage.py totp_code``: the local test helper for the MFA login walk.

Pins the command against the RFC 6238 SHA-1 vector at Unix time 59 (the
6-digit code is hand-written, never recomputed from ``common.totp``), proves
the printed code verifies through the production replay guard, and holds the
exposure line: no secret, and a clear refusal when there is no active factor.
"""

import re
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import TOTPDevice
from common import totp

# RFC 6238 SHA-1 key ("12345678901234567890" in base32) and its 8-digit vector
# at Unix time 59 ("94287082"); the 6-digit form is the low six digits.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC_TIME = 59
RFC_SIX_DIGIT_CODE = "287082"
RFC_COUNTER = 1


def _run(username):
    out = StringIO()
    call_command("totp_code", username, stdout=out)
    return out.getvalue()


class TOTPCodeCommandTests(TestCase):
    def _user(self, username="alice"):
        return User.objects.create_user(username=username, password="fixture")

    def _device(self, user, **overrides):
        defaults = {
            "secret": RFC_SECRET,
            "enabled": True,
            "confirmed_at": timezone.now(),
        }
        defaults.update(overrides)
        return TOTPDevice.objects.create(user=user, **defaults)

    def test_prints_the_rfc_code_and_remaining_seconds_without_the_secret(self):
        user = self._user()
        self._device(user)

        with mock.patch("common.totp.now", return_value=RFC_TIME):
            out = _run("alice")

        self.assertIn(f"code: {RFC_SIX_DIGIT_CODE}", out)
        # 59 seconds leaves 1 of the 30-second step (59 % 30 == 29).
        self.assertIn("valid for: 1s", out)
        self.assertIn("LOCAL TEST HELPER", out)
        self.assertNotIn(RFC_SECRET, out)
        self.assertRegex(out, r"code: \d{6}\b")

    def test_printed_code_verifies_through_the_production_guard(self):
        user = self._user()
        device = self._device(user)

        with mock.patch("common.totp.now", return_value=RFC_TIME):
            out = _run("alice")

        code = re.search(r"code: (\d{6})\b", out).group(1)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        self.assertEqual(
            totp.verify_code(
                device.secret,
                code,
                RFC_TIME,
                last_used_counter=device.last_used_counter,
            ),
            RFC_COUNTER,
        )

    def test_refuses_a_disabled_or_unconfirmed_device(self):
        disabled = self._user("disabled")
        self._device(disabled, enabled=False)
        pending = self._user("pending")
        self._device(pending, confirmed_at=None)

        for username in ("disabled", "pending"):
            with self.subTest(username=username):
                with self.assertRaisesMessage(
                    CommandError, "no confirmed, enabled TOTP device"
                ):
                    _run(username)

    def test_refuses_a_user_with_no_device(self):
        self._user("nodevice")
        with self.assertRaisesMessage(CommandError, "no confirmed, enabled"):
            _run("nodevice")

    def test_refuses_an_unknown_user(self):
        with self.assertRaisesMessage(CommandError, "no user named 'ghost'"):
            _run("ghost")

"""SPEC-17-05 [R-17.9]: mandatory TOTP MFA for privileged roles.

Pins the whole feature end to end:

- common/totp.py against the public RFC 6238 SHA-1 test vectors and every
  branch of the verification/replay logic (stdlib-only helper — R-17.9
  names no mechanism, so no package was added);
- the enrollment API (setup/confirm/status/disable) for privileged roles,
  both trust paths (JWT steady-state and the credential bootstrap that
  keeps blocked-at-login enforcement reachable);
- SPEC-20-13 enrollment REACH: the admin login page offers the enrollment
  surface (and says so loudly when MFA is what blocked the attempt), and the
  bootstrap flow the enrollment page drives mints a usable device without a
  JWT;
- enforcement at BOTH surfaces: the staff API login (totp required for
  privileged users, replay-guarded) and the Django admin login form;
- SPEC-20-10 door split: the storefront login declares no totp field at all
  and refuses privileged accounts, while the staff door keeps demanding and
  spending the factor;
- rollout semantics: unenrolled privileged accounts are blocked with the
  enrollment path named; customers and non-admin staff are untouched;
- SPEC-20-8 "trust this device for N days": the opt-in is explicit, is bound
  to the browser that asked for it, lapses on the env-driven TTL, is dropped
  by disable/re-enrollment, and never touches the challenge path, the replay
  guard or the customer door;
- SPEC-20-8b the same opt-in at the Django admin door, decided by the SAME
  shared function, with every fail-safe (other browser, lapsed grant, forged
  or other-user marker, the TTL kill switch) still challenged there;
- secret exposure: shown exactly once at setup, never returned again.
"""
import base64
from datetime import timedelta
from unittest import mock
from xml.etree import ElementTree

import segno
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core import signing
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from accounts import mfa_trust
from accounts.models import TOTPDevice
from accounts.serializers import StorefrontTokenObtainPairSerializer
from common import qr, totp
from common.models import AuditEvent
from common.permissions import is_privileged
from common.roles import ROLE_ADMIN, ROLE_SUPPORT
from common.testing import TEST_TOTP_SECRET, ApiTestCase

PASSWORD = "S3cure-Passphrase!"

# RFC 6238 appendix B vectors (SHA-1): the 20-byte ASCII secret
# "12345678901234567890" is base32 "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ".
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC_8DIGIT_VECTORS = (
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
)


def make_privileged(username, is_superuser=False):
    """An MFA-privileged account (admin role, or superuser) with NO device."""
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=PASSWORD,
        is_staff=True,
        is_superuser=is_superuser,
    )
    if not is_superuser:
        user.groups.add(Group.objects.get_or_create(name=ROLE_ADMIN)[0])
    return user


def enroll_via_model(user, secret=TEST_TOTP_SECRET):
    """Fixture shortcut: a confirmed, enabled device."""
    return TOTPDevice.objects.create(
        user=user, secret=secret, enabled=True, confirmed_at=timezone.now()
    )


def login_payload(username, password=PASSWORD, **extra):
    payload = {"username": username, "password": password}
    payload.update(extra)
    return payload


def code_after(device):
    """The code right after the one a login consumed.

    api_login (and every successful verification) stores the matched
    counter as the replay watermark, so the next acceptable code is
    watermark + 1 — inside the ±1 step window whether or not a 30-second
    boundary passes mid-test (hermetic: no sleeps, no clock patching).
    The instance is refreshed first: api_login rewinds the watermark in
    the database, not on the caller's object.
    """
    device.refresh_from_db()
    return totp.hotp(device.secret, device.last_used_counter + 1)


class TOTPHelperTests(SimpleTestCase):
    def test_rfc6238_sha1_vectors(self):
        for at_time, expected in RFC_8DIGIT_VECTORS:
            with self.subTest(at_time=at_time):
                self.assertEqual(
                    totp.hotp(RFC_SECRET, at_time // totp.STEP, digits=8), expected
                )

    def test_rfc6238_six_digit_totp(self):
        # 6-digit codes are the low six digits of the 8-digit vector.
        for at_time, expected8 in RFC_8DIGIT_VECTORS:
            with self.subTest(at_time=at_time):
                self.assertEqual(totp.totp(RFC_SECRET, at_time), expected8[-6:])

    def test_generate_secret_shape_and_uniqueness(self):
        secret = totp.generate_secret()
        self.assertEqual(len(secret), 32)
        self.assertNotIn("=", secret)
        self.assertTrue(set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"))
        self.assertNotEqual(secret, totp.generate_secret())

    def test_otpauth_uri_shape(self):
        uri = totp.otpauth_uri("GEZDGNBV234567", "alice@example.com")
        self.assertTrue(uri.startswith("otpauth://totp/Perfume%20Store:alice"))
        self.assertIn("secret=GEZDGNBV234567", uri)
        self.assertIn("issuer=Perfume%20Store", uri)
        self.assertIn("algorithm=SHA1", uri)
        self.assertIn("digits=6", uri)
        self.assertIn("period=30", uri)

    def test_verify_accepts_current_previous_and_next_step(self):
        at_time = 1_000_000
        base = at_time // totp.STEP
        self.assertEqual(
            totp.verify_code(RFC_SECRET, totp.hotp(RFC_SECRET, base), at_time), base
        )
        self.assertEqual(
            totp.verify_code(RFC_SECRET, totp.hotp(RFC_SECRET, base - 1), at_time),
            base - 1,
        )
        self.assertEqual(
            totp.verify_code(RFC_SECRET, totp.hotp(RFC_SECRET, base + 1), at_time),
            base + 1,
        )

    def test_verify_rejects_bad_input(self):
        at_time = 1_000_000
        for bad in (None, "", "12345", "1234567", "abcdef", "12.345"):
            with self.subTest(code=bad):
                self.assertIsNone(totp.verify_code(RFC_SECRET, bad, at_time))

    def test_verify_normalizes_spaces(self):
        at_time = 1_000_000
        code = totp.hotp(RFC_SECRET, at_time // totp.STEP)
        spaced = f"  {code[:3]} {code[3:]}  "
        self.assertEqual(
            totp.verify_code(RFC_SECRET, spaced, at_time), at_time // totp.STEP
        )

    def test_verify_rejects_code_outside_window(self):
        at_time = 1_000_000
        old = totp.hotp(RFC_SECRET, at_time // totp.STEP - 2)
        self.assertIsNone(totp.verify_code(RFC_SECRET, old, at_time))

    def test_replay_guard_skips_used_counters(self):
        at_time = 1_000_000
        base = at_time // totp.STEP
        code = totp.hotp(RFC_SECRET, base)
        self.assertEqual(totp.verify_code(RFC_SECRET, code, at_time), base)
        # Same code after the counter was consumed: every candidate counter
        # in the window is at or below the watermark, so nothing matches.
        self.assertIsNone(
            totp.verify_code(RFC_SECRET, code, at_time, last_used_counter=base)
        )
        # A watermark above the whole window also fails closed.
        self.assertIsNone(
            totp.verify_code(RFC_SECRET, code, at_time, last_used_counter=base + 5)
        )


class PrivilegedPredicateTests(ApiTestCase):
    def test_customer_not_privileged(self):
        self.assertFalse(is_privileged(self.make_user("buyer")))

    def test_non_admin_role_not_privileged(self):
        support = self.make_user("supporter")
        support.is_staff = True
        support.save()
        support.groups.add(Group.objects.get_or_create(name=ROLE_SUPPORT)[0])
        self.assertFalse(is_privileged(support))

    def test_admin_role_privileged(self):
        self.assertTrue(is_privileged(make_privileged("boss")))

    def test_superuser_without_role_group_privileged(self):
        root = make_privileged("root", is_superuser=True)
        self.assertEqual(root.groups.count(), 0)
        self.assertTrue(is_privileged(root))

    def test_anonymous_not_privileged(self):
        self.assertFalse(is_privileged(None))


class MFAEnrollmentTests(ApiTestCase):
    def setUp(self):
        self.boss = make_privileged("boss")

    def bootstrap_setup(self, username="boss", **extra):
        payload = {"username": username, "password": PASSWORD}
        payload.update(extra)
        return self.client.post("/api/accounts/mfa/setup/", payload, format="json")

    def bootstrap_confirm(self, secret, counter, username="boss"):
        return self.client.post(
            "/api/accounts/mfa/confirm/",
            {
                "username": username,
                "password": PASSWORD,
                "code": totp.hotp(secret, counter),
            },
            format="json",
        )

    # -- permission surface -------------------------------------------------

    def test_anonymous_gets_uniform_denials(self):
        # JWT-only surfaces: uniform 403, no auth dance offered.
        self.assertEqual(self.client.get("/api/accounts/mfa/status/").status_code, 403)
        res = self.client.post("/api/accounts/mfa/disable/", {}, format="json")
        self.assertEqual(res.status_code, 403)
        # Bootstrap surfaces: a uniform 401 for unknown credentials, with
        # no signal about which half was wrong — or about the account.
        res = self.client.post("/api/accounts/mfa/setup/", {}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["error"], "Invalid credentials.")
        res = self.client.post("/api/accounts/mfa/confirm/", {}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["error"], "Invalid credentials.")

    def test_customer_cannot_enroll_or_read_status(self):
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        self.auth(token)
        for method, path in (
            ("get", "/api/accounts/mfa/status/"),
            ("post", "/api/accounts/mfa/setup/"),
            ("post", "/api/accounts/mfa/confirm/"),
            ("post", "/api/accounts/mfa/disable/"),
        ):
            res = getattr(self.client, method)(path, {}, format="json")
            self.assertEqual(res.status_code, 403, path)

    def test_bootstrap_rejects_non_privileged_credentials(self):
        self.make_user("buyer")
        self.assertEqual(self.bootstrap_setup(username="buyer").status_code, 403)

    def test_bootstrap_rejects_bad_credentials_uniformly(self):
        res = self.bootstrap_setup(password="Wr0ng-Passphrase!")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["error"], "Invalid credentials.")

    # -- the bootstrap rollout path -----------------------------------------

    def test_bootstrap_enroll_confirm_then_login(self):
        res = self.bootstrap_setup()
        self.assertEqual(res.status_code, 200)
        secret = res.data["secret"]
        self.assertTrue(res.data["otpauth_uri"].startswith("otpauth://totp/"))
        device = TOTPDevice.objects.get(user=self.boss)
        self.assertFalse(device.enabled)
        self.assertIsNone(device.confirmed_at)

        res = self.bootstrap_confirm(secret, totp.now() // totp.STEP)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["enabled"])
        device.refresh_from_db()
        self.assertTrue(device.enabled)
        self.assertIsNotNone(device.confirmed_at)

        # The whole point: the factor now gates login, and a valid code passes.
        res = self.client.post(
            "/api/accounts/login/",
            login_payload("boss", totp=code_after(device)),
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)

    def test_bootstrap_closes_once_enrolled(self):
        secret = self.bootstrap_setup().data["secret"]
        self.bootstrap_confirm(secret, totp.now() // totp.STEP)
        res = self.bootstrap_setup()
        self.assertEqual(res.status_code, 403)
        self.assertIn("already enrolled", res.data["error"])

    # -- steady-state JWT path ----------------------------------------------

    def jwt_login(self, device):
        return self.client.post(
            "/api/accounts/login/",
            login_payload(self.boss.username, totp=code_after(device)),
            format="json",
        )

    def test_setup_requires_code_reenroll_guard(self):
        enroll_via_model(self.boss)
        _, token = self.api_login("boss")
        self.auth(token)
        device = TOTPDevice.objects.get(user=self.boss)
        res = self.client.post("/api/accounts/mfa/setup/", {}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("code", res.data["details"])
        res = self.client.post(
            "/api/accounts/mfa/setup/", {"code": "000000"}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid or expired", res.data["details"]["code"][0])
        device.refresh_from_db()
        self.assertTrue(device.enabled)  # refused setup touched nothing

    def test_setup_reenroll_rotates_secret(self):
        device = enroll_via_model(self.boss)
        _, token = self.api_login("boss")
        self.auth(token)
        res = self.client.post(
            "/api/accounts/mfa/setup/", {"code": code_after(device)}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        new_secret = res.data["secret"]
        self.assertNotEqual(new_secret, device.secret)
        device.refresh_from_db()
        self.assertEqual(device.secret, new_secret)
        self.assertFalse(device.enabled)  # pending again until confirmed
        self.assertIsNone(device.confirmed_at)

    def test_setup_while_pending_needs_no_code(self):
        # Once a rotation is pending (device disabled), a JWT holder who
        # already proved the factor for this session may re-mint freely;
        # the code guard exists to protect an ACTIVE device.
        enroll_via_model(self.boss)
        _, token = self.api_login("boss")
        self.auth(token)
        device = TOTPDevice.objects.get(user=self.boss)
        first = self.client.post(
            "/api/accounts/mfa/setup/", {"code": code_after(device)}, format="json"
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post("/api/accounts/mfa/setup/", {}, format="json")
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(second.data["secret"], first.data["secret"])

    def test_confirm_requires_pending_device(self):
        enroll_via_model(self.boss)
        _, token = self.api_login("boss")
        self.auth(token)
        res = self.client.post(
            "/api/accounts/mfa/confirm/", {"code": "000000"}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("No pending", res.data["details"]["code"][0])

    def test_confirm_rejects_wrong_code(self):
        secret = self.bootstrap_setup().data["secret"]
        res = self.bootstrap_confirm(secret, totp.now() // totp.STEP + 10)
        self.assertEqual(res.status_code, 400)

    def test_confirm_rejects_without_setup(self):
        make_privileged("stranger")
        res = self.client.post(
            "/api/accounts/mfa/confirm/",
            {
                "username": "stranger",
                "password": PASSWORD,
                "code": "000000",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("No pending", res.data["details"]["code"][0])

    def test_status_never_returns_secret(self):
        secret = self.bootstrap_setup().data["secret"]
        self.bootstrap_confirm(secret, totp.now() // totp.STEP)
        _, token = self.api_login("boss")
        self.auth(token)
        res = self.client.get("/api/accounts/mfa/status/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"enabled": True})

    def test_device_str_names_the_user_without_secret(self):
        device = enroll_via_model(self.boss)
        self.assertEqual(str(device), f"TOTP device for {self.boss}")
        self.assertNotIn(device.secret, str(device))

    def test_secret_shown_exactly_once(self):
        res = self.bootstrap_setup()
        secret = res.data["secret"]
        device = TOTPDevice.objects.get(user=self.boss)
        self.assertEqual(device.secret, secret)
        self.bootstrap_confirm(secret, totp.now() // totp.STEP)
        _, token = self.api_login("boss")
        self.auth(token)
        for later in (
            self.client.get("/api/accounts/mfa/status/"),
            self.client.post("/api/accounts/mfa/setup/", {}, format="json"),
            self.client.post("/api/accounts/mfa/disable/", {}, format="json"),
        ):
            self.assertNotIn(secret, str(later.data))

    # -- disable --------------------------------------------------------------

    def test_disable_requires_code_then_mandatory_blocks_login(self):
        enroll_via_model(self.boss)
        _, token = self.api_login("boss")
        self.auth(token)
        device = TOTPDevice.objects.get(user=self.boss)
        res = self.client.post(
            "/api/accounts/mfa/disable/", {"code": "000000"}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        res = self.client.post(
            "/api/accounts/mfa/disable/", {"code": code_after(device)}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["enabled"])
        device.refresh_from_db()
        self.assertFalse(device.enabled)
        # Mandatory means mandatory: with the factor stripped, the next
        # password-only login is refused — and the credential bootstrap
        # reopens so the user can re-enroll (recovery without ops help).
        self.auth(None)
        res = self.client.post(
            "/api/accounts/login/", login_payload("boss"), format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Multi-factor", res.data["details"]["totp"][0])
        res = self.bootstrap_setup()
        self.assertEqual(res.status_code, 200)

    def test_disable_without_device_rejected(self):
        _, token = self.api_login("boss")  # api_login enrolls the fixture device
        self.auth(token)
        TOTPDevice.objects.all().delete()
        res = self.client.post(
            "/api/accounts/mfa/disable/", {"code": "000000"}, format="json"
        )
        self.assertEqual(res.status_code, 400)


class MFAEnrollmentReachTests(ApiTestCase):
    """SPEC-20-13: a blocked privileged account can actually reach enrollment.

    Mandatory MFA (R-17.9) refuses an unenrolled privileged login, so the
    enrollment API was reachable only by hand-typed POSTs — an account with
    no device was locked out of /admin/ and the API with no path back in.
    These pins cover both halves of the reach: the admin login page offers
    the enrollment surface (always, and loudly when MFA is the reason the
    attempt failed), and the credential-bootstrap flow the enrollment page
    drives mints a working device without ever holding a JWT.
    """

    def setUp(self):
        self.boss = make_privileged("boss")

    def admin_login_page(self):
        return self.client.get("/admin/login/")

    def test_admin_login_page_always_offers_enrollment(self):
        res = self.admin_login_page()
        self.assertContains(res, settings.MFA_ENROLL_URL)
        self.assertContains(res, "Enroll your device")

    def test_enrollment_url_is_configuration_not_a_constant(self):
        with override_settings(
            MFA_ENROLL_URL="https://elsewhere.example.com/enroll"
        ):
            self.assertContains(
                self.admin_login_page(), "https://elsewhere.example.com/enroll"
            )

    def test_blocked_privileged_login_offers_the_enrollment_notice(self):
        res = self.client.post(
            "/admin/login/",
            {"username": "boss", "password": PASSWORD, "next": "/admin/"},
            follow=True,
        )
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(res, "Multi-factor authentication is mandatory")
        self.assertContains(res, "Set up multi-factor authentication")
        self.assertContains(res, settings.MFA_ENROLL_URL)

    def test_password_failure_shows_the_link_but_not_the_mfa_notice(self):
        # The notice is keyed off the MFA block specifically: telling a
        # caller who simply mistyped their password to go and enroll would
        # be a confusing (and leaky) answer to an ordinary failure.
        res = self.client.post(
            "/admin/login/",
            {"username": "boss", "password": "Wr0ng-Passphrase!"},
            follow=True,
        )
        self.assertContains(res, "Please enter the correct username and password")
        self.assertNotContains(res, "Set up multi-factor authentication")
        self.assertContains(res, settings.MFA_ENROLL_URL)

    def test_signed_in_admin_visit_is_redirected_not_rendered(self):
        # config.admin.MFAAdminSite.login flags the enrollment block on the
        # unrendered login page; an already-authenticated visit is a redirect
        # that has no form at all.
        self.client.force_login(User.objects.create_superuser(
            username="root", email="root@example.com", password=PASSWORD
        ))
        res = self.admin_login_page()
        self.assertEqual(res.status_code, 302)

    def test_login_page_never_renders_a_secret(self):
        enroll_via_model(self.boss)
        res = self.admin_login_page()
        self.assertNotContains(res, TEST_TOTP_SECRET)

    def test_enrollment_reach_needs_no_jwt_then_login_works(self):
        """The whole reach, exactly as the enrollment page drives it:
        password-proven setup -> secret + provisioning URI -> confirm ->
        the factor now gates (and admits) login."""
        res = self.client.post(
            "/api/accounts/mfa/setup/",
            {"username": "boss", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        secret = res.data["secret"]
        self.assertTrue(res.data["otpauth_uri"].startswith("otpauth://totp/"))
        self.assertIn(secret, res.data["otpauth_uri"])

        device = TOTPDevice.objects.get(user=self.boss)
        self.assertFalse(device.enabled)
        res = self.client.post(
            "/api/accounts/mfa/confirm/",
            {
                "username": "boss",
                "password": PASSWORD,
                "code": totp.hotp(secret, totp.now() // totp.STEP),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        res = self.client.post(
            "/api/accounts/login/",
            login_payload("boss", totp=code_after(device)),
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)


class MfaQrTests(SimpleTestCase):
    """SPEC-20-9: the enrollment QR is a real, scannable QR of the URI.

    What is pinned (no decoder is available, and inventing one here would
    test segno, not this repo): the artifact is an inline SVG data URI, its
    geometry is the QR matrix sized for THIS payload at the declared scale
    and quiet zone, it is byte-identical to an independent render of the
    provisioning URI the same response returns (so the view cannot have
    encoded some other string), and it changes when the payload changes —
    together that is a picture a scanner decodes to the provisioning URI.
    """

    def test_artifact_is_an_inline_svg_of_the_right_geometry(self):
        uri = totp.otpauth_uri(TEST_TOTP_SECRET, "boss")
        artifact = qr.qr_data_uri(uri)
        self.assertTrue(
            artifact.startswith(qr.SVG_DATA_URI_PREFIX)
            and artifact.startswith("data:image/svg+xml;base64,")
        )
        svg = base64.b64decode(artifact[len("data:image/svg+xml;base64,"):])
        root = ElementTree.fromstring(svg)
        self.assertTrue(root.tag.endswith("svg"))
        modules = len(segno.make(uri, error=qr.ERROR_CORRECTION).matrix) + (
            2 * qr.SVG_BORDER
        )
        self.assertEqual(root.get("width"), f"{modules * qr.SVG_SCALE}")
        self.assertEqual(root.get("height"), f"{modules * qr.SVG_SCALE}")

    def test_artifact_is_bound_to_the_exact_payload(self):
        uri = totp.otpauth_uri(TEST_TOTP_SECRET, "boss")
        # One character of secret apart: a different enrollment must never
        # render the same picture, or two users could scan each other's code.
        other = totp.otpauth_uri(TEST_TOTP_SECRET[:-1] + "A", "boss")
        self.assertNotEqual(qr.qr_data_uri(uri), qr.qr_data_uri(other))

    def test_secret_is_encoded_not_written_into_the_markup(self):
        # The payload travels as QR modules; the SVG itself carries no
        # plaintext secret to leak into a DOM, a log or a screenshot.
        artifact = qr.qr_data_uri(totp.otpauth_uri(TEST_TOTP_SECRET, "boss"))
        self.assertNotIn(
            TEST_TOTP_SECRET,
            base64.b64decode(artifact[len("data:image/svg+xml;base64,"):]).decode(),
        )


class MFAEnrollmentQrTests(ApiTestCase):
    """SPEC-20-9: setup hands the enrollment page a scannable code."""

    def setUp(self):
        self.boss = make_privileged("boss")

    def bootstrap_setup(self):
        return self.client.post(
            "/api/accounts/mfa/setup/",
            {"username": "boss", "password": PASSWORD},
            format="json",
        )

    def test_setup_returns_the_qr_of_its_own_provisioning_uri(self):
        res = self.bootstrap_setup()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data["qr_data_uri"], qr.qr_data_uri(res.data["otpauth_uri"])
        )

    def test_bootstrap_rotation_returns_a_new_qr(self):
        # Re-minting mints a new secret, so the picture must change with it:
        # a stale QR would enroll the previous secret.
        first = self.bootstrap_setup().data
        self.client.post(
            "/api/accounts/mfa/confirm/",
            {
                "username": "boss",
                "password": PASSWORD,
                "code": totp.hotp(first["secret"], totp.now() // totp.STEP),
            },
            format="json",
        )
        second = self.bootstrap_setup()
        self.assertEqual(second.status_code, 403)  # the bootstrap window closed
        _, token = self.api_login("boss")
        self.auth(token)
        device = TOTPDevice.objects.get(user=self.boss)
        res = self.client.post(
            "/api/accounts/mfa/setup/",
            {"code": code_after(device)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotEqual(res.data["secret"], first["secret"])
        self.assertNotEqual(res.data["qr_data_uri"], first["qr_data_uri"])

    def test_qr_is_shown_exactly_once_like_the_secret(self):
        res = self.bootstrap_setup()
        secret = res.data["secret"]
        artifact = res.data["qr_data_uri"]
        self.client.post(
            "/api/accounts/mfa/confirm/",
            {
                "username": "boss",
                "password": PASSWORD,
                "code": totp.hotp(secret, totp.now() // totp.STEP),
            },
            format="json",
        )
        _, token = self.api_login("boss")
        self.auth(token)
        for later in (
            self.client.get("/api/accounts/mfa/status/"),
            self.client.post("/api/accounts/mfa/disable/", {}, format="json"),
            self.client.get("/admin/login/"),
        ):
            self.assertNotIn(artifact, str(getattr(later, "data", "") or later))
            self.assertNotIn(secret, str(getattr(later, "data", "") or later))


class MFALoginEnforcementTests(ApiTestCase):
    def setUp(self):
        self.boss = make_privileged("boss")
        enroll_via_model(self.boss)

    def valid_code(self, counter=None):
        counter = totp.now() // totp.STEP if counter is None else counter
        return totp.hotp(TEST_TOTP_SECRET, counter)

    def test_unenrolled_privileged_blocked_at_login(self):
        make_privileged("newbie")
        res = self.client.post(
            "/api/accounts/login/", login_payload("newbie"), format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Multi-factor", res.data["details"]["totp"][0])
        self.assertNotIn("access", res.data)

    def test_missing_code_rejected_and_audited(self):
        res = self.client.post(
            "/api/accounts/login/", login_payload("boss"), format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("authentication code", res.data["details"]["totp"][0])
        self.assertNotIn("access", res.data)
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.AUTH_LOGIN_FAILED, actor=self.boss
            ).exists()
        )

    def test_valid_code_grants_tokens(self):
        res = self.client.post(
            "/api/accounts/login/",
            login_payload("boss", totp=self.valid_code()),
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)

    def test_invalid_code_rejected(self):
        wrong = totp.hotp(TEST_TOTP_SECRET, totp.now() // totp.STEP + 3)
        res = self.client.post(
            "/api/accounts/login/", login_payload("boss", totp=wrong), format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid or expired", res.data["details"]["totp"][0])

    def test_expired_code_outside_window_rejected(self):
        stale_counter = totp.now() // totp.STEP - 5
        res = self.client.post(
            "/api/accounts/login/",
            login_payload("boss", totp=self.valid_code(stale_counter)),
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_same_code_cannot_log_in_twice(self):
        code = self.valid_code()
        first = self.client.post(
            "/api/accounts/login/", login_payload("boss", totp=code), format="json"
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            "/api/accounts/login/", login_payload("boss", totp=code), format="json"
        )
        self.assertEqual(second.status_code, 400)

    def test_watermark_persisted_after_login(self):
        counter = totp.now() // totp.STEP
        self.client.post(
            "/api/accounts/login/",
            login_payload("boss", totp=self.valid_code(counter)),
            format="json",
        )
        device = TOTPDevice.objects.get(user=self.boss)
        self.assertEqual(device.last_used_counter, counter)

    def test_wrong_password_still_uniform_401(self):
        res = self.client.post(
            "/api/accounts/login/",
            login_payload("boss", password="Wr0ng-Passphrase!", totp=self.valid_code()),
            format="json",
        )
        self.assertEqual(res.status_code, 401)

    def test_customer_login_unaffected(self):
        self.make_user("buyer")
        res = self.client.post(
            "/api/accounts/login/", login_payload("buyer"), format="json"
        )
        self.assertEqual(res.status_code, 200)

    def test_customer_door_has_no_totp_field(self):
        # SPEC-20-10 rewrite of the old `test_customer_totp_field_ignored`
        # pin. That pin asserted the superseded contract — "the storefront
        # login SERVES a totp field and ignores its value" — which is the
        # bug: a customer was shown, and could post, an authentication code
        # no server check ever read. The customer door now declares the
        # parent's username/password and nothing else, so `totp` is not a
        # field in either direction (it cannot be rendered, and a body that
        # includes it is ignored instead of half-honoured).
        self.assertEqual(
            sorted(StorefrontTokenObtainPairSerializer().fields),
            ["password", "username"],
        )

    def test_non_admin_staff_login_unaffected(self):
        support = self.make_user("helper")
        support.is_staff = True
        support.save()
        support.groups.add(Group.objects.get_or_create(name=ROLE_SUPPORT)[0])
        res = self.client.post(
            "/api/accounts/login/", login_payload("helper"), format="json"
        )
        self.assertEqual(res.status_code, 200)

    def test_superuser_requires_factor_too(self):
        root = make_privileged("root", is_superuser=True)
        res = self.client.post(
            "/api/accounts/login/", login_payload("root"), format="json"
        )
        self.assertEqual(res.status_code, 400)  # unenrolled superuser blocked
        enroll_via_model(root)
        res = self.client.post(
            "/api/accounts/login/",
            login_payload("root", totp=self.valid_code()),
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)


class MfaTrustDeviceTests(ApiTestCase):
    """SPEC-20-8: "trust this device for 30 days" - opt-in, per browser.

    A fresh code on every privileged login is a burden the user directive
    rejects, but R-17.9 still stands for everyone who does not opt in. These
    pins cover both halves of that promise, in the order the feature makes
    them:

    - the opt-in is explicit and hard to reach by accident: it needs a
      privileged session, a FRESH code and an enabled factor, and nothing
      else in the system (no login, no enrollment, no default) can set it;
    - what it buys is narrow: this browser, this device row, this user, until
      the TTL lapses. Every other login is challenged exactly as before;
    - what it never buys: a way around the factor. Trust cannot spend a
      one-time code, cannot resurrect one already spent, and cannot outlive
      the TTL (nor survive a disable or a secret rotation);
    - non-privileged logins and the customer door are untouched throughout.
    """

    TRUST_PATH = "/api/accounts/mfa/trust/"
    LOGIN_PATH = "/api/accounts/login/"

    def setUp(self):
        self.boss = make_privileged("boss")
        self.device = enroll_via_model(self.boss)

    def grant(self):
        """The opt-in exactly as a staff UI drives it: a privileged session
        (which itself demanded the factor) plus a fresh code here.

        The watermark rewind is the same accommodation ``api_login``
        documents: it leaves the device unable to spend this step's code, so
        without the rewind the trust call could only burn the NEXT step's and
        nothing could authenticate until the clock moved on. No sleeps, no
        clock patching.
        """
        self.api_login("boss")
        device = TOTPDevice.objects.get(user=self.boss)
        counter = totp.now() // totp.STEP
        device.last_used_counter = counter - 1
        device.save(update_fields=["last_used_counter"])
        res = self.client.post(
            self.TRUST_PATH,
            {"code": totp.hotp(device.secret, counter)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res

    def login(self, client=None, **extra):
        return (client or self.client).post(
            self.LOGIN_PATH, login_payload("boss", **extra), format="json"
        )

    def challenge(self, res):
        """Assert the R-17.9 challenge came back (and no tokens rode with it)."""
        self.assertEqual(res.status_code, 400, res.data)
        self.assertNotIn("access", res.data)
        self.assertIn("totp", res.data["details"])
        return res.data["details"]["totp"][0]

    # -- 1. the promise: no fresh code on the trusted device ---------------

    def test_trusted_device_logs_in_without_a_fresh_code(self):
        self.grant()
        res = self.login()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)

    def test_another_browser_is_still_challenged(self):
        # The grant belongs to the browser that asked for it, not to the
        # account: a password reused elsewhere still earns the code.
        self.grant()
        message = self.challenge(self.login(client=self.fresh_client()))
        self.assertIn("authentication code", message)

    def test_a_trusted_login_spends_no_code_and_ignores_a_posted_one(self):
        # On a trusted device the client is never asked for a code, so a
        # posted value is not a factor: it is neither verified nor consumed,
        # and it can neither move the watermark nor block the login.
        self.grant()
        device = TOTPDevice.objects.get(user=self.boss)
        watermark = device.last_used_counter
        res = self.login(totp="000000")
        self.assertEqual(res.status_code, 200, res.data)
        device.refresh_from_db()
        self.assertEqual(device.last_used_counter, watermark)

    # -- 2. nothing grants trust implicitly --------------------------------

    def test_an_untrusted_device_is_challenged(self):
        message = self.challenge(self.login())
        self.assertIn("authentication code", message)

    def test_a_login_with_a_valid_code_still_grants_nothing(self):
        code = totp.hotp(self.device.secret, totp.now() // totp.STEP)
        res = self.login(totp=code)
        self.assertEqual(res.status_code, 200, res.data)
        self.device.refresh_from_db()
        self.assertIsNone(self.device.trusted_until)
        self.assertNotIn(settings.MFA_TRUST_COOKIE_NAME, res.cookies)
        # ...and the NEXT login, from any browser, is still challenged.
        self.challenge(self.login(client=self.fresh_client()))

    def test_enrollment_never_grants_trust(self):
        # Neither bootstrap step of a fresh enrollment writes the TTL: a new
        # device starts untrusted, so enrollment is not a trust side effect.
        newbie = make_privileged("newbie")
        res = self.client.post(
            "/api/accounts/mfa/setup/",
            {"username": "newbie", "password": PASSWORD},
            format="json",
        )
        self.client.post(
            "/api/accounts/mfa/confirm/",
            {
                "username": "newbie",
                "password": PASSWORD,
                "code": totp.hotp(res.data["secret"], totp.now() // totp.STEP),
            },
            format="json",
        )
        device = TOTPDevice.objects.get(user=newbie)
        self.assertIsNone(device.trusted_until)
        res = self.client.post(
            self.LOGIN_PATH, login_payload("newbie"), format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("authentication code", res.data["details"]["totp"][0])

    def test_trust_requires_privilege_a_session_and_a_fresh_code(self):
        # Anonymous gets the same JWT-only 403 as status/disable, and a
        # customer session is refused outright.
        self.assertEqual(
            self.client.post(self.TRUST_PATH, {}, format="json").status_code, 403
        )
        self.make_user("buyer")
        _, token = self.api_login("buyer")
        self.auth(token)
        self.assertEqual(
            self.client.post(self.TRUST_PATH, {}, format="json").status_code, 403
        )
        self.auth(None)
        # A privileged session is still not enough: the second factor must be
        # re-proved here, so a stolen session cannot convert itself into
        # thirty days of challenge-free logins.
        self.api_login("boss")
        for payload in ({}, {"code": "000000"}):
            res = self.client.post(self.TRUST_PATH, payload, format="json")
            self.assertEqual(res.status_code, 400, payload)
            self.assertIn("Invalid or expired", res.data["details"]["code"][0])
        self.device.refresh_from_db()
        self.assertIsNone(self.device.trusted_until)

    def test_trust_needs_an_enrolled_factor(self):
        self.api_login("boss")
        TOTPDevice.objects.filter(user=self.boss).update(
            enabled=False, confirmed_at=None
        )
        res = self.client.post(self.TRUST_PATH, {"code": "000000"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("not enabled", res.data["details"]["code"][0])

    # -- 3. the marker must genuinely identify this device -----------------

    def test_a_forged_marker_is_refused(self):
        self.grant()
        real = self.client.cookies[settings.MFA_TRUST_COOKIE_NAME].value
        self.client.cookies[settings.MFA_TRUST_COOKIE_NAME] = real[:-1] + (
            "A" if real[-1] != "A" else "B"
        )
        self.challenge(self.login())

    def test_another_users_marker_does_not_carry_over(self):
        # Shared/kiosk browser: the marker names (user, device), so one
        # user's grant can never answer a different user's challenge. BOTH
        # devices are genuinely trusted, so the (user, device) pair is the
        # only thing left that can refuse.
        chief = make_privileged("chief")
        chief_device = enroll_via_model(chief)
        chief_device.trusted_until = mfa_trust.trust_deadline()
        chief_device.save(update_fields=["trusted_until"])
        self.device.trusted_until = mfa_trust.trust_deadline()
        self.device.save(update_fields=["trusted_until"])
        self.client.cookies[settings.MFA_TRUST_COOKIE_NAME] = signing.dumps(
            {"user": chief.pk, "device": chief_device.pk}, salt=mfa_trust.TRUST_SALT
        )
        self.challenge(self.login())

    def test_a_marker_for_another_device_row_is_refused(self):
        self.grant()
        self.client.cookies[settings.MFA_TRUST_COOKIE_NAME] = signing.dumps(
            {"user": self.boss.pk, "device": self.device.pk + 999},
            salt=mfa_trust.TRUST_SALT,
        )
        self.challenge(self.login())

    def test_a_marker_signed_before_the_ttl_is_refused(self):
        # Belt to the DB deadline's braces: the signed marker cannot outlive
        # MFA_TRUST_DAYS even while trusted_until is still in the future.
        self.grant()
        stale = totp.now() - (settings.MFA_TRUST_DAYS + 1) * mfa_trust.SECONDS_PER_DAY
        with mock.patch("django.core.signing.time.time", return_value=stale):
            cookie = signing.dumps(
                {"user": self.boss.pk, "device": self.device.pk},
                salt=mfa_trust.TRUST_SALT,
            )
        self.client.cookies[settings.MFA_TRUST_COOKIE_NAME] = cookie
        with mock.patch("django.core.signing.time.time", return_value=totp.now()):
            self.challenge(self.login())

    def test_the_marker_cookie_is_locked_down(self):
        res = self.grant()
        cookie = res.cookies[settings.MFA_TRUST_COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], settings.MFA_TRUST_COOKIE_SAMESITE)
        budget = settings.MFA_TRUST_DAYS * mfa_trust.SECONDS_PER_DAY
        self.assertGreater(cookie["max-age"], budget - 60)
        self.assertLessEqual(cookie["max-age"], budget)

    # -- 4. the TTL ---------------------------------------------------------

    def test_the_grant_lasts_the_env_driven_ttl(self):
        with override_settings(MFA_TRUST_DAYS=7):
            res = self.grant()
        self.assertEqual(res.data["trust_days"], 7)
        self.device.refresh_from_db()
        remaining = self.device.trusted_until - timezone.now()
        self.assertGreater(remaining, timedelta(days=6, hours=23))
        self.assertLess(remaining, timedelta(days=7, minutes=1))
        self.assertEqual(self.login().status_code, 200)

    def test_zero_days_is_the_kill_switch(self):
        # The documented emergency off-switch: the opt-in still resolves, but
        # the grant is already over, so the challenge is straight back.
        with override_settings(MFA_TRUST_DAYS=0):
            res = self.grant()
        self.assertEqual(res.data["trust_days"], 0)
        self.device.refresh_from_db()
        self.assertLessEqual(self.device.trusted_until, timezone.now())
        message = self.challenge(self.login())
        self.assertIn("authentication code", message)

    def test_an_expired_grant_returns_the_challenge(self):
        self.grant()
        self.device.trusted_until = timezone.now() - timedelta(seconds=1)
        self.device.save(update_fields=["trusted_until"])
        message = self.challenge(self.login())
        self.assertIn("authentication code", message)

    def test_an_expired_grant_does_not_resurrect_a_spent_code(self):
        # The replay guard across the trust boundary: the code the trust call
        # spent stays spent once the challenge returns, and the next unused
        # code is the one that works.
        self.grant()
        device = TOTPDevice.objects.get(user=self.boss)
        spent = totp.hotp(device.secret, device.last_used_counter)
        self.assertEqual(self.login().status_code, 200)
        self.device.trusted_until = timezone.now() - timedelta(seconds=1)
        self.device.save(update_fields=["trusted_until"])
        message = self.challenge(self.login(totp=spent))
        self.assertIn("Invalid or expired", message)
        self.assertEqual(self.login(totp=code_after(device)).status_code, 200)

    # -- 5. losing the device or the factor drops the trust ----------------

    def test_disabling_mfa_drops_the_trust(self):
        self.grant()
        device = TOTPDevice.objects.get(user=self.boss)
        res = self.client.post(
            "/api/accounts/mfa/disable/",
            {"code": code_after(device)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["enabled"])
        self.device.refresh_from_db()
        self.assertFalse(self.device.enabled)
        self.assertIsNone(self.device.trusted_until)
        # The stale marker cannot re-open anything: with the factor off the
        # login demands enrollment again rather than answering the challenge.
        self.auth(None)
        self.assertIn("Multi-factor", self.challenge(self.login()))

    def test_re_enrolling_drops_the_trust(self):
        # A rotation is the lost-device path, so trust collected by the old
        # secret must not ride along with the new one, and the leftover marker
        # must not admit the freshly confirmed factor either.
        self.grant()
        device = TOTPDevice.objects.get(user=self.boss)
        res = self.client.post(
            "/api/accounts/mfa/setup/", {"code": code_after(device)}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn(settings.MFA_TRUST_COOKIE_NAME, res.cookies)
        self.device.refresh_from_db()
        self.assertIsNone(self.device.trusted_until)
        res = self.client.post(
            "/api/accounts/mfa/confirm/",
            {"code": totp.hotp(res.data["secret"], totp.now() // totp.STEP)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.auth(None)
        self.assertIn("authentication code", self.challenge(self.login()))

    # -- 6. nobody else is affected ----------------------------------------

    def test_customers_are_untouched_by_trust(self):
        self.make_user("buyer")
        res = self.client.post(self.LOGIN_PATH, login_payload("buyer"), format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn(settings.MFA_TRUST_COOKIE_NAME, res.cookies)
        self.assertFalse(
            TOTPDevice.objects.filter(trusted_until__isnull=False).exists()
        )


class StorefrontLoginSplitTests(ApiTestCase):
    """SPEC-20-10: the customer door never speaks TOTP; the staff door does.

    One `LoginView` used to serve both populations, which forced the TOTP
    field into every customer's validation surface. The split gives
    customers their own door and keeps the privileged enforcement on
    `login/` — and, critically, makes the customer door REFUSE a
    privileged account, so it cannot become a way around [R-17.9].
    """

    CUSTOMER_DOOR = "/api/accounts/storefront/login/"
    STAFF_DOOR = "/api/accounts/login/"

    def setUp(self):
        self.make_user("buyer")

    def test_customer_door_authenticates_with_credentials_alone(self):
        res = self.client.post(
            self.CUSTOMER_DOOR, login_payload("buyer"), format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)

    def test_customer_door_treats_a_posted_totp_as_nothing(self):
        # Not an auth factor and not a device: the code is a non-field, so
        # it reaches no verification and mints no credential of its own.
        res = self.client.post(
            self.CUSTOMER_DOOR,
            login_payload("buyer", totp="000000"),
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(TOTPDevice.objects.exists())

    def test_customer_door_keeps_the_refresh_cookie_contract(self):
        # [R-17.12] unchanged on the new door: the refresh token rides the
        # HttpOnly cookie, never the response body.
        res = self.client.post(
            self.CUSTOMER_DOOR, login_payload("buyer"), format="json"
        )
        self.assertNotIn("refresh", res.data)
        self.assertIn(settings.JWT_REFRESH_COOKIE_NAME, res.cookies)

    def test_customer_door_wrong_password_is_still_uniform(self):
        res = self.client.post(
            self.CUSTOMER_DOOR,
            login_payload("buyer", password="Wr0ng-Passphrase!"),
            format="json",
        )
        self.assertEqual(res.status_code, 401)

    def test_customer_door_refuses_a_privileged_account_and_audits_it(self):
        boss = make_privileged("boss")
        enroll_via_model(boss)
        res = self.client.post(
            self.CUSTOMER_DOOR, login_payload("boss"), format="json"
        )
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("access", res.data)
        self.assertIn("staff sign-in page", res.data["error"])
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.AUTH_LOGIN_FAILED, actor=boss
            ).exists()
        )

    def test_customer_door_refuses_an_unenrolled_privileged_account(self):
        # Mandatory means mandatory: with no device to satisfy, the door
        # without a code field still must not authenticate the account.
        make_privileged("newbie")
        res = self.client.post(
            self.CUSTOMER_DOOR, login_payload("newbie"), format="json"
        )
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("access", res.data)

    def test_customer_door_serves_non_privileged_staff_unchanged(self):
        # R-17.9 binds privileged roles only, and the customer door refuses
        # exactly those: a support-role staffer (staff, no staff.manage) is
        # an ordinary caller here, as it always was on the shared door.
        helper = self.make_user("helper")
        helper.is_staff = True
        helper.save()
        helper.groups.add(Group.objects.get_or_create(name=ROLE_SUPPORT)[0])
        res = self.client.post(
            self.CUSTOMER_DOOR, login_payload("helper"), format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)

    def test_staff_door_still_ignores_a_customers_totp(self):
        # The staff door keeps its own contract for non-privileged callers:
        # the field exists for privileged logins and is inert for everyone
        # else (the pre-split pin, preserved on the door it described).
        res = self.client.post(
            self.STAFF_DOOR, login_payload("buyer", totp="000000"), format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(TOTPDevice.objects.exists())

    def test_staff_door_still_demands_and_spends_the_factor(self):
        boss = make_privileged("boss")
        device = enroll_via_model(boss)
        res = self.client.post(self.STAFF_DOOR, login_payload("boss"), format="json")
        self.assertEqual(res.status_code, 400)  # no code
        self.assertNotIn("access", res.data)
        res = self.client.post(
            self.STAFF_DOOR, login_payload("boss", totp="000000"), format="json"
        )
        self.assertEqual(res.status_code, 400)  # wrong code
        code = totp.hotp(device.secret, totp.now() // totp.STEP)
        res = self.client.post(
            self.STAFF_DOOR, login_payload("boss", totp=code), format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        # The replay guard rides on the door that consumed the code: the same
        # one cannot be spent twice (RFC 6238 §5.2).
        replay = self.fresh_client()
        res = replay.post(
            self.STAFF_DOOR, login_payload("boss", totp=code), format="json"
        )
        self.assertEqual(res.status_code, 400)


class AdminMFAEnforcementTests(ApiTestCase):
    """The second R-17.9 enforcement surface: the Django admin login form."""

    def setUp(self):
        self.boss = make_privileged("boss")
        enroll_via_model(self.boss)

    def valid_code(self, counter=None):
        counter = totp.now() // totp.STEP if counter is None else counter
        return totp.hotp(TEST_TOTP_SECRET, counter)

    def admin_login(self, username, client=None, **extra):
        payload = {"username": username, "password": PASSWORD}
        payload.update(extra)
        return (client or self.client).post("/admin/login/", payload, follow=True)

    def is_logged_in(self, client=None):
        return "_auth_user_id" in (client or self.client).session

    def test_code_field_rendered(self):
        res = self.client.get("/admin/login/")
        self.assertContains(res, "Authentication code")

    def test_privileged_without_code_refused(self):
        res = self.admin_login("boss")
        self.assertFalse(self.is_logged_in())
        self.assertContains(res, "Enter your authentication code.")

    def test_privileged_with_valid_code_logs_in(self):
        res = self.admin_login("boss", totp=self.valid_code())
        self.assertTrue(self.is_logged_in())

    def test_privileged_with_invalid_code_refused(self):
        res = self.admin_login("boss", totp="000000")
        self.assertFalse(self.is_logged_in())
        self.assertContains(res, "Invalid or expired authentication code.")

    def test_unenrolled_privileged_refused_with_enrollment_path(self):
        make_privileged("newbie")
        res = self.admin_login("newbie")
        self.assertFalse(self.is_logged_in())
        self.assertContains(res, "Multi-factor authentication is mandatory")

    def test_same_code_cannot_log_in_twice(self):
        code = self.valid_code()
        res = self.admin_login("boss", totp=code)
        self.assertTrue(self.is_logged_in())
        other = self.fresh_client()
        res = self.admin_login("boss", client=other, totp=code)
        self.assertFalse(self.is_logged_in(other))
        self.assertContains(res, "Invalid or expired authentication code.")

    def test_roleless_staff_unaffected(self):
        helper = self.make_user("helper")
        helper.is_staff = True
        helper.save()
        res = self.admin_login("helper")
        self.assertTrue(self.is_logged_in())

    def test_superuser_requires_code(self):
        root = make_privileged("root", is_superuser=True)
        enroll_via_model(root)
        res = self.admin_login("root")
        self.assertFalse(self.is_logged_in())
        res = self.admin_login("root", totp=self.valid_code())
        self.assertTrue(self.is_logged_in())


class AdminMfaTrustDeviceTests(ApiTestCase):
    """SPEC-20-8b: "trust this device" reaches the Django admin door too.

    SPEC-20-8 shipped the opt-in on the staff API serializer; ``/admin/`` is
    the OTHER privileged door and its form is a second enforcement surface.
    These pins hold the line that there is ONE rule, not two that drift:

    - a trusted, unexpired device with a valid marker logs into the admin
      with no fresh code (the user's directive, on the primary staff door);
    - every fail-safe still bites AT THE ADMIN: another browser, a lapsed
      grant, a forged or someone else's marker, and the ``MFA_TRUST_DAYS=0``
      kill switch all land back on the challenge;
    - the challenge itself is untouched — a wrong code is still
      MFA_CODE_INVALID, the replay watermark is never moved by a skipped or
      untrusted login, and nothing is downgraded to make a skip possible;
    - non-privileged staff (and customers) are exactly as they were.
    """

    TRUST_PATH = "/api/accounts/mfa/trust/"

    def setUp(self):
        self.boss = make_privileged("boss")
        self.device = enroll_via_model(self.boss)

    def grant(self):
        """Drive the real opt-in from a real privileged session, so the
        marker on the client is one ``bind_trust_cookie`` actually signed.
        """
        self.api_login("boss")
        device = TOTPDevice.objects.get(user=self.boss)
        counter = totp.now() // totp.STEP
        # Same accommodation MfaTrustDeviceTests.grant documents: the login
        # above spent this step's code, so the watermark is rewound for the
        # trust call rather than sleeping or patching the clock.
        device.last_used_counter = counter - 1
        device.save(update_fields=["last_used_counter"])
        res = self.client.post(
            self.TRUST_PATH,
            {"code": totp.hotp(device.secret, counter)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(settings.MFA_TRUST_COOKIE_NAME, res.cookies)
        return res

    def admin_login(self, username="boss", client=None, **extra):
        payload = {"username": username, "password": PASSWORD, "next": "/admin/"}
        payload.update(extra)
        return (client or self.client).post("/admin/login/", payload, follow=True)

    def is_logged_in(self, client=None):
        return "_auth_user_id" in (client or self.client).session

    def challenged(self, res, client=None):
        """Assert the admin refused the login and re-asked for the code."""
        self.assertFalse(self.is_logged_in(client))
        self.assertContains(res, "Enter your authentication code.")
        return res

    # -- 1. the promise, on the admin door ---------------------------------

    def test_trusted_device_reaches_the_admin_without_a_fresh_code(self):
        self.grant()
        res = self.admin_login()
        self.assertTrue(self.is_logged_in())
        # Past the login page (the admin chrome is rendering) and never
        # once asked for a code.
        self.assertContains(res, "/admin/logout/")
        self.assertNotContains(res, "Enter your authentication code.")

    def test_a_trusted_admin_login_spends_no_code_and_ignores_a_posted_one(self):
        # Nobody asked this browser for a code, so a posted value is not a
        # factor: never verified, never consumed, watermark unmoved.
        self.grant()
        watermark = TOTPDevice.objects.get(user=self.boss).last_used_counter
        self.admin_login(totp="000000")
        self.assertTrue(self.is_logged_in())
        device = TOTPDevice.objects.get(user=self.boss)
        self.assertEqual(device.last_used_counter, watermark)

    # -- 2. the fail-safes, at the admin door ------------------------------

    def test_another_browser_is_still_challenged_at_the_admin(self):
        self.grant()
        other = self.fresh_client()
        self.challenged(self.admin_login(client=other), other)

    def test_an_expired_grant_challenges_the_admin(self):
        self.grant()
        self.device.trusted_until = timezone.now() - timedelta(seconds=1)
        self.device.save(update_fields=["trusted_until"])
        self.challenged(self.admin_login())

    def test_a_forged_marker_cannot_open_the_admin(self):
        self.grant()
        real = self.client.cookies[settings.MFA_TRUST_COOKIE_NAME].value
        self.client.cookies[settings.MFA_TRUST_COOKIE_NAME] = real[:-1] + (
            "A" if real[-1] != "A" else "B"
        )
        self.challenged(self.admin_login())

    def test_another_users_marker_does_not_open_the_admin(self):
        # Shared/kiosk browser. Both grants are genuinely live, so the
        # (user, device) pair is the only thing left that can refuse.
        chief = make_privileged("chief")
        chief_device = enroll_via_model(chief)
        chief_device.trusted_until = mfa_trust.trust_deadline()
        chief_device.save(update_fields=["trusted_until"])
        self.grant()
        self.client.cookies[settings.MFA_TRUST_COOKIE_NAME] = signing.dumps(
            {"user": chief.pk, "device": chief_device.pk}, salt=mfa_trust.TRUST_SALT
        )
        self.challenged(self.admin_login())

    def test_zero_days_is_the_kill_switch_at_the_admin(self):
        # The deployment-wide off-switch reaches this door too: the grant and
        # the marker are both still here, and the challenge is straight back.
        self.grant()
        with override_settings(MFA_TRUST_DAYS=0):
            self.challenged(self.admin_login())

    # -- 3. the challenge is not weakened ----------------------------------

    def test_an_invalid_code_is_still_refused_while_a_marker_is_present(self):
        # Holding a marker does not make a wrong code acceptable: once the
        # grant is gone, the same invalid code is refused exactly as before.
        self.grant()
        self.device.trusted_until = timezone.now() - timedelta(seconds=1)
        self.device.save(update_fields=["trusted_until"])
        res = self.admin_login(totp="000000")
        self.assertFalse(self.is_logged_in())
        self.assertContains(res, "Invalid or expired authentication code.")

    def test_trust_cannot_resurrect_a_spent_code_at_the_admin(self):
        # The replay guard across the trust boundary: the code the opt-in
        # spent stays spent, and the next unused one is what works.
        self.grant()
        device = TOTPDevice.objects.get(user=self.boss)
        spent = totp.hotp(device.secret, device.last_used_counter)
        # The skipped login spends nothing...
        self.admin_login()
        self.assertTrue(self.is_logged_in())
        self.client.logout()
        # ...and the code the opt-in spent is still spent once the challenge
        # is back: the next unused one is what works.
        self.device.trusted_until = timezone.now() - timedelta(seconds=1)
        self.device.save(update_fields=["trusted_until"])
        res = self.admin_login(totp=spent)
        self.assertContains(res, "Invalid or expired authentication code.")
        self.assertFalse(self.is_logged_in())
        self.admin_login(totp=code_after(device))
        self.assertTrue(self.is_logged_in())

    def test_disabling_mfa_still_demands_enrollment_at_the_admin(self):
        # The enrollment check outranks the skip, so a leftover marker can
        # never answer a login whose factor was turned off.
        self.grant()
        device = TOTPDevice.objects.get(user=self.boss)
        res = self.client.post(
            "/api/accounts/mfa/disable/",
            {"code": code_after(device)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        res = self.admin_login()
        self.assertFalse(self.is_logged_in())
        self.assertContains(res, "Multi-factor authentication is mandatory")

    # -- 4. the plumbing, and the untouched third parties ------------------

    def test_the_form_reads_the_marker_off_the_request_it_is_given(self):
        # White box on the plumbing itself: the form is handed a request
        # (as AdminSite -> LoginView does) and finds the marker there.
        from accounts.admin import MFAAdminAuthenticationForm

        self.grant()
        request = RequestFactory().post("/admin/login/")
        request.COOKIES = {
            settings.MFA_TRUST_COOKIE_NAME: self.client.cookies[
                settings.MFA_TRUST_COOKIE_NAME
            ].value
        }
        form = MFAAdminAuthenticationForm(
            request=request, data={"username": "boss", "password": PASSWORD}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["username"], "boss")

    def test_a_form_without_a_request_is_challenged_not_crashed(self):
        # No request means no browser to vouch for: challenge, never 500.
        from accounts.admin import MFAAdminAuthenticationForm

        self.grant()
        form = MFAAdminAuthenticationForm(
            data={"username": "boss", "password": PASSWORD}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Enter your authentication code.", str(form.errors))

    def test_non_privileged_staff_never_meet_the_challenge_at_the_admin(self):
        helper = self.make_user("helper")
        helper.is_staff = True
        helper.save()
        self.admin_login("helper")
        self.assertTrue(self.is_logged_in())

    def test_a_customer_is_still_refused_the_admin_door(self):
        # The admin door answers a non-staff account with the stock admin
        # message and no MFA wording at all, so neither the challenge nor
        # the enrollment block leaks to a caller who failed the first factor
        # (and the trust machinery changes nothing for them either).
        self.make_user("shopper")
        res = self.admin_login("shopper")
        self.assertFalse(self.is_logged_in())
        self.assertContains(res, "correct username and password for a staff account")
        self.assertNotContains(res, "Enter your authentication code.")
        self.assertNotContains(res, "Multi-factor authentication is mandatory")


class AdminSiteWiringTests(SimpleTestCase):
    def test_default_site_is_mfa_site_with_mfa_form(self):
        from accounts.admin import MFAAdminAuthenticationForm
        from config.admin import MFAAdminSite
        from django.contrib import admin

        self.assertIsInstance(admin.site, MFAAdminSite)
        self.assertIs(admin.site.login_form, MFAAdminAuthenticationForm)

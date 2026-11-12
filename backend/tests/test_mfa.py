import base64
from datetime import timedelta
from unittest import mock
from xml.etree import ElementTree

import segno
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core import signing
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle

from accounts import mfa_trust
from accounts.admin import ADMIN_LOGIN_THROTTLE_MESSAGE, AdminLoginThrottle
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
    return TOTPDevice.objects.create(
        user=user, secret=secret, enabled=True, confirmed_at=timezone.now()
    )


def login_payload(username, password=PASSWORD, **extra):
    payload = {"username": username, "password": password}
    payload.update(extra)
    return payload


def code_after(device):
    """The next acceptable code is watermark + 1, inside the +-1 window with no sleeps; the instance is refreshed first because api_login rewinds the watermark in the DB."""
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
        self.assertIsNone(
            totp.verify_code(RFC_SECRET, code, at_time, last_used_counter=base)
        )
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

    def test_anonymous_gets_uniform_denials(self):
        self.assertEqual(self.client.get("/api/accounts/mfa/status/").status_code, 403)
        res = self.client.post("/api/accounts/mfa/disable/", {}, format="json")
        self.assertEqual(res.status_code, 403)
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
        self.assertTrue(device.enabled)

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
        self.assertFalse(device.enabled)
        self.assertIsNone(device.confirmed_at)

    def test_setup_while_pending_needs_no_code(self):
        # Once a rotation is pending (device disabled), a JWT holder who already proved the factor may re-mint freely; the code guard protects an ACTIVE device.
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
        self.auth(None)
        res = self.client.post(
            "/api/accounts/login/", login_payload("boss"), format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Multi-factor", res.data["details"]["totp"][0])
        res = self.bootstrap_setup()
        self.assertEqual(res.status_code, 200)

    def test_disable_without_device_rejected(self):
        _, token = self.api_login("boss")
        self.auth(token)
        TOTPDevice.objects.all().delete()
        res = self.client.post(
            "/api/accounts/mfa/disable/", {"code": "000000"}, format="json"
        )
        self.assertEqual(res.status_code, 400)


# Q-S4: the budget override keeps this class's many refused admin logins at the
# EXACT MFA refusal, never the throttle's generic one.
@override_settings(ADMIN_LOGIN_THROTTLE_RATE="1000/min")
class MFAEnrollmentReachTests(ApiTestCase):
    def setUp(self):
        self.boss = make_privileged("boss")

    def admin_login_page(self):
        return self.client.get("/admin/login/")

    def test_admin_login_page_always_offers_enrollment(self):
        res = self.admin_login_page()
        self.assertContains(res, settings.MFA_ENROLL_URL)
        self.assertContains(res, "Enroll your device")

    def test_enrollment_url_is_configuration_not_a_constant(self):
        with override_settings(MFA_ENROLL_URL="https://elsewhere.example.com/enroll"):
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
        res = self.client.post(
            "/admin/login/",
            {"username": "boss", "password": "Wr0ng-Passphrase!"},
            follow=True,
        )
        self.assertContains(res, "Please enter the correct username and password")
        self.assertNotContains(res, "Set up multi-factor authentication")
        self.assertContains(res, settings.MFA_ENROLL_URL)

    def test_signed_in_admin_visit_is_redirected_not_rendered(self):
        self.client.force_login(
            User.objects.create_superuser(
                username="root", email="root@example.com", password=PASSWORD
            )
        )
        res = self.admin_login_page()
        self.assertEqual(res.status_code, 302)

    def test_login_page_never_renders_a_secret(self):
        enroll_via_model(self.boss)
        res = self.admin_login_page()
        self.assertNotContains(res, TEST_TOTP_SECRET)

    def test_enrollment_reach_needs_no_jwt_then_login_works(self):
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
    def test_artifact_is_an_inline_svg_of_the_right_geometry(self):
        uri = totp.otpauth_uri(TEST_TOTP_SECRET, "boss")
        artifact = qr.qr_data_uri(uri)
        self.assertTrue(
            artifact.startswith(qr.SVG_DATA_URI_PREFIX)
            and artifact.startswith("data:image/svg+xml;base64,")
        )
        svg = base64.b64decode(artifact[len("data:image/svg+xml;base64,") :])
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
        artifact = qr.qr_data_uri(totp.otpauth_uri(TEST_TOTP_SECRET, "boss"))
        self.assertNotIn(
            TEST_TOTP_SECRET,
            base64.b64decode(artifact[len("data:image/svg+xml;base64,") :]).decode(),
        )


class MFAEnrollmentQrTests(ApiTestCase):
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
        self.assertEqual(second.status_code, 403)
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
        self.assertEqual(res.status_code, 400)
        enroll_via_model(root)
        res = self.client.post(
            "/api/accounts/login/",
            login_payload("root", totp=self.valid_code()),
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)


class MfaTrustDeviceTests(ApiTestCase):
    TRUST_PATH = "/api/accounts/mfa/trust/"
    LOGIN_PATH = "/api/accounts/login/"

    def setUp(self):
        self.boss = make_privileged("boss")
        self.device = enroll_via_model(self.boss)

    def grant(self):
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
        self.assertEqual(res.status_code, 400, res.data)
        self.assertNotIn("access", res.data)
        self.assertIn("totp", res.data["details"])
        return res.data["details"]["totp"][0]

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
        self.grant()
        device = TOTPDevice.objects.get(user=self.boss)
        watermark = device.last_used_counter
        res = self.login(totp="000000")
        self.assertEqual(res.status_code, 200, res.data)
        device.refresh_from_db()
        self.assertEqual(device.last_used_counter, watermark)

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
        self.challenge(self.login(client=self.fresh_client()))

    def test_enrollment_never_grants_trust(self):
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
        res = self.client.post(self.LOGIN_PATH, login_payload("newbie"), format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("authentication code", res.data["details"]["totp"][0])

    def test_trust_requires_privilege_a_session_and_a_fresh_code(self):
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

    def test_a_forged_marker_is_refused(self):
        self.grant()
        real = self.client.cookies[settings.MFA_TRUST_COOKIE_NAME].value
        self.client.cookies[settings.MFA_TRUST_COOKIE_NAME] = real[:-1] + (
            "A" if real[-1] != "A" else "B"
        )
        self.challenge(self.login())

    def test_another_users_marker_does_not_carry_over(self):
        # Shared/kiosk browser: the marker names (user, device), so one user's grant can never answer another's challenge.
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
        self.grant()
        device = TOTPDevice.objects.get(user=self.boss)
        spent = totp.hotp(device.secret, device.last_used_counter)
        self.assertEqual(self.login().status_code, 200)
        self.device.trusted_until = timezone.now() - timedelta(seconds=1)
        self.device.save(update_fields=["trusted_until"])
        message = self.challenge(self.login(totp=spent))
        self.assertIn("Invalid or expired", message)
        self.assertEqual(self.login(totp=code_after(device)).status_code, 200)

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
        self.auth(None)
        self.assertIn("Multi-factor", self.challenge(self.login()))

    def test_re_enrolling_drops_the_trust(self):
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

    def test_customers_are_untouched_by_trust(self):
        self.make_user("buyer")
        res = self.client.post(self.LOGIN_PATH, login_payload("buyer"), format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn(settings.MFA_TRUST_COOKIE_NAME, res.cookies)
        self.assertFalse(
            TOTPDevice.objects.filter(trusted_until__isnull=False).exists()
        )


class StorefrontLoginSplitTests(ApiTestCase):
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
        res = self.client.post(
            self.CUSTOMER_DOOR,
            login_payload("buyer", totp="000000"),
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(TOTPDevice.objects.exists())

    def test_customer_door_keeps_the_refresh_cookie_contract(self):
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
        res = self.client.post(self.CUSTOMER_DOOR, login_payload("boss"), format="json")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("access", res.data)
        self.assertIn("staff sign-in page", res.data["error"])
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.AUTH_LOGIN_FAILED, actor=boss
            ).exists()
        )

    def test_customer_door_refuses_an_unenrolled_privileged_account(self):
        make_privileged("newbie")
        res = self.client.post(
            self.CUSTOMER_DOOR, login_payload("newbie"), format="json"
        )
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("access", res.data)

    def test_customer_door_serves_non_privileged_staff_unchanged(self):
        helper = self.make_user("helper")
        helper.is_staff = True
        helper.save()
        helper.groups.add(Group.objects.get_or_create(name=ROLE_SUPPORT)[0])
        res = self.client.post(
            self.CUSTOMER_DOOR, login_payload("helper"), format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)

    def test_staff_door_still_ignores_a_customers_totp(self):
        res = self.client.post(
            self.STAFF_DOOR, login_payload("buyer", totp="000000"), format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(TOTPDevice.objects.exists())

    def test_staff_door_still_demands_and_spends_the_factor(self):
        boss = make_privileged("boss")
        device = enroll_via_model(boss)
        res = self.client.post(self.STAFF_DOOR, login_payload("boss"), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("access", res.data)
        res = self.client.post(
            self.STAFF_DOOR, login_payload("boss", totp="000000"), format="json"
        )
        self.assertEqual(res.status_code, 400)
        code = totp.hotp(device.secret, totp.now() // totp.STEP)
        res = self.client.post(
            self.STAFF_DOOR, login_payload("boss", totp=code), format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        replay = self.fresh_client()
        res = replay.post(
            self.STAFF_DOOR, login_payload("boss", totp=code), format="json"
        )
        self.assertEqual(res.status_code, 400)


@override_settings(ADMIN_LOGIN_THROTTLE_RATE="1000/min")
class AdminMFAEnforcementTests(ApiTestCase):
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
        self.admin_login("boss", totp=self.valid_code())
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
        self.admin_login("helper")
        self.assertTrue(self.is_logged_in())

    def test_superuser_requires_code(self):
        root = make_privileged("root", is_superuser=True)
        enroll_via_model(root)
        self.admin_login("root")
        self.assertFalse(self.is_logged_in())
        self.admin_login("root", totp=self.valid_code())
        self.assertTrue(self.is_logged_in())


@override_settings(ADMIN_LOGIN_THROTTLE_RATE="1000/min")
class AdminMfaTrustDeviceTests(ApiTestCase):
    TRUST_PATH = "/api/accounts/mfa/trust/"

    def setUp(self):
        self.boss = make_privileged("boss")
        self.device = enroll_via_model(self.boss)

    def grant(self):
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
        self.assertIn(settings.MFA_TRUST_COOKIE_NAME, res.cookies)
        return res

    def admin_login(self, username="boss", client=None, **extra):
        payload = {"username": username, "password": PASSWORD, "next": "/admin/"}
        payload.update(extra)
        return (client or self.client).post("/admin/login/", payload, follow=True)

    def is_logged_in(self, client=None):
        return "_auth_user_id" in (client or self.client).session

    def challenged(self, res, client=None):
        self.assertFalse(self.is_logged_in(client))
        self.assertContains(res, "Enter your authentication code.")
        return res

    def test_trusted_device_reaches_the_admin_without_a_fresh_code(self):
        self.grant()
        res = self.admin_login()
        self.assertTrue(self.is_logged_in())
        self.assertContains(res, "/admin/logout/")
        self.assertNotContains(res, "Enter your authentication code.")

    def test_a_trusted_admin_login_spends_no_code_and_ignores_a_posted_one(self):
        self.grant()
        watermark = TOTPDevice.objects.get(user=self.boss).last_used_counter
        self.admin_login(totp="000000")
        self.assertTrue(self.is_logged_in())
        device = TOTPDevice.objects.get(user=self.boss)
        self.assertEqual(device.last_used_counter, watermark)

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
        self.grant()
        with override_settings(MFA_TRUST_DAYS=0):
            self.challenged(self.admin_login())

    def test_an_invalid_code_is_still_refused_while_a_marker_is_present(self):
        self.grant()
        self.device.trusted_until = timezone.now() - timedelta(seconds=1)
        self.device.save(update_fields=["trusted_until"])
        res = self.admin_login(totp="000000")
        self.assertFalse(self.is_logged_in())
        self.assertContains(res, "Invalid or expired authentication code.")

    def test_trust_cannot_resurrect_a_spent_code_at_the_admin(self):
        self.grant()
        device = TOTPDevice.objects.get(user=self.boss)
        spent = totp.hotp(device.secret, device.last_used_counter)
        self.admin_login()
        self.assertTrue(self.is_logged_in())
        self.client.logout()
        self.device.trusted_until = timezone.now() - timedelta(seconds=1)
        self.device.save(update_fields=["trusted_until"])
        res = self.admin_login(totp=spent)
        self.assertContains(res, "Invalid or expired authentication code.")
        self.assertFalse(self.is_logged_in())
        self.admin_login(totp=code_after(device))
        self.assertTrue(self.is_logged_in())

    def test_disabling_mfa_still_demands_enrollment_at_the_admin(self):
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

    def test_the_form_reads_the_marker_off_the_request_it_is_given(self):
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
        self.make_user("shopper")
        res = self.admin_login("shopper")
        self.assertFalse(self.is_logged_in())
        self.assertContains(res, "correct username and password for a staff account")
        self.assertNotContains(res, "Enter your authentication code.")
        self.assertNotContains(res, "Multi-factor authentication is mandatory")


class AdminSiteWiringTests(SimpleTestCase):
    def test_default_site_is_mfa_site_with_mfa_form(self):
        from django.contrib import admin

        from accounts.admin import MFAAdminAuthenticationForm
        from config.admin import MFAAdminSite

        self.assertIsInstance(admin.site, MFAAdminSite)
        self.assertIs(admin.site.login_form, MFAAdminAuthenticationForm)


@override_settings(ADMIN_LOGIN_THROTTLE_RATE="1000/min")
class AdminMfaPathMatrixCells(ApiTestCase):
    def setUp(self):
        self.boss = make_privileged("boss")
        enroll_via_model(self.boss)

    def test_wrong_password_with_a_valid_code_is_still_refused(self):
        code = totp.hotp(TEST_TOTP_SECRET, totp.now() // totp.STEP)
        res = self.client.post(
            "/admin/login/",
            {"username": "boss", "password": "Wr0ng-Passphrase!", "totp": code},
        )
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(res, "Please enter the correct username and password")
        self.assertNotContains(res, "Enter your authentication code.")
        self.assertNotContains(res, "Invalid or expired authentication code.")
        self.assertNotContains(res, "Multi-factor authentication is mandatory")

    def test_unknown_user_with_a_code_is_not_promoted(self):
        code = totp.hotp(TEST_TOTP_SECRET, totp.now() // totp.STEP)
        res = self.client.post(
            "/admin/login/",
            {"username": "ghost", "password": PASSWORD, "totp": code},
        )
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(res, "Please enter the correct username and password")
        self.assertNotContains(res, "Enter your authentication code.")
        self.assertNotContains(res, "Invalid or expired authentication code.")


class AdminLoginThrottleTests(ApiTestCase):
    RATE = "2/min"

    def setUp(self):
        self.boss = make_privileged("boss")
        self.device = enroll_via_model(self.boss)
        cache.clear()
        override = override_settings(ADMIN_LOGIN_THROTTLE_RATE=self.RATE)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(cache.clear)

    def valid_code(self):
        return totp.hotp(TEST_TOTP_SECRET, totp.now() // totp.STEP)

    def post_login(self, username, password=PASSWORD, client=None, meta=None, **extra):
        payload = {"username": username, "password": password}
        payload.update(extra)
        return (client or self.client).post("/admin/login/", payload, **(meta or {}))

    def is_logged_in(self, client=None):
        return "_auth_user_id" in (client or self.client).session

    def test_spent_budget_refuses_further_attempts_with_one_generic_message(self):
        first = self.post_login("boss", "Wr0ng-Passphrase!")
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, "Please enter the correct username and password")
        second = self.post_login("boss", "Wr0ng-Passphrase!")
        self.assertContains(second, "Please enter the correct username and password")
        third = self.post_login("boss", PASSWORD, totp=self.valid_code())
        self.assertEqual(third.status_code, 200)
        self.assertContains(third, ADMIN_LOGIN_THROTTLE_MESSAGE)
        self.assertFalse(self.is_logged_in())

    def test_successes_spend_nothing(self):
        self.post_login("boss", PASSWORD, totp=self.valid_code())
        self.assertTrue(self.is_logged_in())
        self.client.logout()
        self.post_login("boss", PASSWORD, totp=code_after(self.device))
        self.assertTrue(self.is_logged_in())
        self.client.logout()
        fail = self.post_login("boss", "Wr0ng-Passphrase!")
        self.assertNotContains(fail, ADMIN_LOGIN_THROTTLE_MESSAGE)
        self.assertContains(fail, "Please enter the correct username and password")

    def test_the_budget_is_per_client_ident(self):
        self.post_login("boss", "Wr0ng-Passphrase!")
        self.post_login("boss", "Wr0ng-Passphrase!")
        blocked = self.post_login("boss", "Wr0ng-Passphrase!")
        self.assertContains(blocked, ADMIN_LOGIN_THROTTLE_MESSAGE)
        other = self.post_login(
            "boss", "Wr0ng-Passphrase!", meta={"REMOTE_ADDR": "10.9.8.7"}
        )
        self.assertNotContains(other, ADMIN_LOGIN_THROTTLE_MESSAGE)
        self.assertContains(other, "Please enter the correct username and password")

    def test_the_budget_restores_when_the_window_lapses(self):
        clock = {"now": 1_000_000.0}
        frozen = staticmethod(lambda: clock["now"])
        with mock.patch.object(AdminLoginThrottle, "timer", frozen):
            self.post_login("boss", "Wr0ng-Passphrase!")
            self.post_login("boss", "Wr0ng-Passphrase!")
            blocked = self.post_login("boss", "Wr0ng-Passphrase!")
            self.assertContains(blocked, ADMIN_LOGIN_THROTTLE_MESSAGE)
            clock["now"] += 61
            recovered = self.post_login("boss", "Wr0ng-Passphrase!")
            self.assertNotContains(recovered, ADMIN_LOGIN_THROTTLE_MESSAGE)
            self.assertContains(
                recovered, "Please enter the correct username and password"
            )

    def test_empty_rate_is_the_documented_kill_switch(self):
        with override_settings(ADMIN_LOGIN_THROTTLE_RATE=""):
            for _ in range(5):
                res = self.post_login("boss", "Wr0ng-Passphrase!")
                self.assertNotContains(res, ADMIN_LOGIN_THROTTLE_MESSAGE)
                self.assertContains(
                    res, "Please enter the correct username and password"
                )


class AdminLoginThrottleSettingsTests(SimpleTestCase):
    def test_unset_env_yields_the_documented_default(self):
        self.assertEqual(settings.ADMIN_LOGIN_THROTTLE_RATE, "10/min")


class AdminLoginThrottleNonCoverageTests(ApiTestCase):
    def test_the_api_auth_budget_does_not_bound_the_admin_form(self):
        boss = make_privileged("boss")
        enroll_via_model(boss)
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["auth"] = "1/min"
        cache.clear()
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            bad = self.client.post(
                "/api/accounts/login/",
                {"username": "boss", "password": "Wr0ng-Passphrase!"},
                format="json",
            )
            self.assertEqual(bad.status_code, 401, bad.data)
            limited = self.client.post(
                "/api/accounts/login/",
                {"username": "boss", "password": "Wr0ng-Passphrase!"},
                format="json",
            )
            self.assertEqual(limited.status_code, 429, limited.data)
            code = totp.hotp(TEST_TOTP_SECRET, totp.now() // totp.STEP)
            res = self.client.post(
                "/admin/login/",
                {
                    "username": "boss",
                    "password": PASSWORD,
                    "totp": code,
                    "next": "/admin/",
                },
                follow=True,
            )
            self.assertEqual(res.status_code, 200)
            self.assertIn("_auth_user_id", self.client.session)


class MfaEnrollmentThrottleTests(ApiTestCase):
    def test_the_auth_budget_engages_on_setup_and_confirm(self):
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["auth"] = "2/min"
        cache.clear()
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            for _ in range(2):
                res = self.client.post("/api/accounts/mfa/setup/", {}, format="json")
                self.assertEqual(res.status_code, 401, res.data)
            limited = self.client.post("/api/accounts/mfa/setup/", {}, format="json")
            self.assertEqual(limited.status_code, 429, limited.data)
            confirm = self.client.post("/api/accounts/mfa/confirm/", {}, format="json")
            self.assertEqual(confirm.status_code, 429, confirm.data)

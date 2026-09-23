"""SPEC-17-05 [R-17.9]: mandatory TOTP MFA for privileged roles.

Pins the whole feature end to end:

- common/totp.py against the public RFC 6238 SHA-1 test vectors and every
  branch of the verification/replay logic (stdlib-only helper — R-17.9
  names no mechanism, so no package was added);
- the enrollment API (setup/confirm/status/disable) for privileged roles,
  both trust paths (JWT steady-state and the credential bootstrap that
  keeps blocked-at-login enforcement reachable);
- enforcement at BOTH surfaces: the staff API login (totp required for
  privileged users, replay-guarded) and the Django admin login form;
- rollout semantics: unenrolled privileged accounts are blocked with the
  enrollment path named; customers and non-admin staff are untouched;
- secret exposure: shown exactly once at setup, never returned again.
"""
from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase
from django.utils import timezone

from accounts.models import TOTPDevice
from common import totp
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

    def test_customer_totp_field_ignored(self):
        self.make_user("buyer")
        res = self.client.post(
            "/api/accounts/login/", login_payload("buyer", totp="000000"), format="json"
        )
        self.assertEqual(res.status_code, 200)

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


class AdminSiteWiringTests(SimpleTestCase):
    def test_default_site_is_mfa_site_with_mfa_form(self):
        from accounts.admin import MFAAdminAuthenticationForm
        from config.admin import MFAAdminSite
        from django.contrib import admin

        self.assertIsInstance(admin.site, MFAAdminSite)
        self.assertIs(admin.site.login_form, MFAAdminAuthenticationForm)

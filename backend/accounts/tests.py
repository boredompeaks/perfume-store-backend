"""Accounts unit tests - docs/test-gaps.md items 1-14.

Covers registration validation, email verification, enumeration-safe
recovery flows, and the V-05 password policy: registration and password
reset both run Django's ``validate_password`` (conventions.md), so a
password the shared validators reject fails both paths in the same
field-error shape.
"""
import unittest
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import tag
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from accounts.serializers import RegisterSerializer
from accounts.views import LoginView, _encoded_user_id, _get_user, register
from common.testing import ApiTestCase, extract_link_params
from django.core.cache import cache
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle


def make_inactive_user(username="pending", email=None):
    email = email or f"{username}@example.com"
    serializer = RegisterSerializer(
        data={"username": username, "email": email, "password": "S3cure-Passphrase!"}
    )
    serializer.is_valid(raise_exception=True)
    return serializer.save()


@tag("accounts")
class RegisterSerializerTests(ApiTestCase):
    # 1. valid data creates inactive user -------------------------------------------------
    def test_valid_data_creates_inactive_user(self):
        serializer = RegisterSerializer(
            data={"username": "newuser", "email": "new@example.com", "password": "S3cure-Passphrase!"}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        user = serializer.save()

        self.assertIsInstance(user, User)
        self.assertFalse(user.is_active)
        self.assertEqual(user.username, "newuser")
        self.assertEqual(user.email, "new@example.com")
        # password stored as a hash, never plaintext
        self.assertNotEqual(user.password, "S3cure-Passphrase!")
        self.assertTrue(user.check_password("S3cure-Passphrase!"))

    # 2. duplicate username (iexact) rejected ---------------------------------------------
    def test_duplicate_username_case_insensitive_rejected(self):
        User.objects.create_user("alice", "alice@example.com", "S3cure-Passphrase!")
        for candidate in ("alice", "ALICE", "Alice"):
            serializer = RegisterSerializer(
                data={"username": candidate, "email": "other@example.com", "password": "S3cure-Passphrase!"}
            )
            self.assertFalse(serializer.is_valid(), candidate)
            self.assertIn("username", serializer.errors)

        self.assertEqual(User.objects.filter(username__iexact="alice").count(), 1)

    # 3. duplicate email (iexact) rejected --------------------------------------------------
    def test_duplicate_email_case_insensitive_rejected(self):
        User.objects.create_user("alice", "alice@example.com", "S3cure-Passphrase!")
        for candidate in ("alice@example.com", "ALICE@EXAMPLE.COM"):
            serializer = RegisterSerializer(
                data={"username": f"user_{candidate}", "email": candidate, "password": "S3cure-Passphrase!"}
            )
            self.assertFalse(serializer.is_valid(), candidate)
            self.assertIn("email", serializer.errors)
            self.assertIn("already uses this email", str(serializer.errors["email"]))

    def test_blank_email_rejected(self):
        serializer = RegisterSerializer(
            data={"username": "blankmail", "email": "", "password": "S3cure-Passphrase!"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("email", serializer.errors)

    # 4. password < 8 rejected ----------------------------------------------------------------
    def test_short_password_rejected(self):
        serializer = RegisterSerializer(
            data={"username": "shorty", "email": "shorty@example.com", "password": "short12"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("password", serializer.errors)
        self.assertIn("at least 8 characters", str(serializer.errors["password"]))

    def test_missing_username_or_password_rejected(self):
        for missing in ("username", "password"):
            data = {"username": "u", "email": "u@example.com", "password": "S3cure-Passphrase!"}
            data.pop(missing)
            serializer = RegisterSerializer(data=data)
            self.assertFalse(serializer.is_valid(), missing)
            self.assertIn(missing, serializer.errors)

    @unittest.expectedFailure
    def test_registration_gap_missing_email_currently_accepted(self):
        """Registration validation gap: `User.email` is blank=True, so DRF
        treats email as optional and an account with an empty address can be
        created (it can then never complete verification). Flip when the
        serializer marks email required."""
        serializer = RegisterSerializer(
            data={"username": "noemail", "password": "S3cure-Passphrase!"}
        )
        self.assertFalse(serializer.is_valid(), serializer.errors)

    def test_response_never_returns_password(self):
        res = self.client.post(
            "/api/accounts/register/",
            {"username": "shapecheck", "email": "shape@example.com", "password": "S3cure-Passphrase!"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            res.data["user"], {"id": User.objects.get(username="shapecheck").id, "username": "shapecheck", "email": "shape@example.com"}
        )
        self.assertNotIn("password", res.data["user"])

    # 5. V-05: registration runs the same validate_password policy as reset --------------
    def test_v05_registration_rejects_common_password(self):
        """V-05: `password`-style passwords clear the length validator but
        fail CommonPasswordValidator now that RegisterSerializer runs
        validate_password — the same policy the reset path enforces."""
        res = self.client.post(
            "/api/accounts/register/",
            {"username": "weakpass", "email": "weak@example.com", "password": "password"},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("password", res.data)
        self.assertIn("common", " ".join(res.data["password"]).lower())

    def test_v05_registration_rejects_numeric_only_password(self):
        """V-05 companion: all-numeric passwords are rejected by
        NumericPasswordValidator through the shared policy."""
        res = self.client.post(
            "/api/accounts/register/",
            {"username": "numericpass", "email": "numeric@example.com", "password": "987654321"},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("password", res.data)
        self.assertIn("numeric", " ".join(res.data["password"]).lower())

    def test_password_similar_to_submitted_attributes_rejected(self):
        """User-aware parity with reset: the similarity validator runs against
        the username/email being registered (transient user), not against
        nothing."""
        cases = {
            ("hannelore", "hannelore@example.com"): "hannelorepass",  # ~ username
            ("seafoam", "seafoam@example.com"): "seafoam@example.com",  # == email
        }
        for (username, email), password in cases.items():
            with self.subTest(username=username):
                serializer = RegisterSerializer(
                    data={"username": username, "email": email, "password": password}
                )
                self.assertFalse(serializer.is_valid())
                self.assertIn("password", serializer.errors)
                self.assertIn("similar", " ".join(serializer.errors["password"]).lower())

    def test_policy_errors_match_reset_path_shape_and_block_creation(self):
        """Parity + uniform-shape contract (conventions.md): registration and
        reset-confirm both answer 400 with a list of validator messages under
        the `password` key — the shape the frontend's fieldErrors renderer
        consumes for both forms. A rejected password must also create no user
        row and send no verification email."""
        res = self.client.post(
            "/api/accounts/register/",
            {"username": "parity", "email": "parity@example.com", "password": "password"},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIsInstance(res.data["password"], list)
        self.assertTrue(all(isinstance(message, str) for message in res.data["password"]))
        self.assertFalse(User.objects.filter(username="parity").exists())
        self.assertEqual(len(mail.outbox), 0)

        reset_user = self.make_user("parityreset")
        reset_res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {
                "uid": _encoded_user_id(reset_user),
                "token": default_token_generator.make_token(reset_user),
                "password": "password",
            },
            format="json",
        )
        self.assertEqual(reset_res.status_code, 400, reset_res.data)
        self.assertIsInstance(reset_res.data["password"], list)
        self.assertTrue(all(isinstance(message, str) for message in reset_res.data["password"]))


@tag("accounts")
class RegisterViewTests(ApiTestCase):
    # 6. SMTP failure -> 503, account still created (F-28) ---------------------------------
    def test_smtp_failure_returns_503_and_account_still_created(self):
        with mock.patch("accounts.views._send_verification_email", side_effect=Exception("smtp down")):
            res = self.client.post(
                "/api/accounts/register/",
                {"username": "nomail", "email": "nomail@example.com", "password": "S3cure-Passphrase!"},
                format="json",
            )

        self.assertEqual(res.status_code, 503, res.data)
        self.assertIn("could not be sent", res.data["error"])
        user = User.objects.get(username="nomail")
        self.assertFalse(user.is_active)
        self.assertEqual(len(mail.outbox), 0)

    def test_duplicate_registration_rejected_before_smtp(self):
        User.objects.create_user("taken", "taken@example.com", "S3cure-Passphrase!")
        with mock.patch("accounts.views._send_verification_email") as send_mock:
            res = self.client.post(
                "/api/accounts/register/",
                {"username": "taken2", "email": "taken@example.com", "password": "S3cure-Passphrase!"},
                format="json",
            )
        self.assertEqual(res.status_code, 400, res.data)
        send_mock.assert_not_called()


@tag("accounts")
class VerifyEmailTests(ApiTestCase):
    # 7. valid token activates user -----------------------------------------------------------
    def test_valid_token_activates_user(self):
        user = make_inactive_user("activateme")

        res = self.client.post(
            "/api/accounts/verify-email/",
            {"uid": _encoded_user_id(user), "token": default_token_generator.make_token(user)},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("Email verified", res.data["message"])
        user.refresh_from_db()
        self.assertTrue(user.is_active)

        # idempotent: verifying an already-active account still succeeds
        res = self.client.post(
            "/api/accounts/verify-email/",
            {"uid": _encoded_user_id(user), "token": default_token_generator.make_token(user)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

    # 8. invalid/expired token -> 400 ------------------------------------------------------------
    def test_invalid_token_rejected(self):
        user = make_inactive_user("paranoid")

        for payload in (
            {"uid": _encoded_user_id(user), "token": "wrong-token"},
            {"uid": "!!!not-base64!!!", "token": "whatever"},
            {"uid": urlsafe_base64_encode(force_bytes("999999")), "token": "whatever"},
            {"uid": _encoded_user_id(user), "token": ""},
            {},
        ):
            res = self.client.post("/api/accounts/verify-email/", payload, format="json")
            self.assertEqual(res.status_code, 400, payload)
            self.assertIn("invalid or expired", res.data["error"])

        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_token_from_another_user_rejected(self):
        """Security: a token minted for user B must never activate user A."""
        user_a = make_inactive_user("usera", "a@example.com")
        user_b = make_inactive_user("userb", "b@example.com")
        token_b = default_token_generator.make_token(user_b)

        res = self.client.post(
            "/api/accounts/verify-email/",
            {"uid": _encoded_user_id(user_a), "token": token_b},
            format="json",
        )

        self.assertEqual(res.status_code, 400, res.data)
        user_a.refresh_from_db()
        self.assertFalse(user_a.is_active)

    def test_get_user_helper_rejects_garbage(self):
        self.assertIsNone(_get_user("!!!"))
        self.assertIsNone(_get_user(urlsafe_base64_encode(force_bytes("424242"))))
        user = make_inactive_user("helper")
        self.assertEqual(_get_user(_encoded_user_id(user)), user)


@tag("accounts")
class ResendVerificationTests(ApiTestCase):
    # 9. unknown email -> uniform 200 (no enumeration) -------------------------------------
    def test_unknown_email_uniform_response_no_email_sent(self):
        res = self.client.post("/api/accounts/resend-verification/", {"email": "ghost@example.com"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["message"], "If an unverified account exists, a verification email has been sent.")
        self.assertEqual(len(mail.outbox), 0)

    def test_missing_email_uniform_response(self):
        res = self.client.post("/api/accounts/resend-verification/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_inactive_account_receives_verification_email(self):
        user = make_inactive_user("resendme")

        res = self.client.post("/api/accounts/resend-verification/", {"email": "resendme@example.com"}, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        uid, token = extract_link_params(mail.outbox[0].body, "verify-email")
        res = self.client.post("/api/accounts/verify-email/", {"uid": uid, "token": token}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(User.objects.get(username="resendme").is_active)

    def test_active_account_gets_no_email(self):
        self.make_user("alreadyactive")
        res = self.client.post("/api/accounts/resend-verification/", {"email": "alreadyactive@example.com"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_smtp_failure_returns_503(self):
        make_inactive_user("smtpfail")
        with mock.patch("accounts.views._send_verification_email", side_effect=Exception("smtp down")):
            res = self.client.post("/api/accounts/resend-verification/", {"email": "smtpfail@example.com"}, format="json")
        self.assertEqual(res.status_code, 503, res.data)
        self.assertIn("could not be sent", res.data["error"])


@tag("accounts")
class ForgotUsernameTests(ApiTestCase):
    # 10. unknown email -> uniform 200 ---------------------------------------------------------
    def test_unknown_email_uniform_response(self):
        res = self.client.post("/api/accounts/forgot-username/", {"email": "ghost@example.com"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["message"], "If an account exists for this email, the username has been sent.")
        self.assertEqual(len(mail.outbox), 0)

    def test_known_email_receives_username(self):
        self.make_user("rememberme")
        res = self.client.post("/api/accounts/forgot-username/", {"email": "rememberme@example.com"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Your username is: rememberme", mail.outbox[0].body)

    def test_smtp_failure_returns_503(self):
        self.make_user("smtpfail")
        with mock.patch("accounts.views._send_email", side_effect=Exception("smtp down")):
            res = self.client.post("/api/accounts/forgot-username/", {"email": "smtpfail@example.com"}, format="json")
        self.assertEqual(res.status_code, 503, res.data)


@tag("accounts")
class PasswordResetTests(ApiTestCase):
    # 11. request: inactive-only filter ------------------------------------------------------------
    def test_inactive_account_gets_no_reset_email(self):
        make_inactive_user("frozen", "frozen@example.com")
        res = self.client.post("/api/accounts/password-reset/", {"email": "frozen@example.com"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_active_account_receives_reset_link(self):
        user = self.make_user("resetme")
        res = self.client.post("/api/accounts/password-reset/", {"email": "resetme@example.com"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 1)
        uid, token = extract_link_params(mail.outbox[0].body, "reset-password")
        self.assertEqual(uid, _encoded_user_id(user))
        self.assertTrue(default_token_generator.check_token(user, token))

    def test_uniform_response_regardless_of_account_state(self):
        """Enumeration safety: identical body for known and unknown emails."""
        known = self.client.post("/api/accounts/password-reset/", {"email": "ghost@example.com"}, format="json")
        self.make_user("knownuser")
        unknown = self.client.post("/api/accounts/password-reset/", {"email": "knownuser@example.com"}, format="json")
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.data["message"], unknown.data["message"])

    def test_smtp_failure_returns_503(self):
        self.make_user("smtpfail")
        with mock.patch("accounts.views._send_email", side_effect=Exception("smtp down")):
            res = self.client.post("/api/accounts/password-reset/", {"email": "smtpfail@example.com"}, format="json")
        self.assertEqual(res.status_code, 503, res.data)
        self.assertEqual(len(mail.outbox), 0)

    # 12. confirm: weak password -> 400 with validator messages --------------------------------
    def test_weak_password_rejected_with_validator_messages(self):
        user = self.make_user("weakreset")
        payload = {
            "uid": _encoded_user_id(user),
            "token": default_token_generator.make_token(user),
        }

        cases = {
            "123": "too short",
            "password": "common",
            "weakresetperson": "similar",
        }
        for password, expected in cases.items():
            with self.subTest(password=password):
                res = self.client.post("/api/accounts/password-reset/confirm/", {**payload, "password": password}, format="json")
                self.assertEqual(res.status_code, 400, res.data)
                self.assertIn("password", res.data)
                self.assertIn(expected, " ".join(res.data["password"]).lower())

        user.refresh_from_db()
        self.assertTrue(user.check_password("S3cure-Passphrase!"))

    def test_invalid_link_rejected(self):
        user = self.make_user("linkcheck")
        for payload in (
            {"uid": _encoded_user_id(user), "token": "forged"},
            {"uid": "!!!", "token": "forged"},
            {"uid": _encoded_user_id(user)},
            {},
        ):
            res = self.client.post("/api/accounts/password-reset/confirm/", payload, format="json")
            self.assertEqual(res.status_code, 400, payload)
            self.assertIn("invalid or expired", res.data["error"])
        user.refresh_from_db()
        self.assertTrue(user.check_password("S3cure-Passphrase!"))

    # 13. confirm: valid -> password changed, old token invalidated ----------------------------
    def test_valid_confirm_changes_password_and_invalidates_token(self):
        user = self.make_user("changer")
        uid = _encoded_user_id(user)
        token = default_token_generator.make_token(user)

        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "N3w-Passphrase-99"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password("N3w-Passphrase-99"))
        self.assertFalse(user.check_password("S3cure-Passphrase!"))

        # token is single-use: hashed into the old password, now invalid
        self.assertFalse(default_token_generator.check_token(user, token))
        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "An0ther-Pass-77"},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password("N3w-Passphrase-99"))


@tag("accounts")
class UsernameAvailableTests(ApiTestCase):
    # 14. <3 chars / taken / free ---------------------------------------------------------------
    def test_too_short_username_reports_unavailable(self):
        for candidate in ("", "a", "ab", "  x  "):
            with self.subTest(candidate=candidate):
                res = self.client.get("/api/accounts/username-available/", {"username": candidate})
                self.assertEqual(res.status_code, 200, res.data)
                self.assertFalse(res.data["available"])
                self.assertIn("at least 3 characters", res.data["message"])

    def test_taken_username_case_insensitive(self):
        self.make_user("buyer")
        for candidate in ("buyer", "BUYER", "Buyer"):
            with self.subTest(candidate=candidate):
                res = self.client.get("/api/accounts/username-available/", {"username": candidate})
                self.assertFalse(res.data["available"])
                self.assertIn("already taken", res.data["message"])

    def test_free_username_reports_available(self):
        res = self.client.get("/api/accounts/username-available/", {"username": "freshname"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["available"])
        self.assertIn("available", res.data["message"])

    def test_missing_query_param_reports_unavailable(self):
        res = self.client.get("/api/accounts/username-available/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["available"])


@tag("accounts")
class AuthThrottleTests(ApiTestCase):
    """Conventions: every public mutating endpoint gets a throttle scope.
    Register + login share the 'auth' scope (V-04: credential stuffing,
    registration spam)."""

    def _register(self, username):
        return self.client.post(
            "/api/accounts/register/",
            {
                "username": username,
                "email": f"{username}@example.com",
                "password": "S3cure-Passphrase!",
            },
            format="json",
        )

    def test_auth_scope_and_rate_are_configured(self):
        self.assertEqual(register.view_class.throttle_scope, "auth")
        self.assertEqual(LoginView.throttle_scope, "auth")
        self.assertIn(ScopedRateThrottle, LoginView.throttle_classes)
        self.assertIn("auth", api_settings.DEFAULT_THROTTLE_RATES)

    def test_register_rate_limit_engages(self):
        """A second registration inside a 1/min budget is 429'd and creates
        no account. DRF binds THROTTLE_RATES at import, so the rate is
        patched on the throttle class rather than via override_settings."""
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["auth"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            self.assertEqual(self._register("first").status_code, 201)
            throttled = self._register("second")
        self.assertEqual(throttled.status_code, 429, throttled.data)
        self.assertFalse(User.objects.filter(username="second").exists())

    def test_login_rate_limit_engages(self):
        """The third login attempt inside a 2/min budget is 429'd — the
        credential-stuffing bound. Success responses stay the pinned
        TokenObtainPairView contract (access/refresh issued)."""
        self.make_user("ratelimited")
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["auth"] = "2/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            first = self.client.post(
                "/api/accounts/login/",
                {"username": "ratelimited", "password": "S3cure-Passphrase!"},
                format="json",
            )
            second = self.client.post(
                "/api/accounts/login/",
                {"username": "ratelimited", "password": "S3cure-Passphrase!"},
                format="json",
            )
            throttled = self.client.post(
                "/api/accounts/login/",
                {"username": "ratelimited", "password": "S3cure-Passphrase!"},
                format="json",
            )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertIn("access", first.data)
        self.assertIn("refresh", first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(throttled.status_code, 429, throttled.data)

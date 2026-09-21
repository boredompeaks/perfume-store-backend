"""Accounts unit tests - docs/test-gaps.md items 1-14.

Covers registration validation, email verification, enumeration-safe
recovery flows, and the V-05 password policy: registration and password
reset both run Django's ``validate_password`` (conventions.md), so a
password the shared validators reject fails both paths in the same
field-error shape.
"""
import unittest
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import SimpleTestCase, override_settings, tag
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from accounts.serializers import RegisterSerializer
from accounts.urls import urlpatterns as account_urlpatterns
from accounts.views import LoginView, _encoded_user_id, _get_user, register
from common.testing import ApiTestCase, extract_link_params
from django.core.cache import cache
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken


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
        self.assertIn("password", res.data["details"])
        self.assertIn("common", " ".join(res.data["details"]["password"]).lower())

    def test_v05_registration_rejects_numeric_only_password(self):
        """V-05 companion: all-numeric passwords are rejected by
        NumericPasswordValidator through the shared policy."""
        res = self.client.post(
            "/api/accounts/register/",
            {"username": "numericpass", "email": "numeric@example.com", "password": "987654321"},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("password", res.data["details"])
        self.assertIn("numeric", " ".join(res.data["details"]["password"]).lower())

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
                self.assertIn(
                    "similar", " ".join(serializer.errors["password"]).lower()
                )

    def test_policy_errors_match_reset_path_shape_and_block_creation(self):
        """Parity + uniform-shape contract (conventions.md): registration and
        reset-confirm both answer 400 with a list of validator messages under
        the `password` key — the shape the frontend's fieldErrors renderer
        consumes for both forms. A rejected password must also create no user
        row and send no verification email."""
        res = self.client.post(
            "/api/accounts/register/",
            {
                "username": "parity",
                "email": "parity@example.com",
                "password": "password",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIsInstance(res.data["details"]["password"], list)
        self.assertTrue(
            all(
                isinstance(message, str)
                for message in res.data["details"]["password"]
            )
        )
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
        self.assertIsInstance(reset_res.data["details"]["password"], list)
        self.assertTrue(
            all(isinstance(message, str) for message in reset_res.data["details"]["password"])
        )


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
        with mock.patch(
            "common.notifications.send_email", side_effect=Exception("smtp down")
        ):
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
        with mock.patch(
            "common.notifications.send_email", side_effect=Exception("smtp down")
        ):
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
                self.assertIn("password", res.data["details"])
                self.assertIn(
                    expected,
                    " ".join(res.data["details"]["password"]).lower(),
                )

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
    registration spam); verify-email, password-reset-confirm, the
    username-available GET oracle and token refresh join that budget,
    while the email-sending recovery flows get the tighter dedicated
    'recovery' scope — each accepted request sends an email, so the
    budget *is* the mail-bomb bound. Non-429 bodies stay uniform."""

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

    def test_all_account_urlpatterns_declare_throttle_scopes(self):
        """Wiring guard over the real URLConf: every account route —
        including the GET existence oracle and the JWT refresh route —
        must declare a scope, so a future endpoint cannot ship
        unthrottled silently."""
        expected = {
            "verify-email": "auth",
            "resend-verification": "recovery",
            "forgot-username": "recovery",
            "password-reset": "recovery",
            "password-reset-confirm": "auth",
            "username-available": "auth",
            "register": "auth",
            "login": "auth",
            "token-refresh": "auth",
            "logout": "auth",
        }
        found = {}
        for pattern in account_urlpatterns:
            scope = getattr(pattern.callback.view_class, "throttle_scope", None)
            self.assertTrue(scope, f"/{pattern.name}/ has no throttle_scope")
            found[pattern.name] = scope
        self.assertEqual(found, expected)
        self.assertIn("recovery", api_settings.DEFAULT_THROTTLE_RATES)

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
        TokenObtainPairView contract (access in the body; the refresh token
        rides the HttpOnly cookie since SPEC-17-02 [R-17.12])."""
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
        self.assertIn(COOKIE_NAME, first.cookies)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(throttled.status_code, 429, throttled.data)

    def test_verify_email_rate_limit_engages(self):
        """A second verify inside a 1/min 'auth' budget is 429'd — before
        scoping it answered an idempotent 200, giving token replay an
        unbounded probe budget."""
        user = make_inactive_user("verifyrate")
        payload = {
            "uid": _encoded_user_id(user),
            "token": default_token_generator.make_token(user),
        }
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["auth"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            first = self.client.post(
                "/api/accounts/verify-email/", payload, format="json"
            )
            throttled = self.client.post(
                "/api/accounts/verify-email/", payload, format="json"
            )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertIn("Email verified", first.data["message"])
        self.assertEqual(throttled.status_code, 429, throttled.data)

    def test_password_reset_confirm_rate_limit_engages(self):
        """The second confirm inside a 1/min 'auth' budget is 429'd; the
        one in-budget confirm still changes the password."""
        user = self.make_user("confirmrate")
        payload = {
            "uid": _encoded_user_id(user),
            "token": default_token_generator.make_token(user),
            "password": "N3w-Passphrase-77",
        }
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["auth"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            first = self.client.post(
                "/api/accounts/password-reset/confirm/", payload, format="json"
            )
            throttled = self.client.post(
                "/api/accounts/password-reset/confirm/", payload, format="json"
            )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(throttled.status_code, 429, throttled.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password("N3w-Passphrase-77"))

    def test_token_refresh_rate_limit_engages(self):
        """The second refresh inside a 1/min 'auth' budget is 429'd,
        bounding refresh-token brute forcing; the in-budget contract is
        unchanged (an access token is issued)."""
        user = self.make_user("refreshrate")
        refresh = RefreshToken.for_user(user)
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["auth"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            first = self.client.post(
                "/api/accounts/token/refresh/", {"refresh": str(refresh)}, format="json"
            )
            throttled = self.client.post(
                "/api/accounts/token/refresh/", {"refresh": str(refresh)}, format="json"
            )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertIn("access", first.data)
        self.assertEqual(throttled.status_code, 429, throttled.data)

    def test_username_available_rate_limit_engages(self):
        """The GET existence oracle is throttled too (ScopedRateThrottle
        has no safe-method exemption): the second username probe inside a
        1/min 'auth' budget is 429'd, capping cheap enumeration."""
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["auth"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            first = self.client.get(
                "/api/accounts/username-available/", {"username": "probe1"}
            )
            throttled = self.client.get(
                "/api/accounts/username-available/", {"username": "probe2"}
            )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertTrue(first.data["available"])
        self.assertEqual(throttled.status_code, 429, throttled.data)

    def test_resend_verification_rate_limit_engages(self):
        """The second resend inside a 1/min 'recovery' budget is 429'd and
        sends nothing further — the per-IP mail-bomb bound."""
        make_inactive_user("resendrate")
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["recovery"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            first = self.client.post(
                "/api/accounts/resend-verification/",
                {"email": "resendrate@example.com"},
                format="json",
            )
            throttled = self.client.post(
                "/api/accounts/resend-verification/",
                {"email": "resendrate@example.com"},
                format="json",
            )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(throttled.status_code, 429, throttled.data)
        self.assertEqual(len(mail.outbox), 1)  # throttled request triggers no send

    def test_forgot_username_rate_limit_engages(self):
        """One username email per 'recovery' budget: the in-budget request
        sends exactly one mail, the throttled replay adds none."""
        self.make_user("usernamerate")
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["recovery"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            first = self.client.post(
                "/api/accounts/forgot-username/",
                {"email": "usernamerate@example.com"},
                format="json",
            )
            throttled = self.client.post(
                "/api/accounts/forgot-username/",
                {"email": "usernamerate@example.com"},
                format="json",
            )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertIn("Your username is: usernamerate", mail.outbox[0].body)
        self.assertEqual(throttled.status_code, 429, throttled.data)
        self.assertEqual(len(mail.outbox), 1)  # throttled request triggers no send

    def test_password_reset_rate_limit_engages(self):
        """One reset email per 'recovery' budget — and the throttled
        replay stays 429'd for an unknown address too, so the limiter
        itself cannot be used to differentially probe accounts."""
        self.make_user("resetrate")
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["recovery"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            first = self.client.post(
                "/api/accounts/password-reset/",
                {"email": "resetrate@example.com"},
                format="json",
            )
            throttled = self.client.post(
                "/api/accounts/password-reset/",
                {"email": "ghost@example.com"},
                format="json",
            )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(throttled.status_code, 429, throttled.data)
        self.assertEqual(len(mail.outbox), 1)  # throttled request triggers no send

    def test_recovery_throttling_preserves_uniform_bodies(self):
        """Throttling must not alter the non-429 contract of the uniform
        anonymous recovery flows: known or unknown email alike, every
        in-budget request still answers the exact pinned 200 body."""
        self.make_user("uniformrate")
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["recovery"] = "3/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            resend = self.client.post(
                "/api/accounts/resend-verification/",
                {"email": "ghost@example.com"},
                format="json",
            )
            forgot = self.client.post(
                "/api/accounts/forgot-username/",
                {"email": "ghost@example.com"},
                format="json",
            )
            reset = self.client.post(
                "/api/accounts/password-reset/",
                {"email": "uniformrate@example.com"},
                format="json",
            )
        self.assertEqual(resend.status_code, 200, resend.data)
        self.assertEqual(
            resend.data,
            {
                "message": (
                    "If an unverified account exists, "
                    "a verification email has been sent."
                )
            },
        )
        self.assertEqual(forgot.status_code, 200, forgot.data)
        self.assertEqual(
            forgot.data,
            {
                "message": (
                    "If an account exists for this email, the username has been sent."
                )
            },
        )
        self.assertEqual(reset.status_code, 200, reset.data)
        self.assertEqual(
            reset.data,
            {
                "message": (
                    "If an active account exists for this email, "
                    "a password-reset link has been sent."
                )
            },
        )
        # only the known-address reset probe sent mail — the ghost probes
        # sent none, and throttling added no extra bodies or sends
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["uniformrate@example.com"])


@tag("accounts")
class JwtLifecycleConfigTests(SimpleTestCase):
    """SPEC-17-01 [R-17.5]: the JWT lifecycle is explicitly configured, not
    library-defaulted. Rotation + blacklist-after-rotation are mandatory
    (a non-rotating refresh token is a long-lived bearer credential), the
    blacklist app is installed, and both lifetimes are env-driven with the
    documented defaults. The env-override path is pinned separately in
    tests/test_settings_security.py (subprocess, clean environment)."""

    def test_blacklist_app_is_installed(self):
        self.assertIn(
            "rest_framework_simplejwt.token_blacklist", settings.INSTALLED_APPS
        )

    def test_rotation_and_blacklist_after_rotation_are_enabled(self):
        self.assertIs(settings.SIMPLE_JWT["ROTATE_REFRESH_TOKENS"], True)
        self.assertIs(settings.SIMPLE_JWT["BLACKLIST_AFTER_ROTATION"], True)

    def test_default_lifetimes_are_the_documented_defaults(self):
        self.assertEqual(
            settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"], timedelta(seconds=900)
        )
        self.assertEqual(
            settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"],
            timedelta(seconds=604800),
        )


@tag("accounts")
class LogoutTests(ApiTestCase):
    """SPEC-17-01 [R-17.8] Secure logout: POST /api/accounts/logout/ with a
    valid access token blacklists the presented refresh token, so the
    session cannot outlive the logout — a stolen refresh token cannot mint
    new access tokens afterwards."""

    def _auth_access(self, username):
        user = self.make_user(username)
        refresh = RefreshToken.for_user(user)
        self.auth(str(refresh.access_token))
        return refresh

    def test_logout_requires_authentication(self):
        res = self.client.post(
            "/api/accounts/logout/", {"refresh": "x"}, format="json"
        )
        self.assertEqual(res.status_code, 401, res.data)

    def test_logout_blacklists_the_presented_refresh_token(self):
        refresh = self._auth_access("leaver")
        res = self.client.post(
            "/api/accounts/logout/", {"refresh": str(refresh)}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        replay = self.client.post(
            "/api/accounts/token/refresh/", {"refresh": str(refresh)}, format="json"
        )
        self.assertEqual(replay.status_code, 401, replay.data)
        self.assertIn("blacklisted", replay.data["error"])

    def test_logout_without_refresh_token_is_rejected(self):
        self._auth_access("nologout")
        for payload in ({}, {"refresh": ""}, {"refresh": 123}):
            with self.subTest(payload=payload):
                res = self.client.post("/api/accounts/logout/", payload, format="json")
                self.assertEqual(res.status_code, 400, res.data)
                self.assertIn("refresh", res.data["details"])

    def test_logout_garbage_refresh_token_is_rejected(self):
        self._auth_access("garbagelogout")
        res = self.client.post(
            "/api/accounts/logout/", {"refresh": "not-a-jwt"}, format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("refresh", res.data["details"])


@tag("accounts")
class RefreshRotationTests(ApiTestCase):
    """SPEC-17-01 [R-17.5]: ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION —
    every refresh mints a new refresh token in the response and the
    presented one is blacklisted, so a replayed refresh token is dead on
    arrival while the legitimate client keeps a fresh pair."""

    def test_refresh_rotates_and_blacklists_the_presented_token(self):
        """SPEC-17-01 semantics re-pinned by SPEC-17-02: a body-presented
        refresh token still rotates + blacklists (non-browser API clients),
        but the rotated token now rides the HttpOnly cookie instead of the
        response body."""
        user = self.make_user("rotator")
        original = RefreshToken.for_user(user)
        res = self.client.post(
            "/api/accounts/token/refresh/", {"refresh": str(original)}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)
        self.assertNotIn("refresh", res.data)
        rotated = self.client.cookies[COOKIE_NAME].value
        self.assertNotEqual(rotated, str(original))
        replay = self.client.post(
            "/api/accounts/token/refresh/", {"refresh": str(original)}, format="json"
        )
        self.assertEqual(replay.status_code, 401, replay.data)
        self.assertIn("blacklisted", replay.data["error"])
        second = self.client.post(
            "/api/accounts/token/refresh/", {"refresh": rotated}, format="json"
        )
        self.assertEqual(second.status_code, 200, second.data)


@tag("accounts")
class SessionInvalidationOnPasswordResetTests(ApiTestCase):
    """SPEC-17-01 [R-17.10] Session invalidation after critical account
    changes: confirming a password reset blacklists every outstanding
    refresh token issued to the account (all concurrent sessions die with
    it), while other accounts' sessions are untouched."""

    def _confirm_reset(self, user, password="N3w-Passphrase-77"):
        return self.client.post(
            "/api/accounts/password-reset/confirm/",
            {
                "uid": _encoded_user_id(user),
                "token": default_token_generator.make_token(user),
                "password": password,
            },
            format="json",
        )

    def test_password_reset_blacklists_outstanding_refresh_tokens(self):
        user = self.make_user("resetrotator")
        outstanding = RefreshToken.for_user(user)
        res = self._confirm_reset(user)
        self.assertEqual(res.status_code, 200, res.data)
        replay = self.client.post(
            "/api/accounts/token/refresh/",
            {"refresh": str(outstanding)},
            format="json",
        )
        self.assertEqual(replay.status_code, 401, replay.data)
        self.assertIn("blacklisted", replay.data["error"])

    def test_password_reset_leaves_other_users_sessions_intact(self):
        reset_user = self.make_user("resetone")
        bystander = self.make_user("bystander")
        bystander_refresh = RefreshToken.for_user(bystander)
        res = self._confirm_reset(reset_user)
        self.assertEqual(res.status_code, 200, res.data)
        ok = self.client.post(
            "/api/accounts/token/refresh/",
            {"refresh": str(bystander_refresh)},
            format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.data)

    def test_password_reset_with_no_outstanding_tokens_succeeds(self):
        user = self.make_user("tokenless")
        res = self._confirm_reset(user)
        self.assertEqual(res.status_code, 200, res.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password("N3w-Passphrase-77"))


COOKIE_NAME = settings.JWT_REFRESH_COOKIE_NAME


@tag("accounts")
class RefreshCookieConfigTests(SimpleTestCase):
    """SPEC-17-02 [R-17.12]: the refresh-token cookie contract is explicit,
    not library-defaulted. Name/Path/SameSite are env-driven settings; Secure
    follows DEBUG (V-02 fails closed), HttpOnly is unconditional — browser
    JS must never be able to read the refresh token (spec §17.1)."""

    def test_cookie_settings_have_documented_defaults(self):
        self.assertEqual(COOKIE_NAME, "refresh_token")
        # Path covers both mounts of accounts/urls.py: legacy /api/accounts/
        # and the v1 namespace /api/v1/account/.
        self.assertEqual(settings.JWT_REFRESH_COOKIE_PATH, "/api/")
        self.assertEqual(settings.JWT_REFRESH_COOKIE_SAMESITE, "Lax")


@tag("accounts")
class RefreshCookieTests(ApiTestCase):
    """SPEC-17-02 [R-17.12]: the refresh token leaves the JSON body (and
    localStorage) and rides an HttpOnly backend-set cookie. Login sets it,
    refresh re-sets it on rotation, logout blacklists it and clears it."""

    def _login(self, username="cookiebuyer", password="S3cure-Passphrase!"):
        self.make_user(username, password=password)
        res = self.client.post(
            "/api/accounts/login/",
            {"username": username, "password": password},
            format="json",
        )
        # Logout requires an authenticated caller; the login response's
        # access token (kept memory-only per R-17.12) doubles as the
        # fixture's auth header.
        self.auth(res.data["access"])
        return res

    def test_login_sets_httponly_cookie_and_drops_body_refresh(self):
        res = self._login()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)
        # The body is the leak surface: a refresh token returned as JSON is
        # readable by any injected script, exactly what R-17.12 forbids.
        self.assertNotIn("refresh", res.data)
        cookie = res.cookies[COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"], "tests run with DEBUG=False")
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], settings.JWT_REFRESH_COOKIE_PATH)
        # Cookie lifetime is synced to the refresh-token lifetime, so the
        # browser never carries a cookie older than the token it holds.
        self.assertEqual(
            cookie["max-age"],
            int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()),
        )

    def test_login_cookie_omits_secure_flag_in_debug(self):
        with override_settings(DEBUG=True):
            res = self._login("debugbuyer")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.cookies[COOKIE_NAME]["secure"])

    def test_refresh_via_cookie_rotates_and_re_sets_cookie(self):
        self._login()
        original = self.client.cookies[COOKIE_NAME].value
        res = self.client.post("/api/accounts/token/refresh/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)
        self.assertNotIn("refresh", res.data)
        rotated = self.client.cookies[COOKIE_NAME].value
        self.assertNotEqual(rotated, original)
        # The fresh cookie keeps the session alive end-to-end (empty body:
        # the backend must find the token in its own cookie).
        next = self.client.post("/api/accounts/token/refresh/", {}, format="json")
        self.assertEqual(next.status_code, 200, next.data)
        # Rotation interplay [R-17.5]: the presented cookie token dies even
        # though it never appeared in a request body; the rejection also
        # sweeps the now-stale cookie (401's Set-Cookie wins the jar).
        replay = self.client.post(
            "/api/accounts/token/refresh/", {"refresh": original}, format="json"
        )
        self.assertEqual(replay.status_code, 401, replay.data)
        self.assertIn("blacklisted", replay.data["error"])
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, "")

    def test_refresh_without_cookie_or_body_is_rejected(self):
        res = self.client.post("/api/accounts/token/refresh/", {}, format="json")
        self.assertEqual(res.status_code, 400, res.data)

    def test_refresh_with_dead_cookie_sweeps_the_cookie(self):
        self._login("sweeper")
        self.client.cookies[COOKIE_NAME] = "not-a-jwt"
        res = self.client.post("/api/accounts/token/refresh/", {}, format="json")
        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, "")

    def test_logout_with_cookie_blacklists_and_clears_it(self):
        self._login()
        token = self.client.cookies[COOKIE_NAME].value
        res = self.client.post("/api/accounts/logout/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, "")
        replay = self.client.post(
            "/api/accounts/token/refresh/", {"refresh": token}, format="json"
        )
        self.assertEqual(replay.status_code, 401, replay.data)
        self.assertIn("blacklisted", replay.data["error"])

    def test_logout_with_garbage_cookie_rejects_and_sweeps_it(self):
        self._login("garbage-cookie")
        self.client.cookies[COOKIE_NAME] = "not-a-jwt"
        res = self.client.post("/api/accounts/logout/", {}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("refresh", res.data["details"])
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, "")

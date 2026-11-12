import os
import time
import unittest
from datetime import date, timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.core.signing import TimestampSigner
from django.test import SimpleTestCase, override_settings, tag
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import Address
from accounts.serializers import RegisterSerializer
from accounts.urls import urlpatterns as account_urlpatterns
from accounts.views import (
    LoginView,
    _encoded_user_id,
    _get_user,
    register,
    username_available,
)
from common.testing import ApiTestCase, extract_link_params
from config.settings import _env_iso_date


def make_inactive_user(username="pending", email=None):
    email = email or f"{username}@example.com"
    serializer = RegisterSerializer(
        data={"username": username, "email": email, "password": "S3cure-Passphrase!"}
    )
    serializer.is_valid(raise_exception=True)
    return serializer.save()


@tag("accounts")
class RegisterSerializerTests(ApiTestCase):
    def test_valid_data_creates_inactive_user(self):
        serializer = RegisterSerializer(
            data={
                "username": "newuser",
                "email": "new@example.com",
                "password": "S3cure-Passphrase!",
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        user = serializer.save()

        self.assertIsInstance(user, User)
        self.assertFalse(user.is_active)
        self.assertEqual(user.username, "newuser")
        self.assertEqual(user.email, "new@example.com")
        self.assertNotEqual(user.password, "S3cure-Passphrase!")
        self.assertTrue(user.check_password("S3cure-Passphrase!"))

    def test_duplicate_username_case_insensitive_rejected(self):
        User.objects.create_user("alice", "alice@example.com", "S3cure-Passphrase!")
        for candidate in ("alice", "ALICE", "Alice"):
            serializer = RegisterSerializer(
                data={
                    "username": candidate,
                    "email": "other@example.com",
                    "password": "S3cure-Passphrase!",
                }
            )
            self.assertFalse(serializer.is_valid(), candidate)
            self.assertIn("username", serializer.errors)

        self.assertEqual(User.objects.filter(username__iexact="alice").count(), 1)

    def test_duplicate_email_case_insensitive_rejected(self):
        User.objects.create_user("alice", "alice@example.com", "S3cure-Passphrase!")
        for candidate in ("alice@example.com", "ALICE@EXAMPLE.COM"):
            serializer = RegisterSerializer(
                data={
                    "username": f"user_{candidate}",
                    "email": candidate,
                    "password": "S3cure-Passphrase!",
                }
            )
            self.assertFalse(serializer.is_valid(), candidate)
            self.assertIn("email", serializer.errors)
            self.assertIn("already uses this email", str(serializer.errors["email"]))

    def test_blank_email_rejected(self):
        serializer = RegisterSerializer(
            data={
                "username": "blankmail",
                "email": "",
                "password": "S3cure-Passphrase!",
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("email", serializer.errors)

    def test_short_password_rejected(self):
        serializer = RegisterSerializer(
            data={
                "username": "shorty",
                "email": "shorty@example.com",
                "password": "short12",
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("password", serializer.errors)
        self.assertIn("at least 8 characters", str(serializer.errors["password"]))

    def test_missing_username_or_password_rejected(self):
        for missing in ("username", "password"):
            data = {
                "username": "u",
                "email": "u@example.com",
                "password": "S3cure-Passphrase!",
            }
            data.pop(missing)
            serializer = RegisterSerializer(data=data)
            self.assertFalse(serializer.is_valid(), missing)
            self.assertIn(missing, serializer.errors)

    @unittest.expectedFailure
    def test_registration_gap_missing_email_currently_accepted(self):
        """User.email is blank=True, so DRF treats it as optional and creates an account that can never verify."""
        serializer = RegisterSerializer(
            data={"username": "noemail", "password": "S3cure-Passphrase!"}
        )
        self.assertFalse(serializer.is_valid(), serializer.errors)

    def test_response_never_returns_password(self):
        res = self.client.post(
            "/api/accounts/register/",
            {
                "username": "shapecheck",
                "email": "shape@example.com",
                "password": "S3cure-Passphrase!",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            res.data["user"],
            {
                "username": "shapecheck",
                "email": "shape@example.com",
            },
        )
        self.assertNotIn("password", res.data["user"])
        self.assertNotIn("id", res.data["user"])

    def test_v05_registration_rejects_common_password(self):
        res = self.client.post(
            "/api/accounts/register/",
            {
                "username": "weakpass",
                "email": "weak@example.com",
                "password": "password",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("password", res.data["details"])
        self.assertIn("common", " ".join(res.data["details"]["password"]).lower())

    def test_v05_registration_rejects_numeric_only_password(self):
        res = self.client.post(
            "/api/accounts/register/",
            {
                "username": "numericpass",
                "email": "numeric@example.com",
                "password": "987654321",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("password", res.data["details"])
        self.assertIn("numeric", " ".join(res.data["details"]["password"]).lower())

    def test_password_similar_to_submitted_attributes_rejected(self):
        cases = {
            ("hannelore", "hannelore@example.com"): "hannelorepass",
            ("seafoam", "seafoam@example.com"): "seafoam@example.com",
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
            all(isinstance(message, str) for message in res.data["details"]["password"])
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
            all(
                isinstance(message, str)
                for message in reset_res.data["details"]["password"]
            )
        )


@tag("accounts")
class RegisterViewTests(ApiTestCase):
    def test_smtp_failure_returns_503_and_account_still_created(self):
        with mock.patch(
            "accounts.views._send_verification_email",
            side_effect=Exception("smtp down"),
        ):
            res = self.client.post(
                "/api/accounts/register/",
                {
                    "username": "nomail",
                    "email": "nomail@example.com",
                    "password": "S3cure-Passphrase!",
                },
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
                {
                    "username": "taken2",
                    "email": "taken@example.com",
                    "password": "S3cure-Passphrase!",
                },
                format="json",
            )
        self.assertEqual(res.status_code, 400, res.data)
        send_mock.assert_not_called()


@tag("accounts")
class VerifyEmailTests(ApiTestCase):
    def test_valid_token_activates_user(self):
        user = make_inactive_user("activateme")

        res = self.client.post(
            "/api/accounts/verify-email/",
            {
                "uid": _encoded_user_id(user),
                "token": default_token_generator.make_token(user),
            },
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("Email verified", res.data["message"])
        user.refresh_from_db()
        self.assertTrue(user.is_active)

        res = self.client.post(
            "/api/accounts/verify-email/",
            {
                "uid": _encoded_user_id(user),
                "token": default_token_generator.make_token(user),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

    def test_invalid_token_rejected(self):
        user = make_inactive_user("paranoid")

        for payload in (
            {"uid": _encoded_user_id(user), "token": "wrong-token"},
            {"uid": "!!!not-base64!!!", "token": "whatever"},
            {"uid": urlsafe_base64_encode(force_bytes("999999")), "token": "whatever"},
            {"uid": _encoded_user_id(user), "token": ""},
            {},
        ):
            res = self.client.post(
                "/api/accounts/verify-email/", payload, format="json"
            )
            self.assertEqual(res.status_code, 400, payload)
            self.assertIn("invalid or expired", res.data["error"])

        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_token_from_another_user_rejected(self):
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
    def test_unknown_email_uniform_response_no_email_sent(self):
        res = self.client.post(
            "/api/accounts/resend-verification/",
            {"email": "ghost@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data["message"],
            "If an unverified account exists, a verification email has been sent.",
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_missing_email_uniform_response(self):
        res = self.client.post("/api/accounts/resend-verification/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_inactive_account_receives_verification_email(self):
        user = make_inactive_user("resendme")

        res = self.client.post(
            "/api/accounts/resend-verification/",
            {"email": "resendme@example.com"},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        uid, token = extract_link_params(mail.outbox[0].body, "verify-email")
        res = self.client.post(
            "/api/accounts/verify-email/", {"uid": uid, "token": token}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(User.objects.get(username="resendme").is_active)

    def test_active_account_gets_no_email(self):
        self.make_user("alreadyactive")
        res = self.client.post(
            "/api/accounts/resend-verification/",
            {"email": "alreadyactive@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_smtp_failure_returns_503(self):
        make_inactive_user("smtpfail")
        with mock.patch(
            "accounts.views._send_verification_email",
            side_effect=Exception("smtp down"),
        ):
            res = self.client.post(
                "/api/accounts/resend-verification/",
                {"email": "smtpfail@example.com"},
                format="json",
            )
        self.assertEqual(res.status_code, 503, res.data)
        self.assertIn("could not be sent", res.data["error"])


@tag("accounts")
class ForgotUsernameTests(ApiTestCase):
    def test_unknown_email_uniform_response(self):
        res = self.client.post(
            "/api/accounts/forgot-username/",
            {"email": "ghost@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data["message"],
            "If an account exists for this email, the username has been sent.",
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_known_email_receives_username(self):
        self.make_user("rememberme")
        res = self.client.post(
            "/api/accounts/forgot-username/",
            {"email": "rememberme@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Your username is: rememberme", mail.outbox[0].body)

    def test_smtp_failure_returns_503(self):
        self.make_user("smtpfail")
        with mock.patch(
            "common.notifications.send_email", side_effect=Exception("smtp down")
        ):
            res = self.client.post(
                "/api/accounts/forgot-username/",
                {"email": "smtpfail@example.com"},
                format="json",
            )
        self.assertEqual(res.status_code, 503, res.data)


@tag("accounts")
class PasswordResetTests(ApiTestCase):
    def test_inactive_account_gets_no_reset_email(self):
        make_inactive_user("frozen", "frozen@example.com")
        res = self.client.post(
            "/api/accounts/password-reset/",
            {"email": "frozen@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_active_account_receives_reset_link(self):
        user = self.make_user("resetme")
        res = self.client.post(
            "/api/accounts/password-reset/",
            {"email": "resetme@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 1)
        uid, token = extract_link_params(mail.outbox[0].body, "reset-password")
        self.assertEqual(uid, _encoded_user_id(user))
        self.assertTrue(default_token_generator.check_token(user, token))

    def test_uniform_response_regardless_of_account_state(self):
        known = self.client.post(
            "/api/accounts/password-reset/",
            {"email": "ghost@example.com"},
            format="json",
        )
        self.make_user("knownuser")
        unknown = self.client.post(
            "/api/accounts/password-reset/",
            {"email": "knownuser@example.com"},
            format="json",
        )
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.data["message"], unknown.data["message"])

    def test_smtp_failure_returns_503(self):
        self.make_user("smtpfail")
        with mock.patch(
            "common.notifications.send_email", side_effect=Exception("smtp down")
        ):
            res = self.client.post(
                "/api/accounts/password-reset/",
                {"email": "smtpfail@example.com"},
                format="json",
            )
        self.assertEqual(res.status_code, 503, res.data)
        self.assertEqual(len(mail.outbox), 0)

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
                res = self.client.post(
                    "/api/accounts/password-reset/confirm/",
                    {**payload, "password": password},
                    format="json",
                )
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
            res = self.client.post(
                "/api/accounts/password-reset/confirm/", payload, format="json"
            )
            self.assertEqual(res.status_code, 400, payload)
            self.assertIn("invalid or expired", res.data["error"])
        user.refresh_from_db()
        self.assertTrue(user.check_password("S3cure-Passphrase!"))

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
    def test_too_short_username_reports_unavailable(self):
        for candidate in ("", "a", "ab", "  x  "):
            with self.subTest(candidate=candidate):
                res = self.client.get(
                    "/api/accounts/username-available/", {"username": candidate}
                )
                self.assertEqual(res.status_code, 200, res.data)
                self.assertFalse(res.data["available"])
                self.assertIn("at least 3 characters", res.data["message"])

    def test_taken_username_case_insensitive(self):
        self.make_user("buyer")
        for candidate in ("buyer", "BUYER", "Buyer"):
            with self.subTest(candidate=candidate):
                res = self.client.get(
                    "/api/accounts/username-available/", {"username": candidate}
                )
                self.assertFalse(res.data["available"])
                self.assertIn("already taken", res.data["message"])

    def test_free_username_reports_available(self):
        res = self.client.get(
            "/api/accounts/username-available/", {"username": "freshname"}
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["available"])
        self.assertIn("available", res.data["message"])

    def test_missing_query_param_reports_unavailable(self):
        res = self.client.get("/api/accounts/username-available/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["available"])


@tag("accounts")
class AccessChallengeTests(ApiTestCase):
    SECRET = "q-s3-challenge-secret"
    # The token format is signed with a local copy: the mint and validate halves must agree character-for-character.
    SALT = "accounts.access-challenge"
    VALUE = "accounts-access"
    REFUSAL = "Access challenge required."

    def _mint(self, secret=None, value=None, age=0):
        signer = TimestampSigner(key=secret or self.SECRET, salt=self.SALT)
        payload = value or self.VALUE
        if not age:
            return signer.sign(payload)
        # Backdate the signer's stamp so unsign() sees a token older than max_age; the reference time is read before the patch.
        past = time.time() - age
        with mock.patch("django.core.signing.time.time", return_value=past):
            return signer.sign(payload)

    def _register(self, **extra):
        payload = {
            "username": "gateuser",
            "email": "gateuser@example.com",
            "password": "S3cure-Passphrase!",
        }
        payload.update(extra)
        return self.client.post("/api/accounts/register/", payload, format="json")

    def _probe(self, username="gateuser", **extra):
        params = {"username": username}
        params.update(extra)
        return self.client.get("/api/accounts/username-available/", params)

    def test_zero_config_default_requires_no_challenge(self):
        self.assertEqual(settings.ACCOUNT_CHALLENGE_SECRET, "")
        self.assertEqual(settings.ACCOUNT_CHALLENGE_MAX_AGE, 300)
        res = self._register()
        self.assertEqual(res.status_code, 201, res.data)
        probe = self._probe()
        self.assertEqual(probe.status_code, 200, probe.data)
        self.assertFalse(probe.data["available"])

    def test_missing_challenge_is_refused_identically_on_both_endpoints(self):
        with override_settings(ACCOUNT_CHALLENGE_SECRET=self.SECRET):
            refused_register = self._register()
            refused_probe = self._probe(username="ghostprobe")
        for res in (refused_register, refused_probe):
            self.assertEqual(res.status_code, 403, res.data)
            self.assertEqual(res.data["error"], self.REFUSAL)
            self.assertEqual(res.data["code"], "permission_denied")
        self.assertEqual(refused_register.data, refused_probe.data)
        self.assertFalse(User.objects.filter(username="gateuser").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_valid_signed_challenge_admits_both_endpoints(self):
        token = self._mint()
        with override_settings(ACCOUNT_CHALLENGE_SECRET=self.SECRET):
            res = self._register(challenge=token)
            probe = self._probe(challenge=token)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(probe.status_code, 200, probe.data)
        self.assertFalse(probe.data["available"])

    def test_challenge_rides_the_query_string_too(self):
        token = self._mint()
        with override_settings(ACCOUNT_CHALLENGE_SECRET=self.SECRET):
            res = self.client.post(
                f"/api/accounts/register/?challenge={token}",
                {
                    "username": "gateuser",
                    "email": "gateuser@example.com",
                    "password": "S3cure-Passphrase!",
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.data)

    def test_bad_tokens_all_share_one_refusal(self):
        garbage = "not-a-signed-token"
        cases = {
            "missing": None,
            "garbage": garbage,
            "foreign secret": self._mint(secret="attacker-key"),
            "wrong payload": self._mint(value="something-else"),
            "tampered": self._mint() + "x",
            "non-string challenge": 123,
        }
        with override_settings(ACCOUNT_CHALLENGE_SECRET=self.SECRET):
            for label, token in cases.items():
                with self.subTest(case=label):
                    # Six cases against the 5/min budget: clear the throttle between them so each lands first in its own window.
                    cache.clear()
                    res = self._register(challenge=token)
                    self.assertEqual(res.status_code, 403, res.data)
                    self.assertEqual(res.data["error"], self.REFUSAL)
                    self.assertFalse(User.objects.filter(username="gateuser").exists())

    def test_stale_challenge_is_refused(self):
        stale = self._mint(age=10_000)
        with override_settings(ACCOUNT_CHALLENGE_SECRET=self.SECRET):
            res = self._register(challenge=stale)
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["error"], self.REFUSAL)

    def test_max_age_below_one_keeps_only_the_signature_required(self):
        stale = self._mint(age=10_000)
        with override_settings(
            ACCOUNT_CHALLENGE_SECRET=self.SECRET, ACCOUNT_CHALLENGE_MAX_AGE=0
        ):
            res = self._register(challenge=stale)
        self.assertEqual(res.status_code, 201, res.data)

    def test_the_throttle_budget_still_gates_before_the_challenge(self):
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["register"] = "1/min"
        with override_settings(ACCOUNT_CHALLENGE_SECRET=self.SECRET):
            with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
                cache.clear()
                first = self._register()
                second = self._register()
        self.assertEqual(first.status_code, 403, first.data)
        self.assertEqual(second.status_code, 429, second.data)


@tag("accounts")
class AuthThrottleTests(ApiTestCase):
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
        self.assertEqual(register.view_class.throttle_scope, "register")
        self.assertEqual(username_available.view_class.throttle_scope, "username-probe")
        self.assertEqual(LoginView.throttle_scope, "auth")
        self.assertIn(ScopedRateThrottle, LoginView.throttle_classes)
        self.assertIn("auth", api_settings.DEFAULT_THROTTLE_RATES)
        self.assertEqual(api_settings.DEFAULT_THROTTLE_RATES["register"], "5/min")
        self.assertEqual(api_settings.DEFAULT_THROTTLE_RATES["username-probe"], "5/min")

        def per_minute(rate):
            count, period = rate.split("/")
            self.assertEqual(period, "min")
            return int(count)

        auth_budget = per_minute(api_settings.DEFAULT_THROTTLE_RATES["auth"])
        for scope in ("register", "username-probe"):
            with self.subTest(scope=scope):
                self.assertLess(
                    per_minute(api_settings.DEFAULT_THROTTLE_RATES[scope]),
                    auth_budget,
                )

    def test_all_account_urlpatterns_declare_throttle_scopes(self):
        expected = {
            "verify-email": "auth",
            "resend-verification": "recovery",
            "forgot-username": "recovery",
            "password-reset": "recovery",
            "password-reset-confirm": "auth",
            "username-available": "username-probe",
            "register": "register",
            "login": "auth",
            "storefront-login": "auth",
            "token-refresh": "auth",
            "logout": "auth",
            "mfa-status": "auth",
            "mfa-setup": "auth",
            "mfa-confirm": "auth",
            "mfa-disable": "auth",
            "mfa-trust": "auth",
            "address-list": "auth",
            "address-detail": "auth",
        }
        found = {}
        for pattern in account_urlpatterns:
            callback = pattern.callback
            # A plain DRF view exposes ``view_class``; a ViewSet's as_view() exposes ``cls``.
            view_class = getattr(callback, "view_class", None) or getattr(
                callback, "cls", None
            )
            scope = getattr(view_class, "throttle_scope", None)
            self.assertTrue(scope, f"/{pattern.name}/ has no throttle_scope")
            found[pattern.name] = scope
        self.assertEqual(found, expected)
        self.assertIn("recovery", api_settings.DEFAULT_THROTTLE_RATES)

    def test_register_rate_limit_engages(self):
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["register"] = "1/min"
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", rates):
            cache.clear()
            self.assertEqual(self._register("first").status_code, 201)
            throttled = self._register("second")
        self.assertEqual(throttled.status_code, 429, throttled.data)
        self.assertFalse(User.objects.filter(username="second").exists())

    def test_login_rate_limit_engages(self):
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
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["username-probe"] = "1/min"
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
        self.assertEqual(len(mail.outbox), 1)

    def test_forgot_username_rate_limit_engages(self):
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
        self.assertEqual(len(mail.outbox), 1)

    def test_password_reset_rate_limit_engages(self):
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
        self.assertEqual(len(mail.outbox), 1)

    def test_recovery_throttling_preserves_uniform_bodies(self):
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
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["uniformrate@example.com"])


@tag("accounts")
class JwtLifecycleConfigTests(SimpleTestCase):
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
    def _auth_access(self, username):
        user = self.make_user(username)
        refresh = RefreshToken.for_user(user)
        self.auth(str(refresh.access_token))
        return refresh

    def test_logout_requires_authentication(self):
        res = self.client.post("/api/accounts/logout/", {"refresh": "x"}, format="json")
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
    def test_refresh_rotates_and_blacklists_the_presented_token(self):
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
    def test_cookie_settings_have_documented_defaults(self):
        self.assertEqual(COOKIE_NAME, "refresh_token")
        # Covers both mounts: legacy /api/accounts/ and the v1 namespace /api/v1/account/.
        self.assertEqual(settings.JWT_REFRESH_COOKIE_PATH, "/api/")
        self.assertEqual(settings.JWT_REFRESH_COOKIE_SAMESITE, "Lax")


@tag("accounts")
class RefreshCookieTests(ApiTestCase):
    def _login(self, username="cookiebuyer", password="S3cure-Passphrase!"):
        self.make_user(username, password=password)
        res = self.client.post(
            "/api/accounts/login/",
            {"username": username, "password": password},
            format="json",
        )
        self.auth(res.data["access"])
        return res

    def test_login_sets_httponly_cookie_and_drops_body_refresh(self):
        res = self._login()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)
        self.assertNotIn("refresh", res.data)
        cookie = res.cookies[COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"], "tests run with DEBUG=False")
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], settings.JWT_REFRESH_COOKIE_PATH)
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
        next = self.client.post("/api/accounts/token/refresh/", {}, format="json")
        self.assertEqual(next.status_code, 200, next.data)
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


@tag("accounts")
class RefreshBodyWindowConfigTests(SimpleTestCase):
    def test_window_defaults_are_open_and_generous(self):
        self.assertTrue(settings.JWT_REFRESH_BODY_ACCEPT_ENABLED)
        self.assertEqual(settings.JWT_REFRESH_BODY_ACCEPT_START, date(2026, 10, 15))
        self.assertEqual(settings.JWT_REFRESH_BODY_ACCEPT_DAYS, 90)

    def test_window_anchor_env_parsing_is_fail_safe(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_REFRESH_ANCHOR", None)
            self.assertEqual(
                _env_iso_date("TEST_REFRESH_ANCHOR", date(2026, 10, 15)),
                date(2026, 10, 15),
            )
            os.environ["TEST_REFRESH_ANCHOR"] = "2030-01-02"
            self.assertEqual(
                _env_iso_date("TEST_REFRESH_ANCHOR", date(2026, 10, 15)),
                date(2030, 1, 2),
            )
            os.environ["TEST_REFRESH_ANCHOR"] = "not-a-date"
            self.assertEqual(
                _env_iso_date("TEST_REFRESH_ANCHOR", date(2026, 10, 15)),
                date(2026, 10, 15),
            )

    def test_window_knobs_are_documented_in_env_example(self):
        with open(".env.example", encoding="utf-8") as handle:
            example = handle.read()
        for key in (
            "JWT_REFRESH_BODY_ACCEPT_ENABLED",
            "JWT_REFRESH_BODY_ACCEPT_START",
            "JWT_REFRESH_BODY_ACCEPT_DAYS",
        ):
            self.assertIn(key, example)


@tag("accounts")
class RefreshDualAcceptAndOriginTests(ApiTestCase):
    REFRESH = "/api/accounts/token/refresh/"

    def _login(self, username="seambuyer"):
        self.make_user(username, password="S3cure-Passphrase!")
        res = self.client.post(
            "/api/accounts/login/",
            {"username": username, "password": "S3cure-Passphrase!"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res

    def _legacy_refresh(self, username="legacybuyer"):
        user = self.make_user(username)
        return str(RefreshToken.for_user(user))

    def test_refresh_issues_the_token_only_as_an_httponly_secure_cookie(self):
        self._login()
        original = self.client.cookies[COOKIE_NAME].value
        res = self.client.post(self.REFRESH, {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn("refresh", res.data)
        cookie = res.cookies[COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"], "tests run with DEBUG=False")
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], settings.JWT_REFRESH_COOKIE_PATH)
        self.assertNotEqual(cookie.value, original)

    def test_cross_origin_refresh_is_rejected_and_the_cookie_survives(self):
        self._login("originbuyer")
        original = self.client.cookies[COOKIE_NAME].value
        real_lock = OutstandingToken.objects.select_for_update
        with mock.patch(
            "accounts.views.OutstandingToken.objects.select_for_update",
            wraps=real_lock,
        ) as lock:
            res = self.client.post(
                self.REFRESH, {}, format="json", HTTP_ORIGIN="https://evil.example"
            )
        self.assertEqual(res.status_code, 403, res.data)
        self.assertNotIn("access", res.data)
        self.assertEqual(self.client.cookies[COOKIE_NAME].value, original)
        lock.assert_not_called()

    def test_origin_gate_wins_over_the_window_gate(self):
        # A foreign origin gets 403 (the origin verdict) before the carrier question is asked (400).
        token = self._legacy_refresh("evilbody")
        with override_settings(JWT_REFRESH_BODY_ACCEPT_ENABLED=False):
            res = self.client.post(
                self.REFRESH,
                {"refresh": token},
                format="json",
                HTTP_ORIGIN="https://evil.example",
            )
        self.assertEqual(res.status_code, 403, res.data)

    def test_sandboxed_null_origin_is_rejected(self):
        self._login("nullorigin")
        res = self.client.post(self.REFRESH, {}, format="json", HTTP_ORIGIN="null")
        self.assertEqual(res.status_code, 403, res.data)

    def test_same_origin_and_trusted_origin_refreshes_are_accepted(self):
        self._login("trustedorigin")
        same = self.client.post(
            self.REFRESH, {}, format="json", HTTP_ORIGIN="http://testserver"
        )
        self.assertEqual(same.status_code, 200, same.data)
        trusted = self.client.post(
            self.REFRESH,
            {},
            format="json",
            HTTP_ORIGIN=settings.CSRF_TRUSTED_ORIGINS[0],
        )
        self.assertEqual(trusted.status_code, 200, trusted.data)

    def test_legacy_body_token_is_accepted_while_the_window_is_open(self):
        token = self._legacy_refresh()
        res = self.client.post(self.REFRESH, {"refresh": token}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)
        self.assertNotIn("refresh", res.data)
        self.assertTrue(self.client.cookies[COOKIE_NAME]["httponly"])

    def test_non_json_body_is_not_the_cookie_surface(self):
        # The cookie merge has never applied to a non-dict carrier, so a list body goes straight to serializer validation.
        self._login("listbody")
        res = self.client.post(self.REFRESH, ["not-a-dict"], format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertNotIn("access", res.data)

    def test_body_token_is_refused_once_the_window_flag_is_closed(self):
        token = self._legacy_refresh("flagclosed")
        with override_settings(JWT_REFRESH_BODY_ACCEPT_ENABLED=False):
            res = self.client.post(self.REFRESH, {"refresh": token}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertNotIn("access", res.data)
        self.assertIn("refresh", res.data["details"])
        self.assertIn(
            "Legacy body refresh tokens are no longer accepted.",
            res.data["details"]["refresh"][0],
        )

    def test_body_token_is_refused_after_the_window_deadline(self):
        token = self._legacy_refresh("pastwindow")
        with override_settings(
            JWT_REFRESH_BODY_ACCEPT_START=date(2026, 1, 1),
            JWT_REFRESH_BODY_ACCEPT_DAYS=7,
        ):
            res = self.client.post(self.REFRESH, {"refresh": token}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertNotIn("access", res.data)

    def test_cookie_refresh_still_works_when_the_body_window_is_closed(self):
        self._login("cookieafterclose")
        with override_settings(JWT_REFRESH_BODY_ACCEPT_ENABLED=False):
            res = self.client.post(self.REFRESH, {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)
        self.assertNotIn("refresh", res.data)

    def test_closed_window_prefers_the_cookie_over_a_stray_body_token(self):
        self._login("bothcarriers")
        with override_settings(JWT_REFRESH_BODY_ACCEPT_ENABLED=False):
            res = self.client.post(
                self.REFRESH, {"refresh": "not-a-jwt"}, format="json"
            )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data)

    def test_successful_refresh_locks_the_presented_token_row(self):
        # simplejwt's check-then-blacklist is not atomic, so the presented token's OutstandingToken row is locked FOR UPDATE; a concurrent replay then dies blacklisted.
        self._login("lockbuyer")
        real_lock = OutstandingToken.objects.select_for_update
        with mock.patch(
            "accounts.views.OutstandingToken.objects.select_for_update",
            wraps=real_lock,
        ) as lock:
            res = self.client.post(self.REFRESH, {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        lock.assert_called_once()


@tag("accounts")
class AddressApiTests(ApiTestCase):
    LIST = "/api/v1/account/addresses/"
    LEGACY_LIST = "/api/accounts/addresses/"

    def setUp(self):
        self.user = self.make_user()
        self.api_login(self.user.username)

    def payload(self, **overrides):
        data = {
            "label": "Home",
            "line1": "12 Rose Lane",
            "line2": "Apt 4",
            "city": "Mumbai",
            "state": "Maharashtra",
            "postal_code": "400001",
            "country": "India",
            "phone": "+91 98765 43210",
            "is_default": False,
        }
        data.update(overrides)
        return data

    def detail(self, pk, base=None):
        return f"{base or self.LIST}{pk}/"

    def create(self, data=None, base=None, client=None):
        return (client or self.client).post(
            base or self.LIST, data or self.payload(), format="json"
        )

    def errors(self, res):
        return res.data.get("details", res.data)

    def test_anonymous_requests_are_401(self):
        anon = self.fresh_client()
        self.assertEqual(anon.get(self.LIST).status_code, 401)
        self.assertEqual(
            anon.post(self.LIST, self.payload(), format="json").status_code, 401
        )
        self.assertEqual(anon.get(self.LEGACY_LIST).status_code, 401)

    def test_create_read_update_delete_round_trip(self):
        res = self.create()
        self.assertEqual(res.status_code, 201, res.data)
        pk = res.data["id"]
        self.assertEqual(res.data["line1"], "12 Rose Lane")
        self.assertFalse(res.data["is_default"])
        self.assertNotIn("user", res.data)

        res = self.client.get(self.detail(pk))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(str(Address.objects.get(pk=pk)), "Home (buyer)")

        res = self.client.patch(self.detail(pk), {"city": "Pune"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["city"], "Pune")

        res = self.client.delete(self.detail(pk))
        self.assertEqual(res.status_code, 204)
        self.assertEqual(self.client.get(self.detail(pk)).status_code, 404)

    def test_legacy_alias_serves_the_same_surface(self):
        self.assertEqual(self.fresh_client().get(self.LEGACY_LIST).status_code, 401)
        res = self.client.get(self.LEGACY_LIST)
        self.assertEqual(res.status_code, 200, res.data)
        res = self.create(base=self.LEGACY_LIST)
        self.assertEqual(res.status_code, 201, res.data)
        pk = res.data["id"]
        res = self.client.get(self.detail(pk, base=self.LEGACY_LIST))
        self.assertEqual(res.status_code, 200, res.data)

    def test_another_users_address_is_404_for_read_update_delete(self):
        pk = self.create().data["id"]
        other = self.make_user(username="intruder")
        intruder = self.fresh_client()
        self.api_login(other.username, client=intruder)
        detail = self.detail(pk)
        self.assertEqual(intruder.get(detail).status_code, 404)
        self.assertEqual(
            intruder.patch(detail, {"city": "Pune"}, format="json").status_code, 404
        )
        self.assertEqual(intruder.delete(detail).status_code, 404)
        row = Address.objects.get(pk=pk)
        self.assertEqual(row.user, self.user)
        self.assertEqual(row.city, "Mumbai")

    def test_list_only_contains_the_callers_rows(self):
        self.create()
        other = self.make_user(username="browse")
        Address.objects.create(user=other, **self.payload(line1="Other Road"))
        res = self.client.get(self.LIST)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([row["line1"] for row in res.data], ["12 Rose Lane"])

    def test_second_default_create_flips_the_first(self):
        first = self.create(self.payload(is_default=True)).data["id"]
        second = self.create(self.payload(line1="9 Lily Road", is_default=True)).data[
            "id"
        ]
        self.assertFalse(Address.objects.get(pk=first).is_default)
        self.assertTrue(Address.objects.get(pk=second).is_default)

    def test_update_to_default_flips_the_previous(self):
        first = self.create(self.payload(is_default=True)).data["id"]
        second = self.create(self.payload(line1="9 Lily Road")).data["id"]
        res = self.client.patch(
            self.detail(second), {"is_default": True}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(Address.objects.get(pk=first).is_default)
        self.assertTrue(Address.objects.get(pk=second).is_default)

    def test_non_default_create_keeps_the_existing_default(self):
        first = self.create(self.payload(is_default=True)).data["id"]
        self.create(self.payload(line1="9 Lily Road"))
        self.assertTrue(Address.objects.get(pk=first).is_default)

    def test_default_is_scoped_per_user(self):
        mine = self.create(self.payload(is_default=True)).data["id"]
        other = self.make_user(username="neighbour")
        theirs = Address.objects.create(
            user=other, **self.payload(line1="2 Oak Avenue", is_default=True)
        )
        self.create(self.payload(line1="9 Lily Road", is_default=True))
        self.assertFalse(Address.objects.get(pk=mine).is_default)
        self.assertTrue(Address.objects.get(pk=theirs.pk).is_default)

    def test_deleting_the_default_leaves_none(self):
        pk = self.create(self.payload(is_default=True)).data["id"]
        self.assertEqual(self.client.delete(self.detail(pk)).status_code, 204)
        self.assertFalse(
            Address.objects.filter(user=self.user, is_default=True).exists()
        )

    def test_blank_required_fields_are_rejected(self):
        for field in ("line1", "city", "country"):
            for value in ("", "   "):
                with self.subTest(field=field, value=value):
                    res = self.create(self.payload(**{field: value}))
                    self.assertEqual(res.status_code, 400, res.data)
                    self.assertIn(field, self.errors(res))

    def test_absurd_postal_and_phone_lengths_are_rejected(self):
        res = self.create(self.payload(postal_code="1" * 21))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("postal_code", self.errors(res))
        res = self.create(self.payload(phone="1" * 33))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("phone", self.errors(res))

    def test_default_flip_locks_the_owners_rows(self):
        real_lock = Address.objects.select_for_update
        with mock.patch(
            "accounts.views.Address.objects.select_for_update", wraps=real_lock
        ) as lock:
            res = self.create(self.payload(is_default=True))
        self.assertEqual(res.status_code, 201, res.data)
        lock.assert_called_once()

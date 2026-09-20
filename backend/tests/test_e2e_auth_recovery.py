"""E2E auth/recovery tests - docs/test-gaps.md e2e items 2 and 3."""
from django.core import mail
from django.test import tag

from common.testing import ApiTestCase, extract_link_params


@tag("e2e")
class VerificationGateTests(ApiTestCase):
    # e2e 2. register -> never verify -> login blocked forever (until resend)
    def test_login_stays_blocked_until_resend_and_verify_complete(self):
        res = self.client.post(
            "/api/accounts/register/",
            {"username": "delayed", "email": "delayed@example.com", "password": "S3cure-Passphrase!"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

        # blocked before the verification email is used
        res, _ = self.api_login("delayed")
        self.assertEqual(res.status_code, 401, res.data)

        # resend flow issues a fresh, working link (register already sent one)
        res = self.client.post("/api/accounts/resend-verification/", {"email": "delayed@example.com"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 2)
        uid, token = extract_link_params(mail.outbox[-1].body, "verify-email")

        res = self.client.post("/api/accounts/verify-email/", {"uid": uid, "token": token}, format="json")
        self.assertEqual(res.status_code, 200, res.data)

        # only now does the gate open
        res, access = self.api_login("delayed")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(access)
        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data, [])


@tag("e2e")
class PasswordResetFlowTests(ApiTestCase):
    # e2e 3. password reset end-to-end via outbox links
    def test_password_reset_end_to_end_via_outbox_link(self):
        # register + verify (two flows, two emails so far)
        self.register_and_verify(username="resetflow", email="resetflow@example.com")

        # request a reset link
        res = self.client.post("/api/accounts/password-reset/", {"email": "resetflow@example.com"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        reset_mail = mail.outbox[-1]
        uid, token = extract_link_params(reset_mail.body, "reset-password")

        # weak password rejected with validator messages
        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "123"},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("password", res.data["details"])

        # strong password accepted
        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "Br4nd-N3w-Pass!"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        # old credential is dead, new one works
        res, _ = self.api_login("resetflow", password="S3cure-Passphrase!")
        self.assertEqual(res.status_code, 401, res.data)
        res, access = self.api_login("resetflow", password="Br4nd-N3w-Pass!")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(access)

        # the link was one-time: reuse must fail
        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "An0ther-Pass-99!"},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)

    def test_reset_cannot_give_a_disabled_account_a_working_login(self):
        """Defence in depth: even with a VALID uid+token, an account disabled
        by staff (is_active=False) must never obtain a working login."""
        from django.contrib.auth.models import User
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        user = self.make_user("deactivated")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        User.objects.filter(pk=user.pk).update(is_active=False)  # staff disabled the account

        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "Ev1l-Passphrase-9"},
            format="json",
        )

        user.refresh_from_db()
        self.assertTrue(user.check_password("Ev1l-Passphrase-9"))  # password did change...
        self.assertFalse(user.is_active)                            # ...but the gate stays shut
        res, _ = self.api_login("deactivated", password="Ev1l-Passphrase-9")
        self.assertEqual(res.status_code, 401, res.data)

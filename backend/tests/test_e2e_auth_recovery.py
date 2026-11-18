from django.core import mail
from django.test import tag

from common.testing import ApiTestCase, extract_link_params


@tag("e2e")
class VerificationGateTests(ApiTestCase):
    def test_login_stays_blocked_until_resend_and_verify_complete(self):
        res = self.client.post(
            "/api/accounts/register/",
            {
                "username": "delayed",
                "email": "delayed@example.com",
                "password": "S3cure-Passphrase!",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

        res, _ = self.api_login("delayed")
        self.assertEqual(res.status_code, 401, res.data)

        res = self.client.post(
            "/api/accounts/resend-verification/",
            {"email": "delayed@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(mail.outbox), 2)
        uid, token = extract_link_params(mail.outbox[-1].body, "verify-email")

        res = self.client.post(
            "/api/accounts/verify-email/", {"uid": uid, "token": token}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)

        res, access = self.api_login("delayed")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(access)
        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["results"], [])


@tag("e2e")
class PasswordResetFlowTests(ApiTestCase):
    def test_password_reset_end_to_end_via_outbox_link(self):
        self.register_and_verify(username="resetflow", email="resetflow@example.com")

        res = self.client.post(
            "/api/accounts/password-reset/",
            {"email": "resetflow@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        reset_mail = mail.outbox[-1]
        uid, token = extract_link_params(reset_mail.body, "reset-password")

        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "123"},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("password", res.data["details"])

        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "Br4nd-N3w-Pass!"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        res, _ = self.api_login("resetflow", password="S3cure-Passphrase!")
        self.assertEqual(res.status_code, 401, res.data)
        res, access = self.api_login("resetflow", password="Br4nd-N3w-Pass!")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(access)

        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "An0ther-Pass-99!"},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)

    def test_reset_cannot_give_a_disabled_account_a_working_login(self):
        from django.contrib.auth.models import User
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        user = self.make_user("deactivated")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        User.objects.filter(pk=user.pk).update(
            is_active=False
        )  # staff disabled the account

        res = self.client.post(
            "/api/accounts/password-reset/confirm/",
            {"uid": uid, "token": token, "password": "Ev1l-Passphrase-9"},
            format="json",
        )

        user.refresh_from_db()
        self.assertTrue(
            user.check_password("Ev1l-Passphrase-9")
        )  # password did change...
        self.assertFalse(user.is_active)
        res, _ = self.api_login("deactivated", password="Ev1l-Passphrase-9")
        self.assertEqual(res.status_code, 401, res.data)

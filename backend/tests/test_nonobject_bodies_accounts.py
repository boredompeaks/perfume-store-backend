from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import override_settings, tag
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from common.testing import ApiTestCase
from products.models import RestockNotification

RAW_BODIES = ("[]", "[1,2]", "42", '"hello"', "null", "true")


@tag("accounts")
class NonObjectBodyAccountsTests(ApiTestCase):
    def setUp(self):
        self.inactive = self.make_user("sleeper", is_active=False)
        self.active = self.make_user("waker")

    def _post(self, path, body):
        # Reset the per-test throttle history so a 429 can never masquerade as a body verdict mid-loop.
        cache.clear()
        return self.client.post(path, body, content_type="application/json")

    def _same_as_empty_object(self, path):
        expected = self._post(path, "{}")
        for body in RAW_BODIES:
            with self.subTest(path=path, body=body):
                res = self._post(path, body)
                self.assertEqual(res.status_code, expected.status_code, res.data)
                self.assertEqual(res.content, expected.content)
                self.assertEqual(res.data, expected.data)
        return expected

    def test_verify_email_refuses_non_object_bodies_like_the_empty_object(self):
        expected = self._same_as_empty_object("/api/accounts/verify-email/")
        self.assertEqual(expected.status_code, status.HTTP_400_BAD_REQUEST)
        self.inactive.refresh_from_db()
        self.assertFalse(self.inactive.is_active)

    def test_resend_verification_treats_non_object_body_like_the_empty_object(self):
        expected = self._same_as_empty_object("/api/accounts/resend-verification/")
        self.assertEqual(expected.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)

    def test_forgot_username_treats_non_object_body_like_the_empty_object(self):
        expected = self._same_as_empty_object("/api/accounts/forgot-username/")
        self.assertEqual(expected.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)

    def test_request_password_reset_treats_non_object_body_like_the_empty_object(self):
        expected = self._same_as_empty_object("/api/accounts/password-reset/")
        self.assertEqual(expected.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_password_refuses_non_object_bodies_like_the_empty_object(self):
        expected = self._same_as_empty_object("/api/accounts/password-reset/confirm/")
        self.assertEqual(expected.status_code, status.HTTP_400_BAD_REQUEST)
        self.active.refresh_from_db()
        self.assertTrue(self.active.check_password("S3cure-Passphrase!"))

    def test_real_object_body_flow_is_untouched_by_the_body_guards(self):
        payload = {
            "uid": urlsafe_base64_encode(force_bytes(self.inactive.pk)),
            "token": default_token_generator.make_token(self.inactive),
        }
        res = self.client.post("/api/accounts/verify-email/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.inactive.refresh_from_db()
        self.assertTrue(self.inactive.is_active)


@tag("accounts")
class AlreadyGatedAccountsCanaryTests(ApiTestCase):
    def _post(self, path, body):
        cache.clear()
        return self.client.post(path, body, content_type="application/json")

    def test_logout_non_object_body_is_the_missing_refresh_400(self):
        user = self.make_user("nologoutcanary")
        refresh = RefreshToken.for_user(user)
        self.auth(str(refresh.access_token))
        expected = self._post("/api/accounts/logout/", "{}")
        self.assertEqual(expected.status_code, status.HTTP_400_BAD_REQUEST)
        for body in RAW_BODIES:
            with self.subTest(body=body):
                res = self._post("/api/accounts/logout/", body)
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(res.content, expected.content)

    def test_challenge_gate_treats_non_object_body_like_missing_challenge(self):
        with override_settings(ACCOUNT_CHALLENGE_SECRET="canary-secret"):
            expected = self._post("/api/accounts/register/", "{}")
            self.assertEqual(expected.status_code, status.HTTP_403_FORBIDDEN)
            for body in RAW_BODIES:
                with self.subTest(body=body):
                    res = self._post("/api/accounts/register/", body)
                    self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
                    self.assertEqual(res.content, expected.content)


@tag("restock")
class RestockNonObjectPreservationTests(ApiTestCase):
    def setUp(self):
        self.product = self.make_product(name="Oud Noir", stock=0)
        self.user = self.make_user("acct")

    def _url(self, product=None):
        return f"/api/products/{(product or self.product).slug}/restock-notifications/"

    def _guest_post(self, body):
        # Reset the throttle history between requests: throttling has its own pins.
        cache.clear()
        return self.client.post(self._url(), body, content_type="application/json")

    def test_guest_non_object_bodies_join_the_uniform_401_set(self):
        no_email = self._guest_post("{}")
        self.assertEqual(no_email.status_code, status.HTTP_401_UNAUTHORIZED)
        for body in RAW_BODIES:
            with self.subTest(body=body):
                res = self._guest_post(body)
                self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
                self.assertEqual(res.content, no_email.content)
        cache.clear()
        guest_delete = self.client.delete(
            self._url(), "[]", content_type="application/json"
        )
        self.assertEqual(guest_delete.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(guest_delete.content, no_email.content)
        self.assertEqual(RestockNotification.objects.count(), 0)

    def test_guest_uniform_202_half_is_undisturbed(self):
        valid = self._guest_post('{"email": "valid@example.org"}')
        invalid = self._guest_post('{"email": "not-an-email"}')
        self.assertEqual(valid.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(invalid.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(valid.content, invalid.content)
        self.assertEqual(
            list(RestockNotification.objects.values_list("email", flat=True)),
            ["valid@example.org"],
        )

    def test_authenticated_non_object_body_is_ignored_like_any_other_body(self):
        control = self.make_user("ctrl1")
        control_client = self.fresh_client()
        self.api_login(username=control.username, client=control_client)
        empty = control_client.post(self._url())
        self.assertEqual(empty.status_code, status.HTTP_201_CREATED)
        for i, body in enumerate(RAW_BODIES):
            with self.subTest(body=body):
                user = self.make_user(f"arm{i}")
                client = self.fresh_client()
                self.api_login(username=user.username, client=client)
                res = client.post(self._url(), body, content_type="application/json")
                self.assertEqual(res.status_code, status.HTTP_201_CREATED)
                self.assertEqual(res.content, empty.content)
        self.assertEqual(
            RestockNotification.objects.filter(user__isnull=False).count(),
            1 + len(RAW_BODIES),
        )

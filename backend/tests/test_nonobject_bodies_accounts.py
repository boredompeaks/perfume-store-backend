"""SPEC-Q-T1F2b (lane B): non-object JSON bodies on the accounts + restock
REST surface (T1F1's red-team sibling list, this lane's half).

A JSON body that is not an object (array, number, string, null, boolean)
parses fine in DRF but has no ``.get`` to call, so a dict-assuming view
read raises AttributeError — a 500 on a public endpoint (the F7-B1 class
T1F1 fixed on the cart writes). This module pins the classification of
every body-reading site in this lane's half:

- Ungated sites get the T1F1 gate: a non-object body must answer the
  byte-identical response the endpoint already gives for ``{}`` — never a
  500 (NonObjectBodyAccountsTests).
- Already-gated sites are pinned as such so the classification itself is
  test-backed (AlreadyGatedAccountsCanaryTests).
- restock's guest path is oracle-sensitive (SPEC-Q-RS1's uniform-401 set):
  its permission class already refuses every non-object body as "no
  submitted email", so the guest branch never sees one; those pins prove
  the 401/202 bytes the Q-RS1 audits proved are preserved
  (RestockNonObjectPreservationTests).

Bodies are sent as raw strings because the DRF test client's
``format="json"`` path treats falsy payloads (``[]``, ``{}``, ``0``) as
"no data" and would post an empty body instead of the payload under test
(the T1F1 cart-pin observation).
"""

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

# Every non-object JSON top level a parser can produce.
RAW_BODIES = ("[]", "[1,2]", "42", '"hello"', "null", "true")


@tag("accounts")
class NonObjectBodyAccountsTests(ApiTestCase):
    """The five ungated accounts body reads (verify_email, resend_verification,
    forgot_username, request_password_reset, reset_password): a non-object
    body must land in each endpoint's own ``{}`` verdict, byte-identical."""

    def setUp(self):
        # An inactive account so resend/verify have a real row to (not) act on;
        # an active one for the username/password-reset recovery probes.
        self.inactive = self.make_user("sleeper", is_active=False)
        self.active = self.make_user("waker")

    def _post(self, path, body):
        # The recovery/auth/register scopes are 5-10/min and this pin's
        # subject is body-shape handling, not throttling (budget refusals
        # have their own pins); reset the per-test throttle history so a
        # 429 can never masquerade as a body verdict mid-loop.
        cache.clear()
        return self.client.post(path, body, content_type="application/json")

    def _same_as_empty_object(self, path):
        expected = self._post(path, "{}")
        for body in RAW_BODIES:
            with self.subTest(path=path, body=body):
                res = self._post(path, body)
                self.assertEqual(res.status_code, expected.status_code, res.data)
                # Byte-identical wire body, not just an equal status class.
                self.assertEqual(res.content, expected.content)
                self.assertEqual(res.data, expected.data)
        return expected

    def test_verify_email_refuses_non_object_bodies_like_the_empty_object(self):
        expected = self._same_as_empty_object("/api/accounts/verify-email/")
        self.assertEqual(expected.status_code, status.HTTP_400_BAD_REQUEST)
        # No non-object body can verify anything: the sleeper stays inactive.
        self.inactive.refresh_from_db()
        self.assertFalse(self.inactive.is_active)

    def test_resend_verification_treats_non_object_body_like_the_empty_object(self):
        expected = self._same_as_empty_object("/api/accounts/resend-verification/")
        self.assertEqual(expected.status_code, status.HTTP_200_OK)
        # The empty-object answer mails nothing (no email key matches no
        # account), so the non-object bodies must mail nothing either.
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
        # Nothing reset: the active account keeps its password.
        self.active.refresh_from_db()
        self.assertTrue(self.active.check_password("S3cure-Passphrase!"))

    def test_real_object_body_flow_is_untouched_by_the_body_guards(self):
        # Canary: the gates sit in front of the dict read, so a properly
        # minted object-body flow still verifies end to end.
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
    """Classification evidence for the sites that already gate their body
    read: these pins pass before AND after this task's edits — they pin the
    "already safe" verdict itself, not a fix."""

    def _post(self, path, body):
        cache.clear()
        return self.client.post(path, body, content_type="application/json")

    def test_logout_non_object_body_is_the_missing_refresh_400(self):
        # Bearer-only session (no refresh cookie), so the body path is the
        # only carrier the view can read (the LogoutTests helper pattern).
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
        # With a challenge secret configured, the register gate refuses a
        # missing challenge with one uniform 403; the body read is already
        # dict-gated, so a non-object body is exactly "missing challenge".
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
    """restock_views.py's guest path: NON-REPRODUCING (classified this task).

    ``IsAuthenticatedOrSubmittedEmail`` only passes an anonymous POST whose
    body carries a normalized non-empty email, which requires a body with
    ``.get`` — every non-object body is therefore refused upstream as "no
    submitted email" and answers the uniform 401; the authenticated branch
    never reads the body at all. These pins prove both halves of that
    classification so the Q-RS1 uniform-401/202 bytes stay measured, not
    assumed."""

    def setUp(self):
        self.product = self.make_product(name="Oud Noir", stock=0)
        self.user = self.make_user("acct")

    def _url(self, product=None):
        return f"/api/products/{(product or self.product).slug}/restock-notifications/"

    def _guest_post(self, body):
        # Both restock axes are 5/min per IP (and per IP+email pair); the
        # pin posts more than a budget holds, so reset the throttle history
        # between requests — throttling has its own pins in test_restock.
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
        # A guest DELETE never reads a body; a non-object body keeps the
        # same uniform 401 either way.
        cache.clear()
        guest_delete = self.client.delete(
            self._url(), "[]", content_type="application/json"
        )
        self.assertEqual(guest_delete.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(guest_delete.content, no_email.content)
        # The 401 set arms nothing.
        self.assertEqual(RestockNotification.objects.count(), 0)

    def test_guest_uniform_202_half_is_undisturbed(self):
        # The success half the Q-RS1 audits proved: valid-format and
        # invalid-format guest opt-ins still get one byte-identical 202.
        valid = self._guest_post('{"email": "valid@example.org"}')
        invalid = self._guest_post('{"email": "not-an-email"}')
        self.assertEqual(valid.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(invalid.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(valid.content, invalid.content)
        # Only the valid address armed a row.
        self.assertEqual(
            list(RestockNotification.objects.values_list("email", flat=True)),
            ["valid@example.org"],
        )

    def test_authenticated_non_object_body_is_ignored_like_any_other_body(self):
        # The authed branch never reads request.data, so each non-object
        # body arms the caller's row with the same 201 bytes an empty POST
        # gets — one user per request, because a re-opt-in answers 200.
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

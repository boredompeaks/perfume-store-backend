"""Shared test infrastructure for the whole suite.

Security rules enforced here (per docs/conventions.md and docs/test-gaps.md):

- Tests must never hit the network. The Razorpay client is always mocked
  (``razorpay_mock``) and the settings are pointed at throwaway dummy keys so
  that even an accidentally un-mocked client cannot use the real credentials
  from ``.env`` (V-01 containment).
- Email is delivered via the in-memory locmem backend so registration /
  verification / reset flows can never talk to a real SMTP host.
- Password hashing uses the fast MD5 hasher (test-only speed-up; production
  settings are untouched).
"""
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

import razorpay

TEST_RAZORPAY_KEY_ID = "rzp_test_TESTINGONLYDO NOTUSE"
TEST_RAZORPAY_KEY_SECRET = "TESTINGONLYSECRET-DO-NOT-USE"
# Test uploads must never land in the developer's real media/ directory.
TEST_MEDIA_ROOT = Path(mkdtemp(prefix="perfume-tests-media-"))

ApiSettings = override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RAZORPAY_KEY_ID=TEST_RAZORPAY_KEY_ID,
    RAZORPAY_KEY_SECRET=TEST_RAZORPAY_KEY_SECRET,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    MEDIA_ROOT=TEST_MEDIA_ROOT,
)


@ApiSettings
class ApiTestCase(TestCase):
    """Base class for every API test in the project."""

    # DRF client: JSON payloads + credentials() support; sessions/cookies
    # keep working because APIClient extends django.test.Client.
    client_class = APIClient

    def _pre_setup(self):
        super()._pre_setup()
        # Scoped throttles keep their request history in the default cache,
        # which lives for the whole test run; reset it per test so a rate
        # limit engaged in one test can never 429 another (deterministic
        # suite, independent of throttle rates in settings).
        cache.clear()

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    def make_user(self, username="buyer", password="S3cure-Passphrase!", email=None, is_active=True):
        return User.objects.create_user(
            username=username,
            email=email or f"{username}@example.com",
            password=password,
            is_active=is_active,
        )

    def make_staff(self, username="staff", password="S3cure-Passphrase!"):
        return User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password=password,
            is_staff=True,
        )

    def make_product(self, name="Rose Aurum", price="499.99", stock=10, category="Floral", **overrides):
        from products.models import products

        fields = dict(
            name=name,
            description="A fragrance with rose and musk notes.",
            price=Decimal(price),
            size=50,
            stock=stock,
            category=category,
        )
        fields.update(overrides)
        return products.objects.create(**fields)

    def make_coupon(self, code="SAVE10", discount_type="percentage", discount_value="10",
                    minimum_order_amount="0", maximum_discount=None, active=True,
                    usage_limit=None, used_count=0, **overrides):
        from orders.models import Coupon

        fields = dict(
            code=code,
            discount_type=discount_type,
            discount_value=Decimal(discount_value),
            minimum_order_amount=Decimal(minimum_order_amount),
            maximum_discount=Decimal(maximum_discount) if maximum_discount is not None else None,
            active=active,
            valid_from=timezone.now() - timedelta(days=1),
            valid_until=timezone.now() + timedelta(days=1),
            usage_limit=usage_limit,
            used_count=used_count,
        )
        fields.update(overrides)
        return Coupon.objects.create(**fields)

    # ------------------------------------------------------------------
    # Clients / auth helpers
    # ------------------------------------------------------------------

    def fresh_client(self):
        """A second APIClient with its own independent cookie jar/session."""
        return APIClient()

    def auth(self, token):
        """Set (or clear, with None) the JWT bearer on the default client."""
        if token is None:
            self.client.credentials()
        else:
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def api_login(self, username="buyer", password="S3cure-Passphrase!", client=None):
        """POST /api/accounts/login/ and attach the returned access token to
        the client that performed the login (default: self.client)."""
        target = client or self.client
        res = target.post(
            "/api/accounts/login/",
            {"username": username, "password": password},
            format="json",
        )
        token = res.data.get("access") if res.status_code == 200 else None
        if token:
            target.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return res, token

    def register_and_verify(self, username="buyer", password="S3cure-Passphrase!", email=None):
        """Full registration through the API: register -> verify email link."""
        email = email or f"{username}@example.com"
        res = self.client.post(
            "/api/accounts/register/",
            {"username": username, "email": email, "password": password},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201), res.data)
        self.assertEqual(len(mail.outbox), 1)
        uid, token = extract_link_params(mail.outbox[0].body, "verify-email")
        res = self.client.post(
            "/api/accounts/verify-email/",
            {"uid": uid, "token": token},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        return User.objects.get(username=username)

    # ------------------------------------------------------------------
    # Cart / checkout helpers
    # ------------------------------------------------------------------

    def seed_session_cart(self, items, client=None):
        """Build a session cart through the public cart API.

        ``items`` is an iterable of (product, quantity) tuples.
        Returns the cart representation.
        """
        client = client or self.client
        res = client.get("/api/cart/")
        self.assertEqual(res.status_code, 200, res.data)
        for product, quantity in items:
            res = client.post(
                "/api/cart/",
                {"product_id": product.id, "quantity": quantity},
                format="json",
            )
            self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def checkout_payload(self, **overrides):
        payload = {
            "full_name": "Buyer Person",
            "phone": "9876543210",
            "address": "12 Rose Lane",
            "city": "Mumbai",
            "state": "Maharashtra",
            "pincode": "400001",
        }
        payload.update(overrides)
        return payload

    def checkout(self, client=None, **overrides):
        client = client or self.client
        return client.post("/api/orders/checkout/", self.checkout_payload(**overrides), format="json")

    # ------------------------------------------------------------------
    # Razorpay fakes (never touch the network)
    # ------------------------------------------------------------------

    def razorpay_mock(self, order_id="order_TEST0001"):
        """Patch orders.views.razorpay.Client with a MagicMock.

        Returns the mock client *instance*. ``order.create`` yields
        ``{"id": order_id}`` and signature verification passes by default.
        """
        patcher = patch("orders.views.razorpay.Client")
        client_cls = patcher.start()
        self.addCleanup(patcher.stop)
        client = client_cls.return_value
        client.order.create.return_value = {"id": order_id}
        client.utility.verify_payment_signature.return_value = None
        return client

    def razorpay_fail_signature(self, client_mock):
        client_mock.utility.verify_payment_signature.side_effect = (
            razorpay.errors.SignatureVerificationError("invalid signature")
        )


def extract_link_params(body, hint):
    """Pull ``uid`` and ``token`` out of a verification/reset email link."""
    import re

    match = re.search(rf"{hint}\?uid=([^&\s]+)&token=([^&\s]+)", body)
    assert match, f"No {hint} link found in email body:\n{body}"
    return match.group(1), match.group(2)

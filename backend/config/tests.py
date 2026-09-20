"""Dual-mount pins for the /api/v1/ namespace (SPEC-9-02, spec §9 R-9.0).

Spec §9 (lines 2573–2589) prescribes versioned routes under ``/api/v1/``
with four organizational prefixes — store, account, admin, webhooks — as
boundaries, not authorization substitutes. These tests pin that both URL
families resolve to the identical view callables (the v1 mounts reuse the
legacy urlconf objects — no view duplication) and that the legacy aliases
stay alive until the coordinated frontend base-URL cutover (S9 ledger
note). The webhooks family is pinned as reserved-but-unrouted because no
webhook endpoint exists yet; SPEC-1-06 owns that mount (§11/§17 refs).
"""

from django.test import TestCase
from django.urls import Resolver404, resolve, reverse

from accounts.views import register
from cart.views import cart_detail
from ops.views import api_settings, audit_log, dashboard
from orders.views import create_order
from products.views import product_list


class V1NamespacePinTests(TestCase):
    """Pin both URL families to identical callables, per family."""

    def test_v1_routes_reverse_under_spec_prefixes(self):
        cases = {
            "v1:store:product-list": "/api/v1/store/products/",
            "v1:store:cart-detail": "/api/v1/store/cart/",
            "v1:store:create-order": "/api/v1/store/orders/checkout/",
            "v1:store:config": "/api/v1/store/config/",
            "v1:account:register": "/api/v1/account/register/",
            "v1:admin:dashboard": "/api/v1/admin/dashboard/",
            "v1:admin:audit-log": "/api/v1/admin/audit-log/",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(reverse(name), expected)

    def test_legacy_reverse_unaffected_by_v1_mount(self):
        # reverse() callers (e.g. password-reset emails) must keep producing
        # legacy paths until the frontend cutover is coordinated.
        cases = {
            "product-list": "/api/products/",
            "cart-detail": "/api/cart/",
            "create-order": "/api/orders/checkout/",
            "api-settings": "/api/settings/",
            "register": "/api/accounts/register/",
            "admin-dashboard": "/admin/dashboard/",
            "admin-audit-log": "/admin/audit-log/",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(reverse(name), expected)

    def test_dual_mount_resolves_to_identical_callables(self):
        pairs = [
            ("/api/products/", "/api/v1/store/products/", product_list),
            ("/api/cart/", "/api/v1/store/cart/", cart_detail),
            ("/api/orders/checkout/", "/api/v1/store/orders/checkout/", create_order),
            ("/api/settings/", "/api/v1/store/config/", api_settings),
            ("/api/accounts/register/", "/api/v1/account/register/", register),
            ("/admin/dashboard/", "/api/v1/admin/dashboard/", dashboard),
            ("/admin/audit-log/", "/api/v1/admin/audit-log/", audit_log),
        ]
        for legacy_path, v1_path, view in pairs:
            with self.subTest(v1_path=v1_path):
                self.assertIs(resolve(v1_path).func, view)
                self.assertIs(resolve(legacy_path).func, view)

    def test_legacy_alias_still_serves_the_same_payload(self):
        # Live end-to-end alias proof on the public settings reader: the
        # legacy path and the v1 path return the same JSON body.
        legacy = self.client.get("/api/settings/")
        v1 = self.client.get("/api/v1/store/config/")
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(v1.status_code, 200)
        self.assertEqual(legacy.json(), v1.json())

    def test_webhooks_prefix_reserved_until_webhook_endpoint_exists(self):
        # §9 line 2587 reserves /api/v1/webhooks/; the endpoint itself does
        # not exist yet (SPEC-1-06). Pin that nothing premature is routed.
        with self.assertRaises(Resolver404):
            resolve("/api/v1/webhooks/razorpay/")

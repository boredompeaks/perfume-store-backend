"""Ops tests: /health/, /api/settings/, admin dashboard, services."""
import tempfile
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import User
from django.template import Context, RequestContext, Template
from django.test import override_settings, tag
from django.test.client import RequestFactory
from django.utils import timezone

from common.testing import ApiTestCase
from ops.services import REVENUE_STATUSES, get_health, get_stats


@tag("ops")
class HealthEndpointTests(ApiTestCase):
    def test_health_returns_ok_shape(self):
        res = self.client.get("/health/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["checks"]["database"])
        self.assertIn("razorpay_mode", data["checks"])
        self.assertIn("pending_orders", data)

    def test_health_counts_pending_orders_and_carts(self):
        from cart.models import Cart

        Cart.objects.create(session_id="s1")
        Cart.objects.create(session_id="s2")
        buyer = self.make_user("buyer")
        from decimal import Decimal

        from orders.models import Order

        Order.objects.create(
            user=buyer, full_name="a", phone="1", address="a", city="c", state="s",
            pincode="1", status="pending", total_amount=Decimal("10.00"),
        )
        data = self.client.get("/health/").json()
        self.assertEqual(data["pending_orders"], 1)
        self.assertEqual(data["carts"], 2)

    def test_health_degraded_when_media_root_unwritable(self):
        """If MEDIA_ROOT cannot be created/written the store can serve product
        images from uploads -> status must degrade to 503, not stay ok."""
        tmp = Path(tempfile.mkdtemp())
        blocker = tmp / "blocker"
        blocker.write_text("a file, not a directory", encoding="utf-8")

        with override_settings(MEDIA_ROOT=blocker / "media"):
            res = self.client.get("/health/")

        self.assertEqual(res.status_code, 503, res.json())
        data = res.json()
        self.assertEqual(data["status"], "degraded")
        self.assertFalse(data["checks"]["media_writable"])
        self.assertTrue(data["checks"]["database"])

    def test_health_degraded_when_database_unavailable(self):
        """If the ORM raises (DB down), /health/ must degrade to 503 without
        leaking a traceback."""
        from unittest.mock import patch

        with patch("orders.models.Order.objects.filter", side_effect=Exception("db gone")):
            data = get_health()

        self.assertEqual(data["status"], "degraded")
        self.assertFalse(data["checks"]["database"])
        self.assertIsNone(data["pending_orders"])
        self.assertIsNone(data["carts"])
        self.assertEqual(data["low_stock"], 0)
        self.assertEqual(data["out_of_stock"], 0)

    def test_razorpay_mode_reported_from_key_prefix(self):
        cases = {
            "rzp_test_abc": "test",
            "rzp_live_abc": "live",
            "": "unset",
            "weird": "unset",
        }
        for key, expected in cases.items():
            with self.subTest(key=key or "<empty>"):
                with override_settings(RAZORPAY_KEY_ID=key or None):
                    data = get_health()
                self.assertEqual(data["checks"]["razorpay_mode"], expected)


@tag("ops")
class SettingsEndpointTests(ApiTestCase):
    def test_settings_returns_singleton_values(self):
        from ops.models import SiteSettings

        res = self.client.get("/api/settings/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        for key in (
            "support_email",
            "support_phone",
            "whatsapp_number",
            "whatsapp_message",
            "instagram_url",
        ):
            self.assertIn(key, data)

    def test_settings_reflects_admin_edited_values(self):
        from ops.models import SiteSettings

        row = SiteSettings.load()
        row.support_email = "care@maison.example"
        row.support_phone = "+91 90000 00000"
        row.whatsapp_number = "919000000000"
        row.whatsapp_message = "Hi there!"
        row.instagram_url = "https://instagram.com/maison.example"
        row.save()

        data = self.client.get("/api/settings/").json()
        self.assertEqual(data["support_email"], "care@maison.example")
        self.assertEqual(data["whatsapp_message"], "Hi there!")
        self.assertEqual(data["instagram_url"], "https://instagram.com/maison.example")

    def test_sitesettings_is_a_true_singleton(self):
        from ops.models import SiteSettings

        first = SiteSettings.load()
        second = SiteSettings()
        second.support_email = "x@y.example"
        second.save()  # forces pk=1, overwriting the singleton
        self.assertEqual(SiteSettings.objects.count(), 1)
        self.assertEqual(SiteSettings.load().pk, 1)
        self.assertEqual(first.pk, 1)
        self.assertEqual(str(first), "Site settings")


@tag("ops")
class OpsServicesTests(ApiTestCase):
    def _seed_orders(self):
        from decimal import Decimal

        from orders.models import Order

        buyer = self.make_user("buyer")
        amounts = {
            "pending": "100.00",
            "confirmed": "250.00",
            "shipped": "10.50",
            "delivered": "5.25",
            "cancelled": "999.00",
        }
        for status, amount in amounts.items():
            Order.objects.create(
                user=buyer, full_name="a", phone="1", address="a", city="c", state="s",
                pincode="1", status=status, total_amount=Decimal(amount),
            )
        self.make_product()

    def test_get_stats_counts_and_revenue(self):
        self._seed_orders()
        stats = get_stats()

        self.assertEqual(stats["users"], User.objects.count())
        self.assertEqual(stats["products"], 1)
        self.assertEqual(stats["orders_total"], 5)
        self.assertEqual(stats["orders_by_status"]["pending"], 1)
        # revenue only counts money actually captured
        expected = sum(Decimal(v) for s, v in [("confirmed", "250.00"), ("shipped", "10.50"), ("delivered", "5.25")])
        self.assertEqual(REVENUE_STATUSES, ("confirmed", "shipped", "delivered"))
        self.assertEqual(stats["revenue"], str(expected))
        self.assertEqual(len(stats["recent_orders"]), 5)
        self.assertIn("support_email", stats)

    def test_get_stats_on_empty_store(self):
        stats = get_stats()
        self.assertEqual(stats["orders_total"], 0)
        self.assertEqual(stats["revenue"], "0")
        self.assertEqual(stats["recent_orders"], [])

    def test_get_stats_average_order_value(self):
        self._seed_orders()
        stats = get_stats()

        # paid revenue 250.00 + 10.50 + 5.25 = 265.75 across 3 paid orders;
        # the unpaid (pending) and cancelled orders must not dilute either side.
        self.assertEqual(stats["revenue"], "265.75")
        self.assertEqual(stats["average_order_value"], "88.58")
        self.assertIsInstance(stats["average_order_value"], str)

    def test_get_stats_average_order_value_guards_zero_paid_orders(self):
        """Division by zero must quantize to 0.00, not crash: an order that is
        merely created (pending) has captured no money yet."""
        from decimal import Decimal

        from orders.models import Order

        for subcase in ("empty store", "only unpaid orders"):
            with self.subTest(subcase=subcase):
                Order.objects.all().delete()
                if subcase == "only unpaid orders":
                    buyer = self.make_user("windowshopper")
                    Order.objects.create(
                        user=buyer, full_name="a", phone="1", address="a",
                        city="c", state="s", pincode="1", status="pending",
                        total_amount=Decimal("99.00"),
                    )
                stats = get_stats()
                self.assertEqual(stats["average_order_value"], "0.00")
                self.assertEqual(stats["orders_pending_fulfilment"], 0)

    def test_get_stats_pending_fulfilment_counts_paid_unshipped_only(self):
        """Fulfilment-pending = payment captured but not dispatched yet:
        only 'confirmed' qualifies — 'pending' is payment-pending, 'shipped' /
        'delivered' have left the warehouse, 'cancelled' is dead."""
        from ops.services import PENDING_FULFILMENT_STATUS

        self.assertEqual(PENDING_FULFILMENT_STATUS, "confirmed")
        self._seed_orders()
        stats = get_stats()
        self.assertEqual(stats["orders_pending_fulfilment"], 1)
        self.assertEqual(stats["orders_by_status"]["confirmed"], 1)
        # the old 'pending' (payment-pending) count must stay a separate metric
        self.assertEqual(stats["orders_by_status"]["pending"], 1)


@tag("ops")
class DashboardAccessTests(ApiTestCase):
    def test_dashboard_requires_staff(self):
        res = self.client.get("/admin/dashboard/")
        # Redirects anonymous users to the admin login.
        self.assertEqual(res.status_code, 302)
        self.assertIn("login", res.headers["Location"])

    def test_dashboard_renders_for_staff(self):
        User.objects.create_superuser("boss", "boss@example.com", "boss-pass-123")
        self.client.login(username="boss", password="boss-pass-123")
        res = self.client.get("/admin/dashboard/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Store dashboard")

    def test_dashboard_lists_recent_orders_with_usernames(self):
        from decimal import Decimal

        from orders.models import Order

        User.objects.create_superuser("boss", "boss@example.com", "boss-pass-123")
        self.client.login(username="boss", password="boss-pass-123")
        buyer = self.make_user("dashbuyer")
        Order.objects.create(
            user=buyer, full_name="a", phone="1", address="a", city="c", state="s",
            pincode="1", status="confirmed", total_amount=Decimal("42.00"),
        )
        res = self.client.get("/admin/dashboard/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "dashbuyer")

    def test_dashboard_renders_new_kpi_cards(self):
        """The aggregate Orders card, the AOV card and the pending-fulfilment
        card render real numbers; the payment-pending metric stays visible and
        clearly labeled instead of being silently repurposed."""
        from decimal import Decimal

        from orders.models import Order

        User.objects.create_superuser("boss", "boss@example.com", "boss-pass-123")
        self.client.login(username="boss", password="boss-pass-123")
        buyer = self.make_user("kpibuyer")
        kpis = (("confirmed", "100.00"), ("shipped", "25.00"), ("pending", "50.00"))
        for status, amount in kpis:
            Order.objects.create(
                user=buyer, full_name="a", phone="1", address="a", city="c", state="s",
                pincode="1", status=status, total_amount=Decimal(amount),
            )

        res = self.client.get("/admin/dashboard/")

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "all statuses")
        self.assertEqual(res.context["stats"]["orders_total"], 3)
        # AOV = paid revenue (100.00 + 25.00) / 2 paid orders
        self.assertContains(res, "Average order value")
        self.assertContains(res, "paid revenue ÷ paid orders")
        self.assertContains(res, "₹62.50")
        self.assertContains(res, "Pending fulfilment")
        self.assertContains(res, "paid, awaiting shipment (confirmed)")
        # payment-pending remains its own, clearly labeled metric
        self.assertContains(res, "Orders · pending")
        self.assertContains(res, "awaiting payment")

    def test_dashboard_survives_orders_with_deleted_users(self):
        """An order row whose user no longer resolves must render as a dash,
        not crash the dashboard."""
        from decimal import Decimal
        from unittest.mock import patch

        from orders.models import Order

        User.objects.create_superuser("boss", "boss@example.com", "boss-pass-123")
        self.client.login(username="boss", password="boss-pass-123")
        buyer = self.make_user("goner")
        Order.objects.create(
            user=buyer, full_name="a", phone="1", address="a", city="c", state="s",
            pincode="1", status="pending", total_amount=Decimal("42.00"),
        )
        # make ONLY the buyer's lookup fail (session auth also uses .get)
        real_get = User.objects.get

        def selective_get(*args, **kwargs):
            pk = kwargs.get("pk", args[0] if args else None)
            if pk == buyer.id:
                raise User.DoesNotExist
            return real_get(*args, **kwargs)

        with patch.object(User.objects, "get", side_effect=selective_get):
            res = self.client.get("/admin/dashboard/")
        self.assertEqual(res.status_code, 200)  # no crash; username falls back
        self.assertContains(res, "42.00")  # the orphaned order is still listed

    def test_dashboard_shows_low_and_out_of_stock(self):
        from products.models import products as Product

        User.objects.create_superuser("boss", "boss@example.com", "boss-pass-123")
        self.client.login(username="boss", password="boss-pass-123")
        low = self.make_product(name="Low Stock One", stock=2)
        Product.objects.create(name="Empty Bottle", description="d", price=1, size=1, stock=0, category="X")
        healthy = self.make_product(name="Healthy Stock", stock=50)

        res = self.client.get("/admin/dashboard/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, low.name)
        self.assertNotContains(res, healthy.name)


@tag("ops")
class SiteSettingsAdminTests(ApiTestCase):
    """The singleton settings row: changelist redirects to the change form;
    add/delete are disabled."""

    def setUp(self):
        User.objects.create_superuser("boss", "boss@example.com", "boss-pass-123")
        self.assertTrue(self.client.login(username="boss", password="boss-pass-123"))

    def test_changelist_redirects_to_change_form(self):
        from ops.models import SiteSettings

        SiteSettings.load()  # ensure the singleton row exists
        res = self.client.get("/admin/ops/sitesettings/")
        self.assertEqual(res.status_code, 302)
        self.assertIn(f"/admin/ops/sitesettings/1/change/", res.headers["Location"])

    def test_changelist_creates_nothing_when_row_missing(self):
        """With no singleton row yet the changelist still renders (the row is
        created lazily by .load(), not by the admin)."""
        res = self.client.get("/admin/ops/sitesettings/")
        self.assertEqual(res.status_code, 200)

    def test_change_form_renders_and_saves(self):
        from ops.models import SiteSettings

        SiteSettings.load()
        res = self.client.get("/admin/ops/sitesettings/1/change/")
        self.assertEqual(res.status_code, 200)
        res = self.client.post(
            "/admin/ops/sitesettings/1/change/",
            {
                "support_email": "ops@maison.example",
                "support_phone": "+91 90000 00000",
                "whatsapp_number": "919000000000",
                "whatsapp_message": "Hello from tests",
                "instagram_url": "https://instagram.com/maison.example",
                "_save": "Save",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        row = SiteSettings.load()
        self.assertEqual(row.support_email, "ops@maison.example")

    def test_api_settings_serves_admin_edited_values(self):
        from ops.models import SiteSettings

        SiteSettings.load()
        self.client.post(
            "/admin/ops/sitesettings/1/change/",
            {
                "support_email": "care@maison.example",
                "support_phone": "",
                "whatsapp_number": "",
                "whatsapp_message": "",
                "instagram_url": "",
                "_save": "Save",
            },
            follow=True,
        )
        data = self.client.get("/api/settings/").json()
        self.assertEqual(data["support_email"], "care@maison.example")


@tag("ops")
class DashboardTemplateTagTests(ApiTestCase):
    def test_ops_dashboard_cards_tag_renders(self):
        from ops.templatetags.ops_dashboard import register as _register  # noqa: F401  (loads the tag library)

        request = RequestFactory().get("/")
        html = Template(
            "{% load ops_dashboard %}{% ops_dashboard_cards %}"
        ).render(RequestContext(request))
        self.assertIn("ok", html)

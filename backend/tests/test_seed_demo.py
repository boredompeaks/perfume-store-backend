"""SPEC-T0 (Phase T known-state seed): ``manage.py seed_demo``.

The BUILD-QUEUE T0 promotion of the former ``scripts/demo_seed.py`` into a
management command — the known-state entry point the red-team (T1) and
load (T2) programs both start from. The seed creates MARKED rows (the
script's own ``demo-`` prefix scheme, unchanged) so ``--cleanup`` can
remove exactly the demo set; the seed itself is idempotent by recreation
(clean-then-create), so a second run converges on the same set rather
than refusing or deduping in place.
"""

import re
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import Group, User
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from common.roles import ROLE_SUPPORT, STAFF_ROLES
from orders.management.commands import seed_demo
from orders.models import Coupon, Order, OrderItem
from products.models import StockMovement, products

# Hand-written literals pinning the exact demo set: if STAFF_ROLES or the
# seed's data ever changes, these fail and force a conscious update rather
# than silently recomputing from the same source the command reads.
STAFF_USERNAMES = {
    "demo-support",
    "demo-catalogue",
    "demo-inventory",
    "demo-marketing",
    "demo-finance",
    "demo-admin",
    "demo-superadmin",
}
CUSTOMER_USERNAME = "demo-customer"
PRODUCT_SLUGS = {
    "demo-plenty-perfume": 50,
    "demo-last-unit-perfume": 1,
    "demo-low-stock-perfume": 3,
}
COUPON_CODE = "DEMO-ONCE"


def _run(*argv):
    out = StringIO()
    call_command("seed_demo", *argv, stdout=out)
    return out.getvalue()


def _seed_demo_help():
    parser = seed_demo.Command().create_parser("manage.py", "seed_demo")
    return parser.format_help()


def _create_paid_order(user, total_amount):
    """The shared paid-order fixture fields; user and total vary per test."""
    # task: AUD-20261115-local-001
    return Order.objects.create(
        user=user,
        status="paid",
        payment_status="paid",
        fulfilment_status="unfulfilled",
        total_amount=total_amount,
        full_name="Real Buyer",
        phone="1234567890",
        address="10 Real Street",
        city="Real City",
        state="Real State",
        pincode="111111",
    )


class SeedDemoTests(TestCase):
    """The seeded set: marked rows, exact counts, exact identities."""

    def test_seed_creates_the_marked_demo_set(self):
        _run()

        marked_users = set(
            User.objects.filter(username__startswith="demo-").values_list(
                "username", flat=True
            )
        )
        self.assertEqual(marked_users, STAFF_USERNAMES | {CUSTOMER_USERNAME})

        staff = User.objects.filter(username__in=STAFF_USERNAMES)
        self.assertEqual(staff.count(), len(STAFF_USERNAMES))
        self.assertTrue(all(u.is_staff for u in staff))
        customer = User.objects.get(username=CUSTOMER_USERNAME)
        self.assertFalse(customer.is_staff)

        # One Django Group per staff role, each staff user inside it.
        # Groups are shared RBAC infrastructure, not demo-marked rows.
        self.assertEqual(
            set(
                Group.objects.filter(name__in=STAFF_ROLES).values_list(
                    "name", flat=True
                )
            ),
            set(STAFF_ROLES),
        )
        for role in STAFF_ROLES:
            self.assertTrue(
                User.objects.filter(username=f"demo-{role}", groups__name=role).exists()
            )

        demo_products = products.objects.filter(name__startswith="demo-")
        self.assertEqual(demo_products.count(), 3)
        self.assertEqual({p.slug: p.stock for p in demo_products}, PRODUCT_SLUGS)
        # Every demo product is inside the marker domain by name AND slug.
        self.assertTrue(all(p.slug.startswith("demo-") for p in demo_products))

        coupon = Coupon.objects.get(code=COUPON_CODE)
        self.assertTrue(coupon.code.startswith("DEMO-"))
        self.assertEqual(coupon.usage_limit, 1)
        self.assertEqual(coupon.used_count, 0)
        self.assertTrue(coupon.active)
        self.assertEqual(Coupon.objects.count(), 1)

        demo_orders = Order.objects.filter(user__username__startswith="demo-")
        self.assertEqual(demo_orders.count(), 3)
        self.assertTrue(
            all(o.status == "paid" and o.payment_status == "paid" for o in demo_orders)
        )
        self.assertEqual(OrderItem.objects.count(), 3)
        plenty = products.objects.get(slug="demo-plenty-perfume")
        self.assertTrue(
            all(
                item.product == plenty
                and item.quantity == 1
                and str(item.price) == "499.00"
                for item in OrderItem.objects.all()
            )
        )

    def test_second_seed_run_is_idempotent_by_recreation(self):
        _run()
        first = {
            "users": User.objects.filter(username__startswith="demo-").count(),
            "products": products.objects.filter(name__startswith="demo-").count(),
            "coupons": Coupon.objects.filter(code__startswith="DEMO-").count(),
            "orders": Order.objects.filter(user__username__startswith="demo-").count(),
            "items": OrderItem.objects.count(),
        }
        _run()
        second = {
            "users": User.objects.filter(username__startswith="demo-").count(),
            "products": products.objects.filter(name__startswith="demo-").count(),
            "coupons": Coupon.objects.filter(code__startswith="DEMO-").count(),
            "orders": Order.objects.filter(user__username__startswith="demo-").count(),
            "items": OrderItem.objects.count(),
        }
        self.assertEqual(first, second)
        # Literal pins, not a recomputed oracle: the second run leaves the
        # same counts behind, no duplicates, no growth.
        self.assertEqual(
            second,
            {
                "users": 8,
                "products": 3,
                "coupons": 1,
                "orders": 3,
                "items": 3,
            },
        )

    def test_seeded_customer_logs_in_with_the_printed_demo_credential(self):
        """The printed credential is now the customer's own generated value.

        The test id is the SPEC-T0 pin, kept so the test set cannot silently
        lose it; AUD-20261115-local-001 changed its contract: no shared literal
        exists, the value is unique per account, and printing it is opt-in.
        """
        self.assertIn("--print-credentials", _seed_demo_help())
        out = _run("--print-credentials")
        credentials = self._printed_credentials(out)
        self._assert_customer_logs_in(credentials)

    def _printed_credentials(self, out):
        # task: AUD-20261115-local-001
        self.assertNotIn("demo-account", out)
        credentials = dict(re.findall(r"^\s+(demo-\S+) / (\S+)$", out, re.MULTILINE))
        self.assertEqual(set(credentials), STAFF_USERNAMES | {CUSTOMER_USERNAME})
        self.assertEqual(len(set(credentials.values())), len(credentials))
        return credentials

    def _assert_customer_logs_in(self, credentials):
        self.assertIsNotNone(
            authenticate(
                username=CUSTOMER_USERNAME,
                password=credentials[CUSTOMER_USERNAME],
            )
        )

    def test_default_run_exposes_no_credential_and_the_old_default_is_dead(self):
        out = _run()
        # The shared literal was the finding: one published value opened every
        # seeded account. It must neither be printed nor authenticate anyone.
        self.assertNotIn("demo-account", out)
        self.assertNotIn(f"{CUSTOMER_USERNAME} / ", out)
        for username in STAFF_USERNAMES | {CUSTOMER_USERNAME}:
            with self.subTest(username=username):
                self.assertIsNone(
                    authenticate(username=username, password="demo-account")
                )


class SeedDemoCleanupTests(TestCase):
    """``--cleanup``: removes exactly the marked set; foreign rows live."""

    def _make_foreign_rows(self):
        """Rows sharing the seeded MODELS but outside the marker domain —
        including near-misses that contain 'demo' without the prefix."""
        foreign_user = User.objects.create_user(
            username="real-buyer", password="fixture"
        )
        near_miss_user = User.objects.create_user(
            username="demo",
            password="x-near-miss",  # no dash: unmarked
        )
        foreign_product = products.objects.create(
            name="Bergamot Noir",
            slug="bergamot-noir",
            description="A real product.",
            price=Decimal("899.00"),
            size=50,
            stock=7,
            category="oriental",
        )
        near_miss_product = products.objects.create(
            name="demo",  # no dash: unmarked
            slug="demo-product-word",
            description="Contains the word but not the marker prefix.",
            price=Decimal("199.00"),
            size=30,
            stock=2,
            category="fresh",
        )
        foreign_coupon = Coupon.objects.create(
            code="REAL10",
            discount_type="percentage",
            discount_value=Decimal("10.00"),
            minimum_order_amount=Decimal("0.00"),
            active=True,
            valid_from=timezone.now() - timedelta(days=1),
            valid_until=timezone.now() + timedelta(days=30),
        )
        near_miss_coupon = Coupon.objects.create(
            code="DEMO",  # no dash: unmarked
            discount_type="fixed",
            discount_value=Decimal("5.00"),
            minimum_order_amount=Decimal("0.00"),
            active=True,
            valid_from=timezone.now() - timedelta(days=1),
            valid_until=timezone.now() + timedelta(days=30),
        )
        foreign_order = _create_paid_order(foreign_user, Decimal("899.00"))
        foreign_movement = StockMovement.objects.create(
            product=foreign_product,
            delta=7,
            stock_after=7,
            reason=StockMovement.Reason.RESTOCK,
        )
        return {
            "user": foreign_user,
            "near_miss_user": near_miss_user,
            "product": foreign_product,
            "near_miss_product": near_miss_product,
            "coupon": foreign_coupon,
            "near_miss_coupon": near_miss_coupon,
            "order": foreign_order,
            "movement": foreign_movement,
        }

    def test_cleanup_removes_exactly_the_marked_set(self):
        foreign = self._make_foreign_rows()
        _run()
        # Simulate red-team activity attaching a ledger row to a DEMO
        # product after the seed: it is inside the marker domain, so
        # cleanup owns it too.
        demo_product = products.objects.get(slug="demo-plenty-perfume")
        demo_movement = StockMovement.objects.create(
            product=demo_product,
            delta=-1,
            stock_after=49,
            reason=StockMovement.Reason.CORRECTION,
        )

        _run("--cleanup")

        self.assertEqual(User.objects.filter(username__startswith="demo-").count(), 0)
        self.assertEqual(products.objects.filter(name__startswith="demo-").count(), 0)
        self.assertEqual(Coupon.objects.filter(code__startswith="DEMO-").count(), 0)
        self.assertEqual(
            Order.objects.filter(user__username__startswith="demo-").count(), 0
        )
        self.assertFalse(
            OrderItem.objects.filter(order__user__username__startswith="demo-").exists()
        )
        self.assertFalse(
            StockMovement.objects.filter(product__name__startswith="demo-").exists()
        )
        self.assertFalse(StockMovement.objects.filter(pk=demo_movement.pk).exists())

        # Foreign rows — same models, outside the marker domain — survive,
        # including the 'demo'-without-dash near-misses that pin the
        # prefix boundary exactly.
        self.assertTrue(User.objects.filter(pk=foreign["user"].pk).exists())
        self.assertTrue(User.objects.filter(pk=foreign["near_miss_user"].pk).exists())
        self.assertTrue(products.objects.filter(pk=foreign["product"].pk).exists())
        self.assertTrue(
            products.objects.filter(pk=foreign["near_miss_product"].pk).exists()
        )
        self.assertTrue(Coupon.objects.filter(pk=foreign["coupon"].pk).exists())
        self.assertTrue(
            Coupon.objects.filter(pk=foreign["near_miss_coupon"].pk).exists()
        )
        self.assertTrue(Order.objects.filter(pk=foreign["order"].pk).exists())
        self.assertTrue(
            StockMovement.objects.filter(pk=foreign["movement"].pk).exists()
        )

        # Role groups are shared RBAC infrastructure (get_or_create on
        # role names): cleanup never deletes them.
        self.assertEqual(
            set(
                Group.objects.filter(name__in=STAFF_ROLES).values_list(
                    "name", flat=True
                )
            ),
            set(STAFF_ROLES),
        )

    def test_cleanup_is_idempotent(self):
        foreign = self._make_foreign_rows()
        _run()
        _run("--cleanup")
        _run("--cleanup")  # second pass: nothing marked left, still exit 0

        self.assertEqual(User.objects.filter(username__startswith="demo-").count(), 0)
        self.assertEqual(products.objects.filter(name__startswith="demo-").count(), 0)
        self.assertEqual(Coupon.objects.filter(code__startswith="DEMO-").count(), 0)
        self.assertEqual(
            Order.objects.filter(user__username__startswith="demo-").count(), 0
        )
        # Foreign survivors are untouched by the repeated cleanup.
        self.assertTrue(User.objects.filter(pk=foreign["user"].pk).exists())
        self.assertTrue(products.objects.filter(pk=foreign["product"].pk).exists())
        self.assertTrue(Coupon.objects.filter(pk=foreign["coupon"].pk).exists())
        self.assertTrue(Order.objects.filter(pk=foreign["order"].pk).exists())

    def test_cleanup_on_an_empty_database_is_a_clean_noop(self):
        # No seed, no foreign rows: the marker querysets are empty, the
        # command exits 0 having deleted nothing (the marker path is the
        # ONLY deletion path — there is no blanket-delete mode to refuse).
        _run("--cleanup")
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(products.objects.count(), 0)
        self.assertEqual(Coupon.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)


@contextmanager
def _production_shaped_database():
    """A DEBUG=false boot pointed at a remote database: the guard's refusal case."""
    with (
        mock.patch.dict(
            settings.DATABASES["default"],
            {
                "ENGINE": "django.db.backends.postgresql",
                "HOST": "database-1.cluster.example.rds.amazonaws.com",
            },
        ),
        mock.patch.object(settings, "DEBUG", False),
    ):
        yield


class SeedDemoEnvironmentGuardTests(TestCase):
    """A production-shaped environment gets no demo rows, cleanup included."""

    def test_refuses_a_production_shaped_database_and_writes_nothing(self):
        with _production_shaped_database():
            with self.assertRaisesMessage(CommandError, "[seed_demo] refused"):
                _run()
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(products.objects.count(), 0)

    def test_refuses_a_production_deployment_even_with_a_local_database(self):
        with mock.patch.object(settings, "DJANGO_ENV", "production"):
            with self.assertRaisesMessage(CommandError, "not a test/demo environment"):
                _run()
        self.assertEqual(User.objects.count(), 0)

    def test_cleanup_obeys_the_same_guard(self):
        _run()
        self.assertTrue(User.objects.filter(username="demo-support").exists())
        with _production_shaped_database():
            with self.assertRaisesMessage(CommandError, "[seed_demo] refused"):
                _run("--cleanup")
        self.assertTrue(User.objects.filter(username="demo-support").exists())


class SeedDemoOrderBookBoundaryTests(TestCase):
    """The refused seed leaves no staff identity that can read the order book."""

    def test_refused_production_seed_cannot_reach_the_order_book(self):
        buyer = User.objects.create_user("real-buyer", "buyer@example.com", "fixture")
        _create_paid_order(buyer, Decimal("499.00"))
        with _production_shaped_database():
            with self.assertRaisesMessage(CommandError, "[seed_demo] refused"):
                _run()

        # No demo identity exists, so the published shared value authenticates
        # nobody and no seeded staff token can be minted.
        self.assertIsNone(
            authenticate(username="demo-support", password="demo-account")
        )
        client = APIClient()
        login = client.post(
            "/api/accounts/login/",
            {"username": "demo-support", "password": "demo-account"},
        )
        self.assertIn(login.status_code, (400, 401))

        # The book stays closed to anonymous and unprovisioned callers...
        self.assertIn(client.get("/api/admin/orders/").status_code, (401, 403))
        client.force_authenticate(buyer)
        self.assertIn(client.get("/api/admin/orders/").status_code, (401, 403))

        # ...and only an individually provisioned role identity opens it, so
        # the fix did not narrow the existing orders.read gate.
        support = User.objects.create_user(
            "real-support", "support@example.com", "fixture"
        )
        support.groups.add(Group.objects.get_or_create(name=ROLE_SUPPORT)[0])
        client.force_authenticate(support)
        self.assertEqual(client.get("/api/admin/orders/").status_code, 200)

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import authenticate
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from common.roles import STAFF_ROLES
from orders.models import Coupon, Order, OrderItem
from products.models import StockMovement, products

# Hand-written literals pinning the exact demo set: a change to STAFF_ROLES or the
# seed data fails here rather than silently recomputing from the source.
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


class SeedDemoTests(TestCase):
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
        out = _run()
        # Hand-written literal, NOT the command's own constant: the demo credential is what the seed prints, and it must stay plainly fake so scanners do not read it as real.
        self.assertIn("demo-customer / demo-account", out)
        self.assertIsNotNone(
            authenticate(username="demo-customer", password="demo-account")
        )


class SeedDemoCleanupTests(TestCase):
    def _make_foreign_rows(self):
        foreign_user = User.objects.create_user(
            username="real-buyer", password="fixture"
        )
        near_miss_user = User.objects.create_user(
            username="demo",
            password="x-near-miss",
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
            name="demo",
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
            code="DEMO",
            discount_type="fixed",
            discount_value=Decimal("5.00"),
            minimum_order_amount=Decimal("0.00"),
            active=True,
            valid_from=timezone.now() - timedelta(days=1),
            valid_until=timezone.now() + timedelta(days=30),
        )
        foreign_order = Order.objects.create(
            user=foreign_user,
            status="paid",
            payment_status="paid",
            fulfilment_status="unfulfilled",
            total_amount=Decimal("899.00"),
            full_name="Real Buyer",
            phone="1234567890",
            address="10 Real Street",
            city="Real City",
            state="Real State",
            pincode="111111",
        )
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
        _run("--cleanup")

        self.assertEqual(User.objects.filter(username__startswith="demo-").count(), 0)
        self.assertEqual(products.objects.filter(name__startswith="demo-").count(), 0)
        self.assertEqual(Coupon.objects.filter(code__startswith="DEMO-").count(), 0)
        self.assertEqual(
            Order.objects.filter(user__username__startswith="demo-").count(), 0
        )
        self.assertTrue(User.objects.filter(pk=foreign["user"].pk).exists())
        self.assertTrue(products.objects.filter(pk=foreign["product"].pk).exists())
        self.assertTrue(Coupon.objects.filter(pk=foreign["coupon"].pk).exists())
        self.assertTrue(Order.objects.filter(pk=foreign["order"].pk).exists())

    def test_cleanup_on_an_empty_database_is_a_clean_noop(self):
        _run("--cleanup")
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(products.objects.count(), 0)
        self.assertEqual(Coupon.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)

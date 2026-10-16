"""``manage.py seed_demo``: the Phase-T known-state seed (BUILD-QUEUE T0).

Promotes the former ``scripts/demo_seed.py`` into the tree — the data set
the red-team (T1) and load (T2) programs both start from. Homes in
``orders`` because that app owns most of the seeded model classes
(Coupon, Order, OrderItem; ``products`` contributes one).

Marker scheme (unchanged from the script, which is the source of truth):
every seeded row carries the ``demo-`` prefix on its identity column, so
cleanup is exact and never needs a registry.

- users:        username starts with ``demo-``
- products:     name starts with ``demo-`` (slug mirrors it)
- coupons:      code starts with ``DEMO-``
- orders:       belong to a ``demo-`` user
- stock moves:  belong to a ``demo-`` product (activity the demo state
  owns, e.g. red-team adjustments made against demo stock)

Groups created for staff roles are NOT marked and are never deleted:
they are shared RBAC infrastructure on the same role names
``common.roles`` owns everywhere else.

Semantics: ``seed`` is idempotent by recreation (it first removes the
marked set, then creates a fresh one — a second run converges on the same
rows rather than refusing or deduping in place). ``--cleanup`` deletes
ONLY through the marker querysets below — there is no blanket-delete
mode — and is idempotent: re-running it finds nothing marked and exits 0.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand
from django.utils import timezone

from common.roles import STAFF_ROLES
from orders.models import Coupon, Order, OrderItem
from products.models import StockMovement, products

DEMO_TAG = "demo-"
# Deliberately fake AND deliberately low-entropy: a password-shaped literal
# (mixed case + digits + symbol) here trips GitGuardian's Generic Password
# detector even in a seed fixture (incident 38269594), while the seeded
# demo accounts only need a stable, plainly-fake credential the command
# prints for humans to sign in with.
DEMO_PASSWORD = "demo-account"


def _demo_users():
    return User.objects.filter(username__startswith=DEMO_TAG)


def _demo_products():
    return products.objects.filter(name__startswith=DEMO_TAG)


def _demo_coupons():
    return Coupon.objects.filter(code__startswith=DEMO_TAG.upper())


def _demo_orders():
    return Order.objects.filter(user__username__startswith=DEMO_TAG)


def clean():
    """Delete every demo-tagged row. Order matters for FK constraints."""
    _demo_orders().delete()
    StockMovement.objects.filter(product__name__startswith=DEMO_TAG).delete()
    _demo_products().delete()
    _demo_coupons().delete()
    _demo_users().delete()


def seed():
    """Recreate the canonical demo set (clean-then-create, idempotent)."""
    clean()

    # --- staff users, one per role -------------------------------------
    for role in STAFF_ROLES:
        u = User.objects.create_user(
            username=f"{DEMO_TAG}{role}",
            password=DEMO_PASSWORD,
            email=f"{DEMO_TAG}{role}@example.com",
            is_staff=True,
        )
        group, _ = Group.objects.get_or_create(name=role)
        u.groups.add(group)
        u.save()

    # --- products -------------------------------------------------------
    p_plenty = products.objects.create(
        name=f"{DEMO_TAG}Plenty Perfume",
        slug=f"{DEMO_TAG}plenty-perfume",
        description="Demo product with ample stock.",
        price=Decimal("499.00"),
        size=100,
        stock=50,
        category="demo",
    )
    p_last = products.objects.create(
        name=f"{DEMO_TAG}Last Unit Perfume",
        slug=f"{DEMO_TAG}last-unit-perfume",
        description="Demo product with exactly one unit left.",
        price=Decimal("999.00"),
        size=50,
        stock=1,
        category="demo",
    )
    p_low = products.objects.create(
        name=f"{DEMO_TAG}Low Stock Perfume",
        slug=f"{DEMO_TAG}low-stock-perfume",
        description="Demo product with low stock.",
        price=Decimal("299.00"),
        size=30,
        stock=3,
        category="demo",
    )

    # --- coupon with usage_limit=1 --------------------------------------
    now = timezone.now()
    Coupon.objects.create(
        code=f"{DEMO_TAG.upper()}ONCE",
        discount_type="percentage",
        discount_value=Decimal("20.00"),
        minimum_order_amount=Decimal("0.00"),
        active=True,
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=30),
        usage_limit=1,
        used_count=0,
    )

    # --- a few fake paid orders so the dashboard has numbers ------------
    customer = User.objects.create_user(
        username=f"{DEMO_TAG}customer",
        password=DEMO_PASSWORD,
        email=f"{DEMO_TAG}customer@example.com",
    )
    for _ in range(3):
        order = Order.objects.create(
            user=customer,
            status="paid",
            payment_status="paid",
            fulfilment_status="unfulfilled",
            total_amount=Decimal("499.00"),
            full_name="Demo Customer",
            phone="9999999999",
            address="1 Demo Street",
            city="Demo City",
            state="Demo State",
            pincode="000000",
        )
        OrderItem.objects.create(
            order=order,
            product=p_plenty,
            quantity=1,
            price=Decimal("499.00"),
        )
    return p_plenty, p_last, p_low


class Command(BaseCommand):
    help = (
        "Seed (or with --cleanup, remove) the marked demo data set: "
        "the known-state entry point for red-team and load programs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--cleanup",
            action="store_true",
            help=(
                "Delete only the demo-marked rows (demo- usernames/names, "
                "DEMO- coupon codes, and stock movements on demo products). "
                "Idempotent; unmarked rows are never touched."
            ),
        )

    def handle(self, *args, **options):
        if options["cleanup"]:
            clean()
            self.stdout.write("[seed_demo] cleaned all demo-tagged rows")
            return
        p_plenty, p_last, p_low = seed()
        self.stdout.write("[seed_demo] seeded demo data")
        self.stdout.write(
            f"  staff users: {', '.join(DEMO_TAG + r for r in STAFF_ROLES)}"
        )
        self.stdout.write(f"  products: {p_plenty.name}, {p_last.name}, {p_low.name}")
        self.stdout.write(f"  coupon: {DEMO_TAG.upper()}ONCE (usage_limit=1)")
        self.stdout.write(f"  customer: {DEMO_TAG}customer / {DEMO_PASSWORD}")

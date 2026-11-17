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

Environment guard: the set includes staff accounts that hold real
capabilities, so the command refuses to write anything unless the declared
deployment environment is local/ci AND the resolved database is recognisably
isolated (DEBUG, sqlite, or a localhost host — the same predicate ``seed_e2e``
enforces). ``--cleanup`` obeys the same guard.

Credentials: every account gets its own generated high-entropy password,
never a value shared across accounts. Nothing usable is printed by default;
``--print-credentials`` prints each account's individual value once (a seed
re-run recreates the users, so it replaces them).
"""

import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from common.roles import STAFF_ROLES
from orders.management.commands.seed_e2e import _database_is_a_test_db
from orders.models import Coupon, Order, OrderItem
from products.models import StockMovement, products

DEMO_TAG = "demo-"
# The only declared deployment environments that are demo-safe. local covers a
# developer/demo boot; ci is the ephemeral test deployment. Anything else is
# refused before the database check, so a production/staging boot cannot seed
# even if its DATABASE_URL happens to look local.
_DEMO_SAFE_ENVIRONMENTS = frozenset({"local", "ci"})


def _guard_isolated_environment():
    """Refuse demo seeding outside a test/demo-shaped environment.

    The seeded staff accounts carry live capabilities, so the environment is
    the primary control: a production-shaped boot/database never gets the rows
    at all. Two independent readings are required: the declared deployment
    environment must be demo-safe, and the resolved database must pass
    seed_e2e's own test-DB predicate (one definition of "isolated" in this
    app, not a second one that can drift).
    """
    # task: AUD-20261115-local-001
    if settings.DJANGO_ENV not in _DEMO_SAFE_ENVIRONMENTS:
        raise CommandError(
            "[seed_demo] refused: this command writes demo staff and customer "
            f"accounts and DJANGO_ENV={settings.DJANGO_ENV!r} is not a "
            "test/demo environment (allowed: local, ci)."
        )
    if not _database_is_a_test_db():
        raise CommandError(
            "[seed_demo] refused: this command writes demo staff and customer "
            "accounts and will not run against a production-shaped database. "
            "Point DATABASE_URL at a local test database (DEBUG=true, the "
            "sqlite engine, or a host of localhost/127.0.0.1)."
        )


def _new_credential():
    """One unique password for one seeded account, never a shared literal."""
    return secrets.token_urlsafe(18)


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


def _seed_staff_users(credentials):
    """Create one demo staff identity per role, recording each credential."""
    for role in STAFF_ROLES:
        username = f"{DEMO_TAG}{role}"
        credential = _new_credential()
        credentials[username] = credential
        u = User.objects.create_user(
            username=username,
            password=credential,
            email=f"{DEMO_TAG}{role}@example.com",
            is_staff=True,
        )
        group, _ = Group.objects.get_or_create(name=role)
        u.groups.add(group)
        u.save()


def _seed_products():
    """Create the three catalog rows the demo programs expect."""
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
    return p_plenty, p_last, p_low


def _seed_coupon():
    """Create the usage_limit=1 coupon the demo flows exercise."""
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


def _seed_paid_orders(product, credentials):
    """Create the demo customer plus paid orders so dashboards have numbers."""
    customer_username = f"{DEMO_TAG}customer"
    customer_credential = _new_credential()
    credentials[customer_username] = customer_credential
    customer = User.objects.create_user(
        username=customer_username,
        password=customer_credential,
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
            product=product,
            quantity=1,
            price=Decimal("499.00"),
        )


def seed():
    """Recreate the canonical demo set (clean-then-create, idempotent).

    Returns the three products plus ``{username: credential}``; the credential
    is the account's own generated value and is never shared or stored in the
    source.
    """
    # task: AUD-20261115-local-001
    clean()
    credentials = {}
    _seed_staff_users(credentials)
    p_plenty, p_last, p_low = _seed_products()
    _seed_coupon()
    _seed_paid_orders(p_plenty, credentials)
    return p_plenty, p_last, p_low, credentials


def _add_cleanup_argument(parser):
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help=(
            "Delete only the demo-marked rows (demo- usernames/names, "
            "DEMO- coupon codes, and stock movements on demo products). "
            "Idempotent; unmarked rows are never touched."
        ),
    )


def _add_print_credentials_argument(parser):
    parser.add_argument(
        "--print-credentials",
        action="store_true",
        dest="print_credentials",
        help=(
            "Print each seeded account's own generated credential once. "
            "Off by default: no usable value is printed unless asked for."
        ),
    )


class Command(BaseCommand):
    help = (
        "Seed (or with --cleanup, remove) the marked demo data set: "
        "the known-state entry point for red-team and load programs."
    )

    def add_arguments(self, parser):
        # task: AUD-20261115-local-001
        _add_cleanup_argument(parser)
        _add_print_credentials_argument(parser)

    def handle(self, *args, **options):
        # task: AUD-20261115-local-001
        _guard_isolated_environment()
        if options["cleanup"]:
            self._handle_cleanup()
            return
        self._seed_and_report(options["print_credentials"])

    def _handle_cleanup(self):
        clean()
        self.stdout.write("[seed_demo] cleaned all demo-tagged rows")

    def _seed_and_report(self, print_credentials):
        p_plenty, p_last, p_low, credentials = seed()
        self.stdout.write("[seed_demo] seeded demo data")
        self.stdout.write(
            f"  staff users: {', '.join(DEMO_TAG + r for r in STAFF_ROLES)}"
        )
        self.stdout.write(f"  products: {p_plenty.name}, {p_last.name}, {p_low.name}")
        self.stdout.write(f"  coupon: {DEMO_TAG.upper()}ONCE (usage_limit=1)")
        self.stdout.write(f"  customer: {DEMO_TAG}customer")
        if print_credentials:
            self.stdout.write("  credentials (generated once; a re-run replaces them):")
            for username, credential in credentials.items():
                self.stdout.write(f"    {username} / {credential}")
        else:
            self.stdout.write(
                "  credentials: generated per account, not printed "
                "(use --print-credentials to show them once)"
            )

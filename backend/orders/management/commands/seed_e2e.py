"""``manage.py seed_e2e``: the owner's end-to-end TEST DATA set (SPEC-E2E-01).

This command exists so a human can exercise the five integrated tasks against a
running server without hand-building fixtures: a superadmin (with a confirmed
TOTP device, because R-17.9 refuses an unenrolled privileged login) to sign into
the admin, a non-staff customer to sign into the storefront, two products, two
coupons, two carriers with priced delivery methods, three orders in three
different lifecycle states, and one parcel per order. It is TEST DATA ONLY --
the credentials and the fixed MFA secret below are local development values,
never production ones, and nothing here should run against a live store.

Homes in ``orders`` for the same reason ``seed_demo`` does: that app owns most
of the seeded model classes (Coupon, Order, OrderItem; products and shipping
contribute the rest).

Idempotency: every row is upserted on its natural key (username, slug, code,
tracking number) rather than recreated, so re-running converges on the same set
and preserves values a human changed -- notably each order's generated
``order_number``. Re-running DOES reset the seeded statuses (both the three
orders and the three parcels) to the declared starting states, and resets the
superadmin device's replay watermark so a consumed code never locks the owner
out, which is what makes a second pass a reset rather than a blind duplicate.

``--cleanup`` deletes only the rows this command owns, addressed by those same
natural keys, and is itself idempotent.

``--scenario lifecycle`` (D-09) seeds the same accounts, catalogue and delivery
table but swaps the three A/B/C orders for L1/L2/L3: a dispatch that must move
the order, a split order whose delivered parcel must wait for its in-transit
sibling, and an unpaid order whose dispatch must be refused. Every lifecycle
row is owned by the same cleanup keys as the full seed.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import TOTPDevice
from common import totp
from orders.models import Coupon, Order, OrderItem
from orders.state import fulfilment_for_status, payment_for_status
from orders.views.checkout import (
    ORDER_NUMBER_ATTEMPTS,
    _generate_order_number,
)
from products.models import products
from shipping.models import (
    Shipment,
    ShippingMethod,
    ShippingProvider,
    ShippingRate,
)

# Test credential, printed by the command so a human can sign in. The leading
# "SuperAdmin" is the brief's required local login, not a secret: this command
# is the only place it appears and the summary labels the data as test-only.
SUPERADMIN_USERNAME = "superadmin"
SUPERADMIN_EMAIL = "superadmin@example.com"
SUPERADMIN_PASSWORD = "SuperAdmin#2026"

# The storefront sign-in for the same walk. Deliberately NOT staff: the owner
# needs the buyer's surface (cart, checkout, account pages), and staff flags
# would change what the API and the admin show for that session.
CUSTOMER_USERNAME = "customer"
CUSTOMER_EMAIL = "customer@example.com"
CUSTOMER_PASSWORD = "Customer#2026"

# Fixed TEST-ONLY base32 secret. Variability would be useless here: the owner
# enrolls one authenticator once and the command must keep printing the same
# factor, while production never runs this command. TEST-ONLY because a real
# factor's secret is generated per user and never written down anywhere.
TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
TEST_TOTP_ISSUER = "Perfume Store"

TEST_PRODUCTS = (
    {
        "slug": "marj",
        "name": "Marj",
        "description": "E2E test data only: floral reference product.",
        "price": Decimal("2499.00"),
        "size": 50,
        "stock": 50,
        "category": "floral",
    },
    {
        "slug": "oud-noir",
        "name": "Oud Noir",
        "description": "E2E test data only: woody reference product.",
        "price": Decimal("4999.00"),
        "size": 100,
        "stock": 20,
        "category": "woody",
    },
)

TEST_COUPONS = (
    {
        "code": "FREESHIP",
        "discount_type": Coupon.FREE_SHIPPING,
        "discount_value": Decimal("0.00"),
        "minimum_order_amount": Decimal("0.00"),
    },
    {
        "code": "SAVE10",
        "discount_type": "percentage",
        "discount_value": Decimal("10.00"),
        "minimum_order_amount": Decimal("0.00"),
    },
)

TEST_PROVIDERS = (
    {
        "code": "delhivery",
        "name": "Delhivery",
        "tracking_url_template": (
            "https://www.delhivery.com/track/package/{tracking_number}"
        ),
    },
    {
        "code": "bluedart",
        "name": "Blue Dart",
        "tracking_url_template": "https://www.bluedart.com/tracking/{tracking_number}",
    },
)

TEST_METHODS = (
    {"code": "standard", "name": "Standard delivery"},
    {"code": "express", "name": "Express delivery"},
)

# The 49.00 rate serves the 400xxx band only (region AND postal prefix must
# both match); the wildcard serves every other destination. The order rows
# below are priced with the rate their own address matches, so the seeded
# money is what quote_shipping would charge for that destination.
TEST_RATES = (
    {
        "method_code": "standard",
        "amount": Decimal("49.00"),
        "region": "Maharashtra",
        "postal_code_prefix": "400",
    },
    {
        "method_code": "standard",
        "amount": Decimal("99.00"),
        "region": "",
        "postal_code_prefix": "",
    },
    {
        "method_code": "express",
        "amount": Decimal("149.00"),
        "region": "",
        "postal_code_prefix": "",
    },
)

# ``idempotency_key`` is the order's natural key here: it is unique per user in
# the schema, survives a re-run, and lets the checkout-minted ``order_number``
# stay stable instead of being replaced by a second random value.
TEST_ORDERS = (
    {
        "label": "A",
        "idempotency_key": "seed-e2e-order-a",
        "status": "confirmed",
        "full_name": "Test Customer",
        "phone": "9999999999",
        "address": "1 Marine Drive",
        "city": "Mumbai",
        "state": "Maharashtra",
        "pincode": "400001",
        "shipping_method_code": "standard",
        "shipping_amount": Decimal("49.00"),
        "item_slug": "marj",
        "item_quantity": 1,
    },
    {
        "label": "B",
        "idempotency_key": "seed-e2e-order-b",
        "status": "pending",
        "full_name": "Test Customer",
        "phone": "9999999999",
        "address": "2 FC Road",
        "city": "Pune",
        "state": "Maharashtra",
        "pincode": "411001",
        "shipping_method_code": "standard",
        "shipping_amount": Decimal("99.00"),
        "item_slug": "oud-noir",
        "item_quantity": 1,
    },
    {
        "label": "C",
        "idempotency_key": "seed-e2e-order-c",
        "status": "delivered",
        "full_name": "Test Customer",
        "phone": "9999999999",
        "address": "3 Connaught Place",
        "city": "Delhi",
        "state": "Delhi",
        "pincode": "110001",
        "shipping_method_code": "standard",
        "shipping_amount": Decimal("99.00"),
        "item_slug": "marj",
        "item_quantity": 2,
    },
)

TEST_SHIPMENTS = (
    {
        "order_label": "A",
        "provider_code": "delhivery",
        "tracking_number": "DLV123456789",
        "carrier": "Delhivery",
        "status": Shipment.Status.LABEL_CREATED,
    },
    {
        "order_label": "B",
        "provider_code": "delhivery",
        "tracking_number": "DLV000000002",
        "carrier": "Delhivery",
        "status": Shipment.Status.LABEL_CREATED,
    },
    {
        "order_label": "C",
        "provider_code": "bluedart",
        "tracking_number": "BD987654321",
        "carrier": "Blue Dart",
        "status": Shipment.Status.DELIVERED,
    },
)

# The lifecycle scenario (D-09) is three orders that exercise the three order
# moves a shipment can drive: a dispatch that must move, a delivery that must
# wait for a sibling parcel, and a dispatch that must be refused because the
# order was never paid. It replaces the A/B/C set rather than adding to it, so
# a walk has one order per outcome instead of five overlapping fixtures.
LIFECYCLE_ORDERS = (
    {
        "label": "L1",
        "idempotency_key": "seed-e2e-lifecycle-l1",
        "status": "confirmed",
        "full_name": "Lifecycle One",
        "phone": "9999999991",
        "address": "11 Dispatch Road",
        "city": "Mumbai",
        "state": "Maharashtra",
        "pincode": "400001",
        "shipping_method_code": "standard",
        "shipping_amount": Decimal("49.00"),
        "item_slug": "marj",
        "item_quantity": 1,
    },
    {
        "label": "L2",
        "idempotency_key": "seed-e2e-lifecycle-l2",
        "status": "confirmed",
        "full_name": "Lifecycle Two",
        "phone": "9999999992",
        "address": "22 Split Parcel Road",
        "city": "Pune",
        "state": "Maharashtra",
        "pincode": "411001",
        "shipping_method_code": "standard",
        "shipping_amount": Decimal("99.00"),
        "item_slug": "oud-noir",
        "item_quantity": 1,
    },
    {
        "label": "L3",
        "idempotency_key": "seed-e2e-lifecycle-l3",
        "status": "pending",
        "full_name": "Lifecycle Three",
        "phone": "9999999993",
        "address": "33 Unpaid Road",
        "city": "Delhi",
        "state": "Delhi",
        "pincode": "110001",
        "shipping_method_code": "standard",
        "shipping_amount": Decimal("99.00"),
        "item_slug": "marj",
        "item_quantity": 1,
    },
)

LIFECYCLE_SHIPMENTS = (
    {
        "order_label": "L1",
        "provider_code": "delhivery",
        "tracking_number": "LCY000000001",
        "carrier": "Delhivery",
        "status": Shipment.Status.LABEL_CREATED,
    },
    {
        # One of L2's two parcels has arrived; the other is still moving. The
        # order stays at "confirmed" until a parcel move asks to advance it.
        "order_label": "L2",
        "provider_code": "delhivery",
        "tracking_number": "LCY000000002",
        "carrier": "Delhivery",
        "status": Shipment.Status.DELIVERED,
    },
    {
        "order_label": "L2",
        "provider_code": "bluedart",
        "tracking_number": "LCY000000003",
        "carrier": "Blue Dart",
        "status": Shipment.Status.IN_TRANSIT,
    },
    {
        "order_label": "L3",
        "provider_code": "delhivery",
        "tracking_number": "LCY000000004",
        "carrier": "Delhivery",
        "status": Shipment.Status.LABEL_CREATED,
    },
)


def _seed_superadmin():
    user, _ = User.objects.update_or_create(
        username=SUPERADMIN_USERNAME,
        defaults={
            "email": SUPERADMIN_EMAIL,
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        },
    )
    # check_password keeps a re-run from rewriting the hash on every pass while
    # still resetting a password the owner changed away from the documented one.
    if not user.check_password(SUPERADMIN_PASSWORD):
        user.set_password(SUPERADMIN_PASSWORD)
        user.save(update_fields=["password"])
    return user


def _seed_customer():
    """The storefront account: a plain, active customer.

    Same password rule as the superadmin: ``check_password`` keeps a re-run
    from rewriting the hash while still resetting a password the owner changed
    away from the documented one.
    """
    user, _ = User.objects.update_or_create(
        username=CUSTOMER_USERNAME,
        defaults={
            "email": CUSTOMER_EMAIL,
            "is_staff": False,
            "is_superuser": False,
            "is_active": True,
        },
    )
    if not user.check_password(CUSTOMER_PASSWORD):
        user.set_password(CUSTOMER_PASSWORD)
        user.save(update_fields=["password"])
    return user


def _seed_totp_device(user):
    """Enroll the superadmin's second factor (R-17.9 requires it to log in).

    ``update_or_create`` on the OneToOne user keeps the set at exactly one
    device. Clearing ``last_used_counter`` every run is the important half:
    verification consumes a counter, so without the reset the code printed
    now would be refused after any earlier login in the same 30-second step
    and the owner would have to wait out the window. Clearing
    ``trusted_until`` keeps the seed a clean challenge-every-login state
    rather than inheriting a browser grant a human opted into.
    """
    device, _ = TOTPDevice.objects.update_or_create(
        user=user,
        defaults={
            "secret": TEST_TOTP_SECRET,
            "enabled": True,
            "confirmed_at": timezone.now(),
            "last_used_counter": None,
            "trusted_until": None,
        },
    )
    return device


def _seed_products():
    seeded = []
    for row in TEST_PRODUCTS:
        product, _ = products.objects.update_or_create(
            slug=row["slug"],
            defaults={
                "name": row["name"],
                "description": row["description"],
                "price": row["price"],
                "size": row["size"],
                "stock": row["stock"],
                "category": row["category"],
                "is_active": True,
            },
        )
        seeded.append(product)
    return {product.slug: product for product in seeded}


def _seed_coupons():
    now = timezone.now()
    seeded = []
    for row in TEST_COUPONS:
        coupon, _ = Coupon.objects.update_or_create(
            code=row["code"],
            defaults={
                "discount_type": row["discount_type"],
                "discount_value": row["discount_value"],
                "minimum_order_amount": row["minimum_order_amount"],
                "active": True,
                "valid_from": now - timedelta(days=1),
                "valid_until": now + timedelta(days=30),
            },
        )
        seeded.append(coupon)
    return seeded


def _seed_shipping():
    providers = {}
    for row in TEST_PROVIDERS:
        provider, _ = ShippingProvider.objects.update_or_create(
            code=row["code"],
            defaults={
                "name": row["name"],
                "tracking_url_template": row["tracking_url_template"],
                "is_active": True,
            },
        )
        providers[provider.code] = provider

    methods = {}
    for row in TEST_METHODS:
        method, _ = ShippingMethod.objects.update_or_create(
            code=row["code"],
            defaults={"name": row["name"], "is_active": True},
        )
        methods[method.code] = method

    for row in TEST_RATES:
        # The (method, region, postal_code_prefix) triple is the schema's own
        # uniqueness key, so it is the natural key here too.
        ShippingRate.objects.update_or_create(
            method=methods[row["method_code"]],
            region=row["region"],
            postal_code_prefix=row["postal_code_prefix"],
            defaults={"amount": row["amount"], "is_active": True},
        )
    return providers, methods


def _ensure_order_number(order):
    """Give a freshly seeded order its checkout-format reference.

    Reuses the checkout generator rather than re-implementing the format, and
    retries on the unique constraint exactly as checkout does: the database is
    the concurrency authority for a generated unique value.
    """
    if order.order_number:
        return
    for _ in range(ORDER_NUMBER_ATTEMPTS):
        order.order_number = _generate_order_number()
        try:
            with transaction.atomic():
                order.save(update_fields=["order_number"])
            return
        except IntegrityError:
            continue
    raise CommandError(
        f"[seed_e2e] could not mint a unique order_number for {order.idempotency_key}"
    )


def _seed_orders(user, product_by_slug, method_by_code, rows=TEST_ORDERS):
    seeded = {}
    for row in rows:
        product = product_by_slug[row["item_slug"]]
        quantity = row["item_quantity"]
        merchandise_total = product.price * quantity
        # payment/fulfilment dimensions come from the machine's own mapping, so
        # a "confirmed" order carries the captured payment the shipped
        # precondition requires and a "delivered" one is fulfilled -- a seed row
        # no writer could have produced would make the D-01 admin walk refuse.
        order, _ = Order.objects.update_or_create(
            user=user,
            idempotency_key=row["idempotency_key"],
            defaults={
                "status": row["status"],
                "payment_status": payment_for_status(row["status"]),
                "fulfilment_status": fulfilment_for_status(row["status"]),
                "full_name": row["full_name"],
                "phone": row["phone"],
                "address": row["address"],
                "city": row["city"],
                "state": row["state"],
                "pincode": row["pincode"],
                "shipping_method": method_by_code[row["shipping_method_code"]],
                "shipping_method_code": row["shipping_method_code"],
                "shipping_amount": row["shipping_amount"],
                "discount_amount": Decimal("0.00"),
                "total_amount": merchandise_total + row["shipping_amount"],
                "currency": "INR",
            },
        )
        _ensure_order_number(order)
        OrderItem.objects.update_or_create(
            order=order,
            product=product,
            defaults={
                "product_name": product.name,
                "variant_name": product.name,
                "sku": "",
                "price": product.price,
                "quantity": quantity,
                "subtotal": merchandise_total,
                "currency": "INR",
            },
        )
        seeded[row["label"]] = order
    return seeded


def _seed_shipments(order_by_label, provider_by_code, rows=TEST_SHIPMENTS):
    seeded = []
    for row in rows:
        shipment, _ = Shipment.objects.update_or_create(
            tracking_number=row["tracking_number"],
            defaults={
                "order": order_by_label[row["order_label"]],
                "provider": provider_by_code[row["provider_code"]],
                "carrier": row["carrier"],
                "status": row["status"],
            },
        )
        seeded.append(shipment)
    return seeded


def seed(scenario="full"):
    """Upsert the whole E2E set; returns the rows the summary prints.

    ``full`` is the original A/B/C walk. ``lifecycle`` builds the same shared
    catalogue and accounts but three orders chosen for the D-01 lifecycle
    moves (dispatch, split-delivery gate, unpaid refusal). Both paths share
    the upsert functions, so idempotency and cleanup ownership are identical.
    """
    order_rows = TEST_ORDERS if scenario == "full" else LIFECYCLE_ORDERS
    shipment_rows = TEST_SHIPMENTS if scenario == "full" else LIFECYCLE_SHIPMENTS
    with transaction.atomic():
        user = _seed_superadmin()
        customer = _seed_customer()
        device = _seed_totp_device(user)
        product_by_slug = _seed_products()
        coupons = _seed_coupons()
        providers, methods = _seed_shipping()
        orders = _seed_orders(user, product_by_slug, methods, rows=order_rows)
        shipments = _seed_shipments(orders, providers, rows=shipment_rows)
    return {
        "scenario": scenario,
        "user": user,
        "customer": customer,
        "device": device,
        "otpauth_uri": totp.otpauth_uri(
            device.secret, user.username, issuer=TEST_TOTP_ISSUER
        ),
        "products": list(product_by_slug.values()),
        "coupons": coupons,
        "orders": [orders[row["label"]] for row in order_rows],
        "orders_by_label": orders,
        "shipments": shipments,
        "shipments_by_tracking": {
            shipment.tracking_number: shipment for shipment in shipments
        },
    }


def clean():
    """Delete only the rows this command owns, by their natural keys."""
    order_rows = TEST_ORDERS + LIFECYCLE_ORDERS
    shipment_rows = TEST_SHIPMENTS + LIFECYCLE_SHIPMENTS
    with transaction.atomic():
        # Shipments first: ShippingProvider is PROTECTed by a live parcel.
        Shipment.objects.filter(
            tracking_number__in=[row["tracking_number"] for row in shipment_rows]
        ).delete()
        Order.objects.filter(
            user__username=SUPERADMIN_USERNAME,
            idempotency_key__in=[row["idempotency_key"] for row in order_rows],
        ).delete()
        products.objects.filter(
            slug__in=[row["slug"] for row in TEST_PRODUCTS]
        ).delete()
        Coupon.objects.filter(code__in=[row["code"] for row in TEST_COUPONS]).delete()
        ShippingRate.objects.filter(
            method__code__in=[row["code"] for row in TEST_METHODS]
        ).delete()
        ShippingMethod.objects.filter(
            code__in=[row["code"] for row in TEST_METHODS]
        ).delete()
        # PROTECT, not CASCADE: a parcel the owner linked to a seeded provider
        # is not this command's row, so it keeps the provider rather than
        # turning cleanup into a ProtectedError (or into deleting that parcel).
        ShippingProvider.objects.filter(
            code__in=[row["code"] for row in TEST_PROVIDERS],
            shipments__isnull=True,
        ).delete()
        # Deleting the user cascades its OneToOne TOTPDevice, so cleanup
        # removes the seeded factor with the account it belongs to.
        User.objects.filter(username=SUPERADMIN_USERNAME).delete()
        # The storefront customer goes only while it owns no orders: an order
        # the owner placed during the walk is not this command's row, and
        # deleting the account would cascade it away.
        User.objects.filter(username=CUSTOMER_USERNAME, orders__isnull=True).delete()


class Command(BaseCommand):
    help = (
        "Seed (or with --cleanup, remove) the SPEC-E2E-01 owner test data: "
        "superadmin, storefront customer, products, coupons, shipping "
        "providers/methods/rates, and either the original three orders or "
        "(--scenario lifecycle) the D-09 lifecycle set. TEST DATA ONLY."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--scenario",
            choices=("full", "lifecycle"),
            default="full",
            help=(
                "Which order set to seed: 'full' (the original A/B/C walk, "
                "the default) or 'lifecycle' (D-09: dispatch, split-delivery "
                "gate, unpaid refusal)."
            ),
        )
        parser.add_argument(
            "--cleanup",
            action="store_true",
            help=(
                "Delete only the rows this command owns (by username, slug, "
                "coupon code, method/provider code and tracking number). "
                "Idempotent; unmarked rows are never touched."
            ),
        )

    def handle(self, *args, **options):
        if options["cleanup"]:
            clean()
            self.stdout.write("[seed_e2e] removed the E2E test rows (test data only)")
            return
        summary = seed(scenario=options["scenario"])
        self._print_summary(summary)

    def _print_summary(self, summary):
        user = summary["user"]
        write = self.stdout.write
        write("[seed_e2e] TEST DATA ONLY -- seeded the owner E2E set")
        write(f"  superadmin: {user.username} / {SUPERADMIN_PASSWORD}")
        write(f"  email: {user.email}")
        write(
            "  customer (storefront): "
            f"{summary['customer'].username} / {CUSTOMER_PASSWORD}"
        )
        write(f"  email: {summary['customer'].email}")
        write("  admin MFA -- TEST-ONLY, must never exist outside this local test DB:")
        write(f"    otpauth URI: {summary['otpauth_uri']}")
        write(
            "    current code: manage.py totp_code "
            f"{SUPERADMIN_USERNAME} (or any enrolled authenticator)"
        )
        write("  products:")
        for product in summary["products"]:
            write(
                f"    {product.slug}: {product.name}, {product.price} INR, "
                f"{product.size} ml, stock {product.stock}, {product.category}"
            )
        write("  coupons:")
        for coupon in summary["coupons"]:
            write(
                f"    {coupon.code}: {coupon.discount_type} "
                f"{coupon.discount_value}, minimum {coupon.minimum_order_amount}"
            )
        write("  orders:")
        for order in summary["orders"]:
            write(
                f"    {order.order_number} [{order.status}] "
                f"total {order.total_amount} {order.currency} "
                f"shipping {order.shipping_amount}"
            )
            write(
                "      admin: " + reverse("admin:orders_order_change", args=[order.pk])
            )
        write("  shipments:")
        for shipment in summary["shipments"]:
            write(
                f"    #{shipment.pk} {shipment.tracking_number} "
                f"[{shipment.status}] {shipment.tracking_url}"
            )
            write(
                "      admin: "
                + reverse("admin:shipping_shipment_change", args=[shipment.pk])
            )
        write("  admin URLs:")
        for label, url_name in (
            ("dashboard", "admin:index"),
            ("orders", "admin:orders_order_changelist"),
            ("coupons", "admin:orders_coupon_changelist"),
            ("products", "admin:products_products_changelist"),
            ("providers", "admin:shipping_shippingprovider_changelist"),
            ("methods", "admin:shipping_shippingmethod_changelist"),
            ("rates", "admin:shipping_shippingrate_changelist"),
            ("shipments", "admin:shipping_shipment_changelist"),
        ):
            write(f"    {label}: {reverse(url_name)}")
        if summary["scenario"] == "lifecycle":
            self._print_lifecycle_outcomes(summary)

    def _print_lifecycle_outcomes(self, summary):
        """The three moves a human should see, written before the walk."""
        orders = summary["orders_by_label"]
        parcels = summary["shipments_by_tracking"]
        write = self.stdout.write
        l2_order = reverse("admin:orders_order_change", args=[orders["L2"].pk])
        l1_parcel = reverse(
            "admin:shipping_shipment_change", args=[parcels["LCY000000001"].pk]
        )
        l2_delivered = reverse(
            "admin:shipping_shipment_change", args=[parcels["LCY000000002"].pk]
        )
        l2_in_transit = reverse(
            "admin:shipping_shipment_change", args=[parcels["LCY000000003"].pk]
        )
        l3_parcel = reverse(
            "admin:shipping_shipment_change", args=[parcels["LCY000000004"].pk]
        )
        write("  expected outcomes (lifecycle scenario):")
        write(
            "    L1: set its label_created parcel to dispatched; the order "
            "moves confirmed -> shipped."
        )
        write(f"      L1 parcel: {l1_parcel}")
        write(
            "    L2: the delivered parcel alone must NOT move the order to "
            f"delivered while the second parcel is in_transit ({l2_order}). "
            "After BOTH parcels are delivered the order moves to delivered."
        )
        write(f"      L2 delivered parcel: {l2_delivered}")
        write(f"      L2 in-transit parcel: {l2_in_transit}")
        write(
            "    L3: set its label_created parcel to dispatched; the parcel "
            "saves but the order stays pending because the order is unpaid."
        )
        write(f"      L3 parcel: {l3_parcel}")

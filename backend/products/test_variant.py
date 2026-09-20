"""SPEC-8-02a [R-8.7]: ProductVariant schema core (spec section 8).

The SKU is merchant-entered (no generation path exists), so there is no
IntegrityError-retry loop to prove — conventions.md:17 applies to generation
paths. The globally-unique constraint plus its backing unique index are the
concurrency authority and are proven deterministically the same way Order's
order_number race is (orders/test_order_number.py): a forced-collision
IntegrityError pin plus a live DB constraint introspection. Razorpay stays
mocked; no test touches the network.
"""

from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, models
from django.db.models import DecimalField
from django.test import tag

from common.admin import RoleAwareModelAdmin
from common.testing import ApiTestCase
from products.models import ProductVariant, products


@tag("products")
class ProductVariantSchemaTests(ApiTestCase):
    """Field-set pin, FK policy and the DB-level SKU uniqueness proof."""

    def setUp(self):
        self.product = self.make_product(name="Rose Aurum")
        self.other_product = self.make_product(name="Oud Royale")

    def make_variant(self, product=None, sku="RA-STD-001", **overrides):
        fields = dict(
            product=product or self.product,
            name="Standard 50ml",
            sku=sku,
            price=Decimal("449.00"),
            stock=7,
        )
        fields.update(overrides)
        return ProductVariant.objects.create(**fields)

    def test_field_set_is_pinned(self):
        """Spec 8.3 prescribes exactly this core: the FK to the product, the
        attributes the order-item snapshot list reads (variant name/options
        2503, SKU 2505, unit price 2507), on-hand stock and the distinct
        created/updated timestamps (2523). A new column must force a
        conscious decision here, never auto-appear."""
        names = {field.name for field in ProductVariant._meta.concrete_fields}
        self.assertEqual(
            names,
            {
                "id",
                "product",
                "name",
                "sku",
                "price",
                "stock",
                "created_at",
                "updated_at",
            },
        )

    def test_fk_targets_product_with_cascade_and_reverse_accessor(self):
        field = ProductVariant._meta.get_field("product")
        self.assertIs(field.related_model, products)
        self.assertIs(field.remote_field.on_delete, models.CASCADE)

        variant = self.make_variant()
        self.assertIn(variant, self.product.variants.all())

        # explicit deletion policy (2485): the catalogue child goes with the
        # parent — what orders keep is the OrderItem snapshot (SPEC-8-02b,
        # 2497), not a live FK row
        self.product.delete()
        self.assertEqual(ProductVariant.objects.count(), 0)

    def test_price_is_exact_decimal_money_and_optional(self):
        """2489 + conventions.md:15: the variant-specific price (spec 6.2's
        "variant-specific prices") is exact decimal money; NULL means the
        product's own price applies — the read-side wiring is SPEC-8-02b's
        OrderItem work, not invented here."""
        field = ProductVariant._meta.get_field("price")
        self.assertIsInstance(field, DecimalField)
        self.assertEqual((field.max_digits, field.decimal_places), (10, 2))
        self.assertTrue(field.null)

        priced = self.make_variant(sku="RA-DELUXE-002")
        priced.refresh_from_db()
        self.assertEqual(priced.price, Decimal("449.00"))

        bare = self.make_variant(sku="RA-PLAIN-003", price=None)
        bare.refresh_from_db()
        self.assertIsNone(bare.price)

    def test_created_and_updated_timestamps_are_distinct(self):
        """2523: distinct created/updated storage — created_at is frozen and
        updated_at advances on a later save."""
        variant = self.make_variant()
        created = variant.created_at
        self.assertIsNotNone(created)
        self.assertIsNotNone(variant.updated_at)

        variant.name = "Deluxe 100ml"
        variant.save()
        variant.refresh_from_db()

        self.assertEqual(variant.created_at, created)
        self.assertGreaterEqual(variant.updated_at, created)

    def test_string_representation_names_product_and_sku(self):
        variant = self.make_variant()
        self.assertEqual(str(variant), f"Rose Aurum - Standard 50ml ({variant.sku})")


@tag("products")
class ProductVariantSkuUniquenessTests(ApiTestCase):
    """[R-8.7] The SKU is globally unique: constraint + index at the DB.

    Merchant-entered, so there is no generation retry to exercise; the
    constraint is the race authority, pinned deterministically.
    """

    def setUp(self):
        self.product = self.make_product(name="Rose Aurum")
        self.other_product = self.make_product(name="Oud Royale")

    def make_variant(self, product=None, sku="RA-STD-001", **overrides):
        fields = dict(
            product=product or self.product,
            name="Standard 50ml",
            sku=sku,
        )
        fields.update(overrides)
        return ProductVariant.objects.create(**fields)

    def test_sku_is_globally_unique_across_products(self):
        """2481: uniqueness is table-global, not per-product — the same SKU
        under a DIFFERENT product is rejected too. The model-level flag and
        the DB behaviour agree."""
        self.assertTrue(ProductVariant._meta.get_field("sku").unique)
        self.make_variant(sku="SHARED-001")
        self.assertEqual(ProductVariant.objects.count(), 1)

        with self.assertRaises(IntegrityError):
            self.make_variant(product=self.other_product, sku="SHARED-001")

    def test_sku_unique_constraint_lands_at_the_db_level(self):
        """2481 + 2553: the pin is a real DB-level unique constraint on sku
        — its backing unique index is how that constraint is implemented
        (the same reading Order.order_number pins; Django's SQLite
        introspection labels constraint-origin indexes ``index=False``, the
        physical auto-index exists regardless)."""
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, ProductVariant._meta.db_table
            )
        sku_constraints = [
            info for info in constraints.values() if info["columns"] == ["sku"]
        ]
        self.assertTrue(
            any(info["unique"] for info in sku_constraints),
            f"no unique constraint on sku: {sku_constraints}",
        )


@tag("products")
class ProductVariantStockBoundaryTests(ApiTestCase):
    """Constraint 4 of SPEC-8-02a: variant stock is schema-core only.

    ``products.stock`` stays the ONLY order-time authority — checkout and
    verify_payment never read ``ProductVariant.stock``, so adding variant
    rows cannot change order behaviour. SPEC-6-13 owns the reconciliation.
    """

    def test_stock_defaults_to_zero(self):
        product = self.make_product(name="Rose Aurum")
        variant = ProductVariant.objects.create(
            product=product, name="Bare", sku="RA-BARE-001", price=None
        )
        self.assertEqual(variant.stock, 0)

    def test_zero_stock_variant_never_blocks_or_consumes_a_checkout(self):
        """A paid checkout with a zero-stock variant succeeds on the
        product's stock, decrements only the product, and leaves the
        variant's own count untouched."""
        product = self.make_product(name="Inert Rose")
        variant = ProductVariant.objects.create(
            product=product, name="Standard", sku="IR-STD-001", stock=0
        )

        self.make_user("buyer")
        self.api_login("buyer")
        self.razorpay_mock()
        self.seed_session_cart([(product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)

        payment = self.client.post(
            "/api/orders/payment/", {"order_id": res.data["id"]}, format="json"
        )
        self.assertEqual(payment.status_code, 200, payment.data)
        verified = self.client.post(
            "/api/orders/payment/verify/",
            {
                "order_id": res.data["id"],
                "razorpay_order_id": "order_TEST0001",
                "razorpay_payment_id": "pay_TEST0001",
                "razorpay_signature": "sig",
            },
            format="json",
        )
        self.assertEqual(verified.status_code, 200, verified.data)

        product.refresh_from_db()
        variant.refresh_from_db()
        self.assertEqual(product.stock, 9)   # product stock is the authority
        self.assertEqual(variant.stock, 0)   # variant stock untouched


@tag("products")
class ProductVariantAdminTests(ApiTestCase):
    """Bare registration pin (SPEC-8-02a, constraint 2): the entity is
    visible in the admin with default ModelAdmin behaviour — SPEC-6-08 owns
    the role matrix/fieldsets/actions depth."""

    def test_variant_is_registered_with_a_bare_modeladmin(self):
        registration = admin.site._registry[ProductVariant]
        self.assertIsInstance(registration, admin.ModelAdmin)
        # deliberately not the role-aware base yet: no capability map until
        # SPEC-6-08 makes that a conscious catalogue decision
        self.assertNotIsInstance(registration, RoleAwareModelAdmin)

    def test_variant_changelist_is_reachable(self):
        User.objects.create_superuser(
            "vadmin", "vadmin@example.com", "S3cure-Passphrase!"
        )
        self.assertTrue(
            self.client.login(username="vadmin", password="S3cure-Passphrase!")
        )
        product = self.make_product(name="Rose Aurum")
        ProductVariant.objects.create(
            product=product, name="Standard 50ml", sku="RA-STD-001"
        )
        res = self.client.get("/admin/products/productvariant/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "RA-STD-001")

"""SPEC-E2E-01: ``manage.py seed_e2e``, the owner's end-to-end test data set.

Pins the full seeded state with hand-written literals (never recomputed from
the command's own tables), the idempotent re-run, the cleanup ownership
boundary, and the summary a human uses to sign in and open the admin.
"""

import re
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from orders.management.commands import seed_e2e
from orders.models import Coupon, Order, OrderItem
from products.models import products
from shipping.models import (
    Shipment,
    ShippingMethod,
    ShippingProvider,
    ShippingRate,
)

SUPERADMIN_PASSWORD = "SuperAdmin#2026"
ORDER_NUMBER_PATTERN = re.compile(r"^ORD-\d{4}-[0-9A-F]{8}$")


def _run(*argv):
    out = StringIO()
    call_command("seed_e2e", *argv, stdout=out)
    return out.getvalue()


def _seed_then_order_numbers():
    _run()
    return {
        order.idempotency_key: order.order_number
        for order in Order.objects.filter(
            user__username="superadmin",
            idempotency_key__startswith="seed-e2e-order-",
        )
    }


class SeedE2ETests(TestCase):
    def test_seed_creates_the_expected_owner_set(self):
        out = _run()

        admin = User.objects.get(username="superadmin")
        self.assertEqual(admin.email, "superadmin@example.com")
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_active)
        self.assertNotEqual(admin.password, SUPERADMIN_PASSWORD)
        self.assertTrue(admin.check_password(SUPERADMIN_PASSWORD))
        self.assertIsNotNone(
            authenticate(username="superadmin", password=SUPERADMIN_PASSWORD)
        )

        marj = products.objects.get(slug="marj")
        self.assertEqual(marj.name, "Marj")
        self.assertEqual(marj.price, Decimal("2499.00"))
        self.assertEqual(marj.size, 50)
        self.assertEqual(marj.stock, 50)
        self.assertEqual(marj.category, "floral")
        self.assertTrue(marj.is_active)
        oud = products.objects.get(slug="oud-noir")
        self.assertEqual(oud.name, "Oud Noir")
        self.assertEqual(oud.price, Decimal("4999.00"))
        self.assertEqual(oud.size, 100)
        self.assertEqual(oud.stock, 20)
        self.assertEqual(oud.category, "woody")
        self.assertTrue(oud.is_active)

        free_ship = Coupon.objects.get(code="FREESHIP")
        self.assertEqual(free_ship.discount_type, "free_shipping")
        self.assertEqual(free_ship.discount_value, Decimal("0.00"))
        self.assertTrue(free_ship.active)
        self.assertLessEqual(free_ship.valid_from, timezone.now())
        self.assertGreater(free_ship.valid_until, timezone.now())
        save10 = Coupon.objects.get(code="SAVE10")
        self.assertEqual(save10.discount_type, "percentage")
        self.assertEqual(save10.discount_value, Decimal("10.00"))

        delhivery = ShippingProvider.objects.get(code="delhivery")
        self.assertEqual(delhivery.name, "Delhivery")
        self.assertEqual(
            delhivery.tracking_url_template,
            "https://www.delhivery.com/track/package/{tracking_number}",
        )
        bluedart = ShippingProvider.objects.get(code="bluedart")
        self.assertEqual(bluedart.name, "Blue Dart")
        self.assertEqual(
            bluedart.tracking_url_template,
            "https://www.bluedart.com/tracking/{tracking_number}",
        )

        standard = ShippingMethod.objects.get(code="standard")
        express = ShippingMethod.objects.get(code="express")
        self.assertTrue(standard.is_active)
        self.assertTrue(express.is_active)
        self.assertEqual(
            set(
                ShippingRate.objects.filter(method=standard).values_list(
                    "amount", "region", "postal_code_prefix"
                )
            ),
            {
                (Decimal("49.00"), "Maharashtra", "400"),
                (Decimal("99.00"), "", ""),
            },
        )
        self.assertEqual(
            ShippingRate.objects.get(method=express).amount, Decimal("149.00")
        )
        self.assertEqual(ShippingRate.objects.get(method=express).region, "")

        orders = Order.objects.filter(
            user__username="superadmin",
            idempotency_key__startswith="seed-e2e-order-",
        ).order_by("idempotency_key")
        self.assertEqual(orders.count(), 3)
        by_status = {order.status: order for order in orders}

        order_a = by_status["confirmed"]
        self.assertEqual(order_a.full_name, "Test Customer")
        self.assertEqual(order_a.phone, "9999999999")
        self.assertEqual(order_a.address, "1 Marine Drive")
        self.assertEqual(order_a.city, "Mumbai")
        self.assertEqual(order_a.state, "Maharashtra")
        self.assertEqual(order_a.pincode, "400001")
        self.assertEqual(order_a.shipping_method, standard)
        self.assertEqual(order_a.shipping_amount, Decimal("49.00"))
        self.assertEqual(order_a.total_amount, Decimal("2548.00"))
        self.assertEqual(order_a.discount_amount, Decimal("0.00"))
        self.assertEqual(order_a.currency, "INR")
        # The D-01 walk needs the shipped precondition to hold.
        self.assertEqual(order_a.payment_status, "captured")
        item = order_a.items.get()
        self.assertEqual(item.product, marj)
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.price, Decimal("2499.00"))
        self.assertEqual(item.subtotal, Decimal("2499.00"))

        order_b = by_status["pending"]
        self.assertEqual(order_b.pincode, "411001")
        self.assertEqual(order_b.total_amount, Decimal("5098.00"))
        self.assertEqual(order_b.payment_status, "pending")
        self.assertEqual(order_b.items.get().product, oud)

        order_c = by_status["delivered"]
        self.assertEqual(order_c.pincode, "110001")
        self.assertEqual(order_c.total_amount, Decimal("5097.00"))
        self.assertEqual(order_c.payment_status, "captured")
        self.assertEqual(order_c.fulfilment_status, "fulfilled")
        self.assertEqual(order_c.items.get().quantity, 2)

        for order in orders:
            self.assertRegex(order.order_number, ORDER_NUMBER_PATTERN)
            self.assertEqual(order.items.count(), 1)

        shipments = list(Shipment.objects.order_by("id"))
        self.assertEqual(len(shipments), 3)
        by_tracking = {shipment.tracking_number: shipment for shipment in shipments}
        self.assertEqual(
            set(by_tracking), {"DLV123456789", "DLV000000002", "BD987654321"}
        )
        parcel_a = by_tracking["DLV123456789"]
        self.assertEqual(parcel_a.order, order_a)
        self.assertEqual(parcel_a.provider, delhivery)
        self.assertEqual(parcel_a.carrier, "Delhivery")
        self.assertEqual(parcel_a.status, "label_created")
        self.assertEqual(
            parcel_a.tracking_url,
            "https://www.delhivery.com/track/package/DLV123456789",
        )
        parcel_b = by_tracking["DLV000000002"]
        self.assertEqual(parcel_b.order, order_b)
        self.assertEqual(parcel_b.status, "label_created")
        parcel_c = by_tracking["BD987654321"]
        self.assertEqual(parcel_c.order, order_c)
        self.assertEqual(parcel_c.provider, bluedart)
        self.assertEqual(parcel_c.status, "delivered")
        self.assertEqual(
            parcel_c.tracking_url, "https://www.bluedart.com/tracking/BD987654321"
        )
        # No transition was driven by the seed: the owner performs the D-01
        # walk in the admin.
        self.assertFalse(parcel_a.events.exists())

        self.assertIn("superadmin / SuperAdmin#2026", out)
        self.assertIn(parcel_a.tracking_url, out)
        self.assertIn(order_a.order_number, out)
        self.assertIn("/admin/orders/order/", out)
        self.assertIn("/admin/shipping/shipment/", out)
        self.assertIn("TEST DATA ONLY", out)

    def test_second_run_is_idempotent_and_preserves_order_numbers(self):
        first_numbers = _seed_then_order_numbers()
        counts = {
            "users": User.objects.filter(username="superadmin").count(),
            "products": products.objects.filter(
                slug__in=["marj", "oud-noir"]
            ).count(),
            "coupons": Coupon.objects.filter(
                code__in=["FREESHIP", "SAVE10"]
            ).count(),
            "providers": ShippingProvider.objects.filter(
                code__in=["delhivery", "bluedart"]
            ).count(),
            "methods": ShippingMethod.objects.filter(
                code__in=["standard", "express"]
            ).count(),
            "rates": ShippingRate.objects.count(),
            "orders": Order.objects.filter(
                user__username="superadmin",
                idempotency_key__startswith="seed-e2e-order-",
            ).count(),
            "items": OrderItem.objects.count(),
            "shipments": Shipment.objects.count(),
        }
        self.assertEqual(
            counts,
            {
                "users": 1,
                "products": 2,
                "coupons": 2,
                "providers": 2,
                "methods": 2,
                "rates": 3,
                "orders": 3,
                "items": 3,
                "shipments": 3,
            },
        )

        _run()

        second_numbers = _seed_then_order_numbers()
        self.assertEqual(first_numbers, second_numbers)
        self.assertEqual(Order.objects.count(), 3)
        self.assertEqual(OrderItem.objects.count(), 3)
        self.assertEqual(Shipment.objects.count(), 3)
        self.assertEqual(ShippingRate.objects.count(), 3)

    def test_a_lost_order_number_race_retries_and_exhaustion_raises(self):
        # The generator is the checkout one; a duplicate hits the unique
        # constraint and the command must retry, exactly as checkout does.
        taken = "ORD-2026-AAAAAAAA"
        holder = User.objects.create_user(username="holder", password="fixture")
        Order.objects.create(
            user=holder,
            full_name="Holder",
            phone="9999999999",
            address="1 Holder Street",
            city="Holder City",
            state="Holder State",
            pincode="000000",
            total_amount=Decimal("1.00"),
            order_number=taken,
        )
        with mock.patch.object(
            seed_e2e,
            "_generate_order_number",
            side_effect=[
                taken,
                "ORD-2026-BBBBBBBB",
                "ORD-2026-CCCCCCCC",
                "ORD-2026-DDDDDDDD",
            ],
        ) as generator:
            _run()
        self.assertEqual(generator.call_count, 4)
        self.assertTrue(
            Order.objects.filter(order_number="ORD-2026-BBBBBBBB").exists()
        )

        _run("--cleanup")
        Order.objects.filter(order_number=taken).delete()
        with mock.patch.object(
            seed_e2e, "_generate_order_number", return_value=taken
        ):
            Order.objects.create(
                user=holder,
                full_name="Holder",
                phone="9999999999",
                address="1 Holder Street",
                city="Holder City",
                state="Holder State",
                pincode="000000",
                total_amount=Decimal("1.00"),
                order_number=taken,
            )
            with self.assertRaises(CommandError):
                _run()


class SeedE2ECleanupTests(TestCase):
    def _foreign_rows(self):
        buyer = User.objects.create_user(username="other-buyer", password="fixture")
        product = products.objects.create(
            name="Bergamot Noir",
            slug="bergamot-noir",
            description="A real product.",
            price=Decimal("899.00"),
            size=50,
            stock=7,
            category="oriental",
        )
        coupon = Coupon.objects.create(
            code="OTHER10",
            discount_type="percentage",
            discount_value=Decimal("10.00"),
            minimum_order_amount=Decimal("0.00"),
            active=True,
            valid_from=timezone.now() - timedelta(days=1),
            valid_until=timezone.now() + timedelta(days=30),
        )
        order = Order.objects.create(
            user=buyer,
            full_name="Other Buyer",
            phone="1234567890",
            address="9 Other Street",
            city="Other City",
            state="Other State",
            pincode="111111",
            total_amount=Decimal("899.00"),
        )
        return buyer, product, coupon, order

    def test_cleanup_removes_only_the_seeded_rows(self):
        buyer, product, coupon, foreign_order = self._foreign_rows()
        _run()
        provider = ShippingProvider.objects.get(code="delhivery")
        # A parcel the owner linked to a seeded provider AFTER the seed: it is
        # not the command's row, so PROTECT must not make cleanup crash.
        foreign_shipment = Shipment.objects.create(
            order=foreign_order,
            tracking_number="OWNER0001",
            provider=provider,
            carrier="Delhivery",
            status="label_created",
        )

        _run("--cleanup")

        self.assertFalse(User.objects.filter(username="superadmin").exists())
        self.assertFalse(
            products.objects.filter(slug__in=["marj", "oud-noir"]).exists()
        )
        self.assertFalse(
            Coupon.objects.filter(code__in=["FREESHIP", "SAVE10"]).exists()
        )
        self.assertFalse(
            ShippingMethod.objects.filter(
                code__in=["standard", "express"]
            ).exists()
        )
        self.assertFalse(
            ShippingProvider.objects.filter(code="bluedart").exists()
        )
        self.assertFalse(
            Order.objects.filter(
                idempotency_key__startswith="seed-e2e-order-"
            ).exists()
        )
        self.assertFalse(
            Shipment.objects.filter(
                tracking_number__in=["DLV123456789", "DLV000000002", "BD987654321"]
            ).exists()
        )

        # Foreign rows survive untouched.
        self.assertTrue(User.objects.filter(pk=buyer.pk).exists())
        self.assertTrue(products.objects.filter(pk=product.pk).exists())
        self.assertTrue(Coupon.objects.filter(pk=coupon.pk).exists())
        self.assertTrue(Order.objects.filter(pk=foreign_order.pk).exists())
        self.assertTrue(Shipment.objects.filter(pk=foreign_shipment.pk).exists())
        # The provider is still referenced by the foreign parcel, so it stays.
        self.assertTrue(ShippingProvider.objects.filter(pk=provider.pk).exists())

    def test_cleanup_is_idempotent_and_cleanup_on_empty_db_is_a_noop(self):
        _run("--cleanup")
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(products.objects.count(), 0)

        _run()
        _run("--cleanup")
        _run("--cleanup")
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Shipment.objects.count(), 0)
        self.assertEqual(ShippingRate.objects.count(), 0)

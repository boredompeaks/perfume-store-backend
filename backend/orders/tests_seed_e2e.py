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

from accounts.models import TOTPDevice
from common import totp
from orders.management.commands import seed_e2e
from orders.models import Coupon, Order, OrderItem
from products.models import products
from shipping.lifecycle import sync_order_status
from shipping.models import (
    Shipment,
    ShippingMethod,
    ShippingProvider,
    ShippingRate,
)

SUPERADMIN_PASSWORD = "SuperAdmin#2026"
ORDER_NUMBER_PATTERN = re.compile(r"^ORD-\d{4}-[0-9A-F]{8}$")
# Hand-written literals, never derived from the command's constants: a pin
# that recomputes its expected value from the code under test agrees with a
# wrong value from both sides.
SEEDED_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
SEEDED_OTPAUTH_URI = (
    "otpauth://totp/Perfume%20Store:superadmin"
    "?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    "&issuer=Perfume%20Store&algorithm=SHA1&digits=6&period=30"
)


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

        customer = User.objects.get(username="customer")
        self.assertEqual(customer.email, "customer@example.com")
        self.assertFalse(customer.is_staff)
        self.assertFalse(customer.is_superuser)
        self.assertTrue(customer.is_active)
        self.assertNotEqual(customer.password, "Customer#2026")
        self.assertTrue(customer.check_password("Customer#2026"))
        # The owner signs in on the storefront, so the customer door must
        # admit it: the staff door refuses non-staff accounts.
        res = self.client.post(
            "/api/accounts/storefront/login/",
            {"username": "customer", "password": "Customer#2026"},
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn("access", res.json())

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
        self.assertIn("customer (storefront): customer / Customer#2026", out)
        self.assertIn(parcel_a.tracking_url, out)
        self.assertIn(order_a.order_number, out)
        self.assertIn("/admin/orders/order/", out)
        self.assertIn("/admin/shipping/shipment/", out)
        self.assertIn("TEST DATA ONLY", out)

    def test_seed_enrolls_exactly_one_confirmed_enabled_device(self):
        out = _run()

        devices = TOTPDevice.objects.filter(user__username="superadmin")
        self.assertEqual(devices.count(), 1)
        device = devices.get()
        self.assertEqual(device.secret, SEEDED_TOTP_SECRET)
        self.assertTrue(device.enabled)
        self.assertIsNotNone(device.confirmed_at)
        self.assertIsNone(device.last_used_counter)
        self.assertIsNone(device.trusted_until)
        self.assertEqual(TOTPDevice.active_for(device.user), device)

        self.assertIn(SEEDED_OTPAUTH_URI, out)
        self.assertIn("TEST-ONLY", out)
        self.assertNotIn(SEEDED_TOTP_SECRET, out.split("otpauth URI:")[0])

    def test_reseed_resets_the_replay_watermark_and_keeps_one_device(self):
        _run()
        device = TOTPDevice.objects.get(user__username="superadmin")
        # Simulate a consumed code and a browser trust grant: both must be
        # cleared by the next seed run, or the printed current code would be
        # refused (replay guard) or the challenge skipped (stale grant).
        device.last_used_counter = 56_666_666
        device.trusted_until = timezone.now() + timedelta(days=1)
        device.save(update_fields=["last_used_counter", "trusted_until"])

        _run()

        self.assertEqual(
            TOTPDevice.objects.filter(user__username="superadmin").count(), 1
        )
        device.refresh_from_db()
        self.assertEqual(device.secret, SEEDED_TOTP_SECRET)
        self.assertTrue(device.enabled)
        self.assertIsNotNone(device.confirmed_at)
        self.assertIsNone(device.last_used_counter)
        self.assertIsNone(device.trusted_until)
        at_time = totp.now()
        code = totp.totp(device.secret, at_time)
        self.assertIsNotNone(
            totp.verify_code(
                device.secret,
                code,
                at_time,
                last_used_counter=device.last_used_counter,
            )
        )

    def test_seeded_device_admits_the_admin_login(self):
        _run()
        device = TOTPDevice.objects.get(user__username="superadmin")
        code = totp.totp(device.secret, totp.now())

        res = self.client.post(
            "/admin/login/",
            {
                "username": "superadmin",
                "password": SUPERADMIN_PASSWORD,
                "totp": code,
                "next": "/admin/",
            },
        )

        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], "/admin/")
        self.assertIn("_auth_user_id", self.client.session)
        device.refresh_from_db()
        self.assertIsNotNone(device.last_used_counter)

    def test_second_run_is_idempotent_and_preserves_order_numbers(self):
        first_numbers = _seed_then_order_numbers()
        counts = {
            "users": User.objects.filter(username="superadmin").count(),
            "customers": User.objects.filter(username="customer").count(),
            "products": products.objects.filter(slug__in=["marj", "oud-noir"]).count(),
            "coupons": Coupon.objects.filter(code__in=["FREESHIP", "SAVE10"]).count(),
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
                "customers": 1,
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
        self.assertEqual(User.objects.filter(username="customer").count(), 1)
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
        self.assertTrue(Order.objects.filter(order_number="ORD-2026-BBBBBBBB").exists())

        _run("--cleanup")
        Order.objects.filter(order_number=taken).delete()
        with mock.patch.object(seed_e2e, "_generate_order_number", return_value=taken):
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
        self.assertFalse(User.objects.filter(username="customer").exists())
        self.assertEqual(TOTPDevice.objects.count(), 0)
        self.assertFalse(
            products.objects.filter(slug__in=["marj", "oud-noir"]).exists()
        )
        self.assertFalse(
            Coupon.objects.filter(code__in=["FREESHIP", "SAVE10"]).exists()
        )
        self.assertFalse(
            ShippingMethod.objects.filter(code__in=["standard", "express"]).exists()
        )
        self.assertFalse(ShippingProvider.objects.filter(code="bluedart").exists())
        self.assertFalse(
            Order.objects.filter(idempotency_key__startswith="seed-e2e-order-").exists()
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

    def test_cleanup_keeps_the_customer_account_when_it_owns_an_order(self):
        _run()
        customer = User.objects.get(username="customer")
        # An order the owner placed during the walk is not this command's
        # row; deleting the account would cascade it away.
        order = Order.objects.create(
            user=customer,
            full_name="Customer Walk",
            phone="9999999999",
            address="4 Walk Street",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal("100.00"),
            idempotency_key="owner-walk-order",
        )

        _run("--cleanup")

        self.assertTrue(User.objects.filter(pk=customer.pk).exists())
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

    def test_cleanup_is_idempotent_and_cleanup_on_empty_db_is_a_noop(self):
        _run("--cleanup")
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(products.objects.count(), 0)
        self.assertEqual(TOTPDevice.objects.count(), 0)

        _run()
        _run("--cleanup")
        _run("--cleanup")
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Shipment.objects.count(), 0)
        self.assertEqual(ShippingRate.objects.count(), 0)
        self.assertEqual(TOTPDevice.objects.count(), 0)


def _run_lifecycle(*argv):
    out = StringIO()
    call_command("seed_e2e", "--scenario", "lifecycle", *argv, stdout=out)
    return out.getvalue()


class SeedE2ELifecycleTests(TestCase):
    """D-09: the three states a shipment-driven order walk has to prove."""

    def _orders(self):
        return {
            order.idempotency_key: order
            for order in Order.objects.filter(
                idempotency_key__startswith="seed-e2e-lifecycle-"
            )
        }

    def test_lifecycle_scenario_seeds_the_expected_three_states(self):
        out = _run_lifecycle()

        orders = self._orders()
        self.assertEqual(
            set(orders),
            {
                "seed-e2e-lifecycle-l1",
                "seed-e2e-lifecycle-l2",
                "seed-e2e-lifecycle-l3",
            },
        )
        self.assertEqual(Order.objects.count(), 3)
        self.assertEqual(Shipment.objects.count(), 4)
        # The default A/B/C set is not seeded by this scenario.
        self.assertFalse(
            Order.objects.filter(idempotency_key__startswith="seed-e2e-order-").exists()
        )

        l1 = orders["seed-e2e-lifecycle-l1"]
        self.assertEqual(l1.status, "confirmed")
        self.assertEqual(l1.payment_status, "captured")
        self.assertEqual(l1.fulfilment_status, "unfulfilled")
        self.assertEqual(l1.shipments.count(), 1)
        self.assertEqual(l1.shipments.get().status, Shipment.Status.LABEL_CREATED)

        l2 = orders["seed-e2e-lifecycle-l2"]
        self.assertEqual(l2.status, "confirmed")
        self.assertEqual(l2.payment_status, "captured")
        self.assertEqual(l2.fulfilment_status, "unfulfilled")
        self.assertEqual(
            set(l2.shipments.values_list("status", flat=True)),
            {Shipment.Status.DELIVERED, Shipment.Status.IN_TRANSIT},
        )

        l3 = orders["seed-e2e-lifecycle-l3"]
        self.assertEqual(l3.status, "pending")
        self.assertEqual(l3.payment_status, "pending")
        self.assertEqual(l3.fulfilment_status, "unfulfilled")
        self.assertEqual(l3.shipments.count(), 1)
        self.assertEqual(l3.shipments.get().status, Shipment.Status.LABEL_CREATED)

        for order in orders.values():
            self.assertRegex(order.order_number, ORDER_NUMBER_PATTERN)

        self.assertIn("expected outcomes (lifecycle scenario)", out)
        self.assertIn("L1: set its label_created parcel to dispatched", out)
        self.assertIn("L2: the delivered parcel alone must NOT", out)
        self.assertIn("After BOTH parcels are delivered", out)
        self.assertIn("L3: set its label_created parcel to dispatched", out)
        self.assertIn("/admin/shipping/shipment/", out)
        self.assertIn("/admin/orders/order/", out)
        self.assertIn("TEST DATA ONLY", out)

    def test_lifecycle_scenario_is_idempotent(self):
        _run_lifecycle()
        first_numbers = {
            order.idempotency_key: order.order_number
            for order in self._orders().values()
        }
        l1 = self._orders()["seed-e2e-lifecycle-l1"]
        parcel = l1.shipments.get()
        parcel.status = Shipment.Status.DISPATCHED
        parcel.save(update_fields=["status"])

        _run_lifecycle()

        self.assertEqual(Order.objects.count(), 3)
        self.assertEqual(Shipment.objects.count(), 4)
        self.assertEqual(
            {
                order.idempotency_key: order.order_number
                for order in self._orders().values()
            },
            first_numbers,
        )
        parcel.refresh_from_db()
        # A re-run resets the declared starting state rather than preserving a
        # human's in-progress walk.
        self.assertEqual(parcel.status, Shipment.Status.LABEL_CREATED)

    def test_lifecycle_cleanup_removes_only_the_lifecycle_rows(self):
        _run_lifecycle()
        _run("--cleanup")

        self.assertFalse(
            Order.objects.filter(
                idempotency_key__startswith="seed-e2e-lifecycle-"
            ).exists()
        )
        self.assertFalse(
            Shipment.objects.filter(
                tracking_number__in=[
                    "LCY000000001",
                    "LCY000000002",
                    "LCY000000003",
                    "LCY000000004",
                ]
            ).exists()
        )
        self.assertFalse(products.objects.filter(slug="marj").exists())
        self.assertFalse(ShippingProvider.objects.exists())

    def test_l1_dispatch_moves_the_order_to_shipped(self):
        _run_lifecycle()
        order = self._orders()["seed-e2e-lifecycle-l1"]
        parcel = order.shipments.get()
        parcel.status = Shipment.Status.DISPATCHED
        parcel.save(update_fields=["status"])

        result = sync_order_status(parcel)

        order.refresh_from_db()
        self.assertEqual(result.moved_to, "shipped")
        self.assertIsNone(result.refusal)
        self.assertEqual(order.status, "shipped")
        self.assertEqual(order.fulfilment_status, "fulfilled")

    def test_l2_delivered_gate_waits_for_both_parcels(self):
        _run_lifecycle()
        order = self._orders()["seed-e2e-lifecycle-l2"]
        delivered = order.shipments.get(status=Shipment.Status.DELIVERED)
        in_transit = order.shipments.get(status=Shipment.Status.IN_TRANSIT)

        # Dispatching the moving parcel advances the order; the delivered one
        # then asks for "delivered" but the sibling has not arrived.
        self.assertEqual(sync_order_status(in_transit).moved_to, "shipped")
        result = sync_order_status(delivered)
        order.refresh_from_db()
        self.assertEqual(result.refusal, "not every parcel has been delivered")
        self.assertEqual(order.status, "shipped")

        in_transit.status = Shipment.Status.DELIVERED
        in_transit.save(update_fields=["status"])
        result = sync_order_status(in_transit)
        order.refresh_from_db()
        self.assertEqual(result.moved_to, "delivered")
        self.assertEqual(order.status, "delivered")

    def test_l3_dispatch_saves_the_parcel_but_not_the_unpaid_order(self):
        _run_lifecycle()
        order = self._orders()["seed-e2e-lifecycle-l3"]
        parcel = order.shipments.get()
        parcel.status = Shipment.Status.DISPATCHED
        parcel.save(update_fields=["status"])

        result = sync_order_status(parcel)

        parcel.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.DISPATCHED)
        self.assertEqual(result.refusal, "pending -> shipped is not a legal transition")
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.payment_status, "pending")

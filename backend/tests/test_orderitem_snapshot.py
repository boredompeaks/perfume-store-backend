import importlib
from decimal import Decimal

from django.apps import apps as global_apps
from django.test import tag

from common.testing import ApiTestCase
from orders.admin import OrderItemInline
from orders.models import Order, OrderItem
from orders.serializers import OrderItemSerializer


def make_order(user):
    return Order.objects.create(
        user=user,
        full_name="Snapshot Test",
        phone="9876543210",
        address="1 Snapshot Way",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        total_amount=Decimal("100.00"),
    )


@tag("e2e")
class OrderItemSnapshotTests(ApiTestCase):
    def setUp(self):
        self.product = self.make_product(name="Oud Royale", price="250.00", stock=5)
        self.make_user("buyer")
        self.api_login("buyer")

    def _checkout(self):
        self.seed_session_cart([(self.product, 1)])
        res = self.checkout()
        self.assertEqual(res.status_code, 201, res.data)
        return OrderItem.objects.get(order_id=res.data["id"])

    def _staff_client(self):
        self.make_staff()
        staff = self.fresh_client()
        self.api_login("staff", client=staff)
        return staff

    def test_checkout_snapshots_product_identity(self):
        item = self._checkout()

        # No variant can be chosen at checkout and products carry no SKU, so sku snapshots empty and variant_name mirrors the product name.
        self.assertEqual(item.sku, "")
        self.assertEqual(item.variant_name, "Oud Royale")

        res = self.client.get("/api/orders/")
        self.assertEqual(res.status_code, 200, res.data)
        row = res.data["results"][0]["items"][0]
        self.assertEqual(row["sku"], "")
        self.assertEqual(row["variant_name"], "Oud Royale")

    def test_snapshot_survives_product_rename_and_reprice(self):
        item = self._checkout()

        staff = self._staff_client()
        res = staff.patch(
            f"/api/products/{self.product.slug}/",
            {"name": "Oud Royale (Discontinued)", "price": "400.00"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        item.refresh_from_db()
        self.assertEqual(item.variant_name, "Oud Royale")
        self.assertEqual(item.sku, "")
        self.assertEqual(item.price, Decimal("250.00"))

        res = self.client.get("/api/orders/")
        row = res.data["results"][0]["items"][0]
        self.assertEqual(row["variant_name"], "Oud Royale")
        self.assertEqual(row["price"], "250.00")

    def test_snapshot_survives_product_delete(self):
        item = self._checkout()

        staff = self._staff_client()
        res = staff.delete(f"/api/products/{self.product.slug}/")
        self.assertEqual(res.status_code, 204, res.data)

        item.refresh_from_db()
        self.assertIsNone(item.product)
        self.assertEqual(item.sku, "")
        self.assertEqual(item.variant_name, "Oud Royale")

        res = self.client.get("/api/orders/")
        row = res.data["results"][0]["items"][0]
        self.assertEqual(row["product"], None)
        self.assertEqual(row["sku"], "")
        self.assertEqual(row["variant_name"], "Oud Royale")

    def test_serializer_exposes_snapshots_read_only(self):
        fields = OrderItemSerializer().fields
        self.assertIn("sku", fields)
        self.assertIn("variant_name", fields)
        self.assertTrue(fields["sku"].read_only)
        self.assertTrue(fields["variant_name"].read_only)
        self.assertIn("sku", OrderItemSerializer.Meta.read_only_fields)
        self.assertIn("variant_name", OrderItemSerializer.Meta.read_only_fields)

    def test_admin_inline_surfaces_snapshots_read_only(self):
        self.assertEqual(
            OrderItemInline.fields,
            (
                "product",
                "product_name",
                "sku",
                "variant_name",
                "price",
                "quantity",
                "subtotal",
            ),
        )
        from django.contrib.admin.sites import site

        inline = OrderItemInline(Order, site)
        # Admin edits would falsify purchase history: the inline refuses
        # add/change/delete outright, so the snapshot columns are read-only.
        self.assertFalse(inline.has_add_permission(None))
        self.assertFalse(inline.has_change_permission(None))
        self.assertFalse(inline.has_delete_permission(None))


class SnapshotBackfillTests(ApiTestCase):
    def test_backfill_populates_from_current_product_state(self):
        module = importlib.import_module(
            "orders.migrations.0007_orderitem_sku_orderitem_variant_name"
        )
        buyer = self.make_user("legacy")
        order = make_order(buyer)

        product = self.make_product(name="Backfill Source")
        keep = OrderItem.objects.create(
            order=order,
            product=product,
            product_name="Name At Purchase",
            price=Decimal("50.00"),
            quantity=1,
            subtotal=Decimal("50.00"),
        )

        gone = self.make_product(name="Deleted Before Backfill")
        orphan = OrderItem.objects.create(
            order=order,
            product=gone,
            product_name="Orphan Line",
            price=Decimal("50.00"),
            quantity=1,
            subtotal=Decimal("50.00"),
        )
        gone.delete()

        module.backfill_order_item_snapshots(global_apps, None)

        keep.refresh_from_db()
        orphan.refresh_from_db()
        self.assertEqual(keep.sku, "")
        self.assertEqual(keep.variant_name, "Backfill Source")
        self.assertEqual(orphan.sku, "")
        self.assertEqual(orphan.variant_name, "")
        self.assertEqual(orphan.product_name, "Orphan Line")

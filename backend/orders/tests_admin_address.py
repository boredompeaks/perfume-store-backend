from decimal import Decimal

from django.contrib.admin.sites import site
from django.contrib.auth.models import Group, User
from django.test import tag

from common.roles import ROLE_CATALOGUE, ROLE_FINANCE, ROLE_INVENTORY
from common.testing import ApiTestCase
from orders.admin import OrderAdmin
from orders.models import Order

LONG_ADDRESS = "12 Lavender Crescent, Bandra West, Mumbai 400050, India"
LONG_ADDRESS_PREVIEW = "12 Lavender Crescent, Bandra West, Mumb…"
SHORT_ADDRESS = "12 Rose Lane"


def make_role_user(username, role):
    user = User.objects.create_user(
        username, f"{username}@example.com", "S3cure-Passphrase!"
    )
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


@tag("orders")
class OrderAdminAddressTests(ApiTestCase):
    def setUp(self):
        self.buyer = self.make_user("buyer")
        self.finance = make_role_user("addr-finance", ROLE_FINANCE)
        self.packer = make_role_user("addr-packer", ROLE_INVENTORY)
        self.catalogue = make_role_user("addr-catalogue", ROLE_CATALOGUE)
        self.long_order = self.make_order("Long Buyer", LONG_ADDRESS)
        self.short_order = self.make_order("Short Buyer", SHORT_ADDRESS)

    def make_order(self, full_name, address, status="pending"):
        return Order.objects.create(
            user=self.buyer,
            full_name=full_name,
            phone="9876543210",
            address=address,
            city="Mumbai",
            state="Maharashtra",
            pincode="400050",
            status=status,
            total_amount=Decimal("10.00"),
        )

    def url(self):
        return "/admin/orders/order/"

    def test_orders_read_role_sees_the_truncated_address_column(self):
        self.client.force_login(self.finance)

        res = self.client.get(self.url())

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Address")
        self.assertContains(res, LONG_ADDRESS_PREVIEW)

    def test_orders_read_role_can_search_by_address(self):
        self.client.force_login(self.finance)

        res = self.client.get(self.url(), {"q": "Lavender"})

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Long Buyer")
        self.assertNotContains(res, "Short Buyer")

    def test_packing_role_sees_the_column_but_searches_by_reference_only(self):
        self.client.force_login(self.packer)

        listing = self.client.get(self.url())
        by_address = self.client.get(self.url(), {"q": "Lavender"})
        by_reference = self.client.get(self.url(), {"q": str(self.long_order.pk)})

        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "Address")
        self.assertContains(listing, LONG_ADDRESS_PREVIEW)
        # Assert on the preview, not the name: the packing grid withholds customer names,
        # so a name-based assertion here would pass vacuously.
        self.assertEqual(by_address.status_code, 200)
        self.assertNotContains(by_address, LONG_ADDRESS_PREVIEW)
        self.assertEqual(by_reference.status_code, 200)
        self.assertContains(by_reference, LONG_ADDRESS_PREVIEW)

    def test_scoped_search_fields_stay_reference_only(self):
        # Pin the deliberate omission so a future column cannot widen the packing role's search terms.
        self.assertEqual(OrderAdmin.scoped_search_fields, ("id", "order_number"))
        self.assertNotIn("address", OrderAdmin.scoped_search_fields)

    def test_short_address_truncates_and_collapses_whitespace(self):
        model_admin = OrderAdmin(Order, site)

        long = Order(address=LONG_ADDRESS)
        exact = Order(address="A" * 40)
        over = Order(address="A" * 41)
        multi_line = Order(address="  Flat\n4,   Rose Lane  ")
        empty = Order(address="")

        self.assertEqual(model_admin.short_address(long), LONG_ADDRESS_PREVIEW)
        self.assertEqual(model_admin.short_address(exact), "A" * 40)
        self.assertEqual(model_admin.short_address(over), "A" * 39 + "…")
        self.assertEqual(model_admin.short_address(multi_line), "Flat 4, Rose Lane")
        self.assertEqual(model_admin.short_address(empty), "")

    def test_change_form_keeps_the_full_address(self):
        self.client.force_login(self.finance)

        res = self.client.get(f"{self.url()}{self.long_order.pk}/change/")

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, LONG_ADDRESS)

    def test_role_without_orders_read_cannot_reach_the_changelist(self):
        self.client.force_login(self.catalogue)

        self.assertEqual(self.client.get(self.url()).status_code, 403)

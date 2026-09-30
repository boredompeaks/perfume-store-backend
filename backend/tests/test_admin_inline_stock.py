"""SPEC-20-11 [R-20.35]: SAFE inline admin stock editing on the changelist.

Inline editing is the UX affordance the spec asks for (``stock`` editable
in the grid like ``price``); the ledger write is the contract. SPEC-6-02
[6.5.17] — "no silent inventory edits, every mutation path lands a
StockMovement row" — is what these tests pin, so an inline edit can never
become a second, ledger-free writer:

- an inline stock change writes exactly one StockMovement row with the
  right delta / stock_after / reason / created_by and persists the count,
- the write is race-safe (delta through ``adjust_stock``, which re-reads the
  row under ``select_for_update``) rather than a blind absolute write,
- a role without ``inventory.adjust`` gets no cell and cannot move stock
  with a hand-crafted POST, and a role without ``products.write`` is
  rejected outright — no ledger row on either path,
- the deliberate adjust-stock action still writes its own ledger row.

Both gates are needed: ``inventory.adjust`` (the same capability the
adjust-stock action rides, read off ``action_capabilities`` so the two
cannot drift) AND Django's own ``products.write`` change permission.
"""
import re
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import Group, User
from django.test import RequestFactory, tag

from common.roles import ROLE_ADMIN, ROLE_CATALOGUE, ROLE_INVENTORY
from common.testing import ApiTestCase
from products.admin import ChangelistStockForm, ProductAdmin
from products.models import StockMovement, products

TEST_PASSWORD = "S3cure-Passphrase!"
CHANGELIST = "/admin/products/products/"


def make_role_user(role, username):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


@tag("e2e")
class InlineStockSurfaceTests(ApiTestCase):
    """The grid offers the cell, and the form behind it is the pinned one."""

    def setUp(self):
        self.chief = make_role_user(ROLE_ADMIN, "inline-chief")
        self.product = self.make_product(name="Inline Rose", stock=3)

    def test_inline_cell_renders_for_a_role_holding_the_capability(self):
        self.client.force_login(self.chief)
        res = self.client.get(CHANGELIST)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="form-0-stock"')
        self.assertContains(res, 'name="form-0-price"')

    def test_inline_form_covers_exactly_the_list_editable_columns(self):
        """The changelist formset builds its form from ``list_editable``;
        a drift between the two would silently drop a column (or ask for a
        field the grid never rendered)."""
        self.assertEqual(
            set(ChangelistStockForm.Meta.fields), set(ProductAdmin.list_editable)
        )

    def test_inline_capability_is_the_adjust_stock_capability(self):
        product_admin = admin.site._registry[products]
        self.assertEqual(
            product_admin.inline_stock_capability,
            product_admin.action_capabilities["adjust_stock"],
        )


@tag("e2e")
class InlineStockEditLedgerTests(ApiTestCase):
    """An inline stock change is ledger-routed, exactly once, by the actor."""

    def setUp(self):
        self.chief = make_role_user(ROLE_ADMIN, "inline-editor")
        self.product = self.make_product(name="Inline Oud", stock=3)
        self.client.force_login(self.chief)

    def _grid(self):
        """The changelist's management form + inputs, as a browser sends them."""
        res = self.client.get(CHANGELIST)
        self.assertEqual(res.status_code, 200)
        return dict(
            re.findall(r'name="(form-[A-Z_]+)" value="([^"]*)"', res.content.decode())
        )

    def _post_row(self, **overrides):
        payload = {
            **self._grid(),
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-0-id": str(self.product.id),
            "form-0-price": "60.00",
            "form-0-stock": "3",
            "_save": "Save",
        }
        payload.update(overrides)
        return self.client.post(CHANGELIST, payload, follow=True)

    def test_inline_edit_writes_one_ledger_row_and_persists_the_count(self):
        res = self._post_row(**{"form-0-stock": "42"})
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 42)

        movement = StockMovement.objects.get(product=self.product)
        self.assertEqual(StockMovement.objects.count(), 1)  # exactly one
        self.assertEqual(movement.delta, 39)                # 42 - 3, not 42
        self.assertEqual(movement.stock_after, 42)
        self.assertEqual(movement.reason, StockMovement.Reason.CORRECTION)
        self.assertEqual(movement.note, "changelist inline stock edit")
        self.assertEqual(movement.created_by, self.chief)
        self.assertEqual(self.product.stock, movement.stock_after)

    def test_inline_edit_downward_lands_a_negative_delta(self):
        res = self._post_row(**{"form-0-stock": "1"})
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 1)
        movement = StockMovement.objects.get(product=self.product)
        self.assertEqual(movement.delta, -2)
        self.assertEqual(movement.stock_after, 1)

    def test_inline_edit_of_price_and_stock_keeps_both(self):
        res = self._post_row(**{"form-0-price": "71.50", "form-0-stock": "8"})
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, Decimal("71.50"))
        self.assertEqual(self.product.stock, 8)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_below_zero_count_is_rejected_and_writes_no_movement(self):
        res = self._post_row(**{"form-0-stock": "-1"})
        self.assertEqual(res.status_code, 200)
        # the grid re-renders with the cell's error; nothing is saved
        self.assertTrue(res.context["cl"].formset.forms[0].errors["stock"])
        self.assertContains(res, "greater than or equal to 0")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_non_numeric_count_is_rejected_and_writes_no_movement(self):
        res = self._post_row(**{"form-0-stock": "many"})
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_stock_column_and_ledger_cannot_diverge(self):
        """The column and the ledger are the same write: a raw formset save
        followed by a zero-delta adjustment would leave them disagreeing, so
        the persisted count is pinned to the movement's ``stock_after``."""
        res = self._post_row(**{"form-0-stock": "9"})
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        movement = StockMovement.objects.get()
        self.assertEqual(self.product.stock, movement.stock_after)

    def test_two_rows_in_one_save_each_land_their_own_ledger_row(self):
        """The formset loop must route every changed row, not just the first
        (the grid posts the whole page, and the ledger is per product)."""
        second = self.make_product(name="Inline Musk", stock=7)
        fields = dict(
            re.findall(
                r'name="(form-[\w-]+)" value="([^"]*)"',
                self.client.get(CHANGELIST).content.decode(),
            )
        )
        row = {
            pk: name.split("-")[1]
            for name, pk in fields.items()
            if name.endswith("-id")
        }  # product pk -> the form-N prefix the grid rendered it under
        payload = {
            **fields,
            "form-TOTAL_FORMS": "2",
            "form-INITIAL_FORMS": "2",
            f"form-{row[str(self.product.id)]}-stock": "12",
            f"form-{row[str(second.id)]}-stock": "5",
            "_save": "Save",
        }
        res = self.client.post(CHANGELIST, payload, follow=True)
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.product.stock, 12)
        self.assertEqual(second.stock, 5)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(
            {
                movement.product_id: movement.delta
                for movement in StockMovement.objects.all()
            },
            {self.product.id: 9, second.id: -2},
        )

    def test_untouched_cell_writes_no_movement(self):
        res = self._post_row()
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)


@tag("e2e")
class InlineStockCapabilityGateTests(ApiTestCase):
    """The inline cell is the adjust-stock capability's, not merely a
    products permission's."""

    def setUp(self):
        self.product = self.make_product(name="Gated Rose", stock=3)
        # catalogue: products.write (so the changelist is editable at all)
        # but NOT inventory.adjust.
        self.scribe = make_role_user(ROLE_CATALOGUE, "inline-scribe")
        # inventory: inventory.adjust (the adjust-stock action's capability)
        # but NOT products.write, so no changelist edit of any column.
        self.keeper = make_role_user(ROLE_INVENTORY, "inline-keeper")

    def _grid(self, client):
        res = client.get(CHANGELIST)
        self.assertEqual(res.status_code, 200)
        return dict(
            re.findall(r'name="(form-[A-Z_]+)" value="([^"]*)"', res.content.decode())
        )

    def test_role_without_the_capability_gets_no_inline_stock_cell(self):
        client = self.fresh_client()
        client.force_login(self.scribe)
        res = client.get(CHANGELIST)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="form-0-price"')      # still editable
        self.assertNotContains(res, 'name="form-0-stock"')  # cell withdrawn
        self.assertContains(res, 'class="field-stock"')      # column still shown

    def test_forged_stock_in_a_post_is_inert_and_writes_no_movement(self):
        client = self.fresh_client()
        client.force_login(self.scribe)
        payload = {
            **self._grid(client),
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-0-id": str(self.product.id),
            "form-0-price": "64.00",
            "form-0-stock": "999",  # not a field on this user's form
            "_save": "Save",
        }
        res = client.post(CHANGELIST, payload, follow=True)
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)                 # untouched
        self.assertEqual(self.product.price, Decimal("64.00"))  # its own edit lands
        self.assertEqual(StockMovement.objects.count(), 0)    # no mutation, no movement

    def test_role_without_change_permission_is_rejected_with_403(self):
        """inventory.adjust alone is not changelist write access: Django's own
        change-permission check rejects the save before the formset runs."""
        client = self.fresh_client()
        client.force_login(self.keeper)
        res = client.post(
            CHANGELIST,
            {
                **self._grid(client),
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-0-id": str(self.product.id),
                "form-0-stock": "999",
                "_save": "Save",
            },
        )
        self.assertEqual(res.status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_capability_gate_does_not_narrow_the_registered_admin(self):
        """The narrowed grid lives on a throwaway copy, so one request's role
        cannot leak into the next one's (the registry entry is a singleton)."""
        product_admin = admin.site._registry[products]
        request = RequestFactory().get(CHANGELIST)
        request.user = self.scribe
        before = product_admin.list_editable
        product_admin.changelist_view(request)  # denied path
        self.assertEqual(product_admin.list_editable, before)
        self.assertIn("stock", product_admin.list_editable)


@tag("e2e")
class AdjustStockActionNoRegressionTests(ApiTestCase):
    """The deliberate adjust-stock action keeps its own ledger write."""

    def setUp(self):
        self.chief = make_role_user(ROLE_ADMIN, "action-chief")
        self.product = self.make_product(name="Action Rose", stock=10)
        self.client.force_login(self.chief)

    def test_adjust_stock_action_still_writes_its_ledger_row(self):
        res = self.client.post(
            CHANGELIST,
            {
                "action": "adjust_stock",
                "_selected_action": [str(self.product.id)],
                "select_across": "0",
                "apply": "1",
                "delta": "20",
                "reason": "restock",
                "note": "supplier delivery",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 30)
        movement = StockMovement.objects.get(product=self.product)
        self.assertEqual(movement.delta, 20)
        self.assertEqual(movement.stock_after, 30)
        self.assertEqual(movement.reason, "restock")
        self.assertEqual(movement.created_by, self.chief)

    def test_adjust_stock_action_still_refuses_a_below_zero_result(self):
        res = self.client.post(
            CHANGELIST,
            {
                "action": "adjust_stock",
                "_selected_action": [str(self.product.id)],
                "select_across": "0",
                "apply": "1",
                "delta": "-50",
                "reason": "damage",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10)
        self.assertEqual(StockMovement.objects.count(), 0)

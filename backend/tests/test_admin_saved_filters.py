"""SPEC-20-6 [R-20.11]: saved filters/views on the two busiest changelists.

Spec 20.1 lists "Saved filters/views where useful" among the data-table
capabilities; Django ships no equivalent, so orders and products get a
per-user saved filter. These tests pin the four properties that make it
safe rather than a shortcut around the admin's own gates:

- a saved spec applies on the changelist and narrows the listing,
- it belongs to ONE user: a foreign pk neither applies nor confirms its
  existence, and cannot be deleted,
- it cannot surface a row the user could not otherwise list — the gate is
  the ModelAdmin's own ``has_view_permission``, so a role without
  ``orders.read`` is refused by the changelist itself,
- only parameters the changelist itself accepts are ever stored, so a
  replayed spec can never turn into a 400.
"""

from decimal import Decimal
from html import unescape
import re
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
from django.test import tag

from common.models import AuditEvent, SavedFilter
from common.roles import ROLE_ADMIN, ROLE_MARKETING, ROLE_SUPPORT
from common.saved_filters import APPLY_PARAM, NON_SPEC_PARAMS, SPEC_FIELD
from common.testing import ApiTestCase
from orders.models import Order

TEST_PASSWORD = "S3cure-Passphrase!"
ORDER_CHANGELIST = "/admin/orders/order/"
PRODUCT_CHANGELIST = "/admin/products/products/"
ORDER_SAVE = "/admin/saved-filters/orders/order/save/"
PRODUCT_SAVE = "/admin/saved-filters/products/products/save/"


def make_role_user(role, username):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def make_order(user, status="pending", full_name="Someone"):
    return Order.objects.create(
        user=user,
        full_name=full_name,
        phone="9876543210",
        address="1 Admin Way",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        status=status,
        total_amount=Decimal("100.00"),
    )


def save_view(client, url, name, spec):
    return client.post(url, {SPEC_FIELD: spec, "name": name}, follow=True)


def content_type_of(model):
    return ContentType.objects.get_for_model(model)


@tag("e2e")
class SavedFilterBarTests(ApiTestCase):
    """The bar is on both high-traffic changelists, and only for viewers."""

    def setUp(self):
        self.support = make_role_user(ROLE_SUPPORT, "bar-support")
        self.client.force_login(self.support)

    def test_bar_renders_on_the_orders_and_products_changelists(self):
        orders = self.client.get(ORDER_CHANGELIST)
        self.assertContains(orders, "/admin/saved-filters/orders/order/save/")
        products = self.client.get(PRODUCT_CHANGELIST)
        self.assertContains(products, "/admin/saved-filters/products/products/save/")

    def test_the_delivered_grid_is_untouched_by_the_bar(self):
        """The bar is additive chrome below the grid, never a replacement:
        the inline-editable stock cell is still rendered for a role holding
        ``inventory.adjust`` (SPEC-20-11)."""
        product = self.make_product(name="Bar Rose", stock=4)
        self.client.force_login(make_role_user(ROLE_ADMIN, "bar-chief"))
        res = self.client.get(PRODUCT_CHANGELIST)
        self.assertContains(res, 'name="form-0-stock"')
        self.assertContains(res, 'id="saved-filters"')
        self.assertEqual(product.stock, 4)

    def test_a_role_without_the_capability_never_reaches_the_bar(self):
        """No capability -> the changelist's own 403, and the saved-filter
        table is not touched on the way there."""
        self.client.force_login(make_role_user(ROLE_MARKETING, "bar-marketing"))
        res = self.client.get(ORDER_CHANGELIST)
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("/admin/saved-filters/", res.content.decode())
        self.assertEqual(SavedFilter.objects.count(), 0)


@tag("e2e")
class SavedFilterSaveTests(ApiTestCase):
    """What gets stored: the current selection, validated by the changelist."""

    def setUp(self):
        self.support = make_role_user(ROLE_SUPPORT, "save-support")
        self.client.force_login(self.support)

    def test_a_saved_filter_stores_the_current_selection(self):
        res = save_view(self.client, ORDER_SAVE, "Open orders", "status__exact=pending")
        self.assertEqual(res.status_code, 200)
        saved = SavedFilter.objects.get()
        self.assertEqual(saved.user, self.support)
        self.assertEqual(saved.content_type, content_type_of(Order))
        self.assertEqual(saved.params, {"status__exact": "pending"})
        self.assertEqual(saved.__str__(), "Open orders")
        self.assertRedirects(res, ORDER_CHANGELIST, fetch_redirect_response=False)

    def test_a_search_term_is_a_saveable_selection(self):
        save_view(self.client, PRODUCT_SAVE, "Rose search", "q=Rose")
        self.assertEqual(SavedFilter.objects.get().params, {"q": "Rose"})

    def test_ordering_is_deliberately_not_part_of_a_saved_spec(self):
        """``o`` is a list of ``list_display`` column INDEXES, so a stored
        copy re-sorts a different column the moment one is inserted."""
        self.assertIn("o", NON_SPEC_PARAMS)
        save_view(self.client, ORDER_SAVE, "Sorted", "o=-3.1&status__exact=pending")
        self.assertEqual(SavedFilter.objects.get().params, {"status__exact": "pending"})

    def test_a_parameter_the_changelist_rejects_is_dropped_not_stored(self):
        """A spec is replayed from the database, so it may only hold
        parameters the changelist itself accepts — an unknown lookup would
        be a 400 on every later visit."""
        save_view(
            self.client,
            ORDER_SAVE,
            "Crafted",
            "not_a_field__exact=1&status__exact=pending",
        )
        self.assertEqual(SavedFilter.objects.get().params, {"status__exact": "pending"})

    def test_a_multi_valued_parameter_is_not_a_saved_view(self):
        save_view(
            self.client,
            ORDER_SAVE,
            "Two values",
            "status__exact=pending&status__exact=shipped&q=rose",
        )
        self.assertEqual(SavedFilter.objects.get().params, {"q": "rose"})

    def test_saving_nothing_is_refused_and_stores_no_row(self):
        res = save_view(self.client, ORDER_SAVE, "Nothing", "")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(SavedFilter.objects.count(), 0)
        self.assertContains(res, "Nothing to save")

    def test_a_blank_or_over_long_name_is_refused(self):
        for bad_name in ("", "x" * 200):
            with self.subTest(name=bad_name):
                save_view(self.client, ORDER_SAVE, bad_name, "status__exact=pending")
                self.assertEqual(SavedFilter.objects.count(), 0)

    def test_re_saving_a_name_replaces_its_selection(self):
        save_view(self.client, ORDER_SAVE, "Open", "status__exact=pending")
        save_view(self.client, ORDER_SAVE, "Open", "status__exact=confirmed")
        self.assertEqual(SavedFilter.objects.count(), 1)
        self.assertEqual(
            SavedFilter.objects.get().params, {"status__exact": "confirmed"}
        )

    def test_a_concurrent_save_of_the_same_name_is_reported_not_crashed(self):
        """The unique constraint is the authority on the name, not a prior
        SELECT: the losing insert is told so instead of 500ing."""
        saved = SavedFilter.objects.create(
            user=self.support,
            content_type=content_type_of(Order),
            name="Race",
            params={},
        )
        with patch.object(
            SavedFilter.objects,
            "update_or_create",
            side_effect=IntegrityError("duplicate key"),
        ):
            res = save_view(self.client, ORDER_SAVE, "Race", "status__exact=pending")
        self.assertContains(res, "already exists")
        saved.refresh_from_db()
        self.assertEqual(saved.params, {})

    def test_the_endpoints_are_post_only(self):
        self.assertEqual(self.client.get(ORDER_SAVE).status_code, 405)
        saved = SavedFilter.objects.create(
            user=self.support,
            content_type=content_type_of(Order),
            name="Mine",
            params={"status__exact": "pending"},
        )
        self.assertEqual(
            self.client.get(f"/admin/saved-filters/delete/{saved.pk}/").status_code,
            405,
        )

    def test_anonymous_callers_are_sent_to_the_admin_login(self):
        self.client.logout()
        res = self.client.post(ORDER_SAVE, {"name": "x", SPEC_FIELD: "q=a"})
        self.assertEqual(res.status_code, 302)
        self.assertIn("/admin/login/", res["Location"])
        self.assertEqual(SavedFilter.objects.count(), 0)

    def test_a_role_without_the_capability_cannot_save_a_filter(self):
        """The gate is the ModelAdmin's own view permission: marketing holds
        no order capability, so there is no orders saved view to own."""
        self.client.force_login(make_role_user(ROLE_MARKETING, "save-marketing"))
        res = save_view(self.client, ORDER_SAVE, "Sneaky", "status__exact=pending")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(SavedFilter.objects.count(), 0)

    def test_an_unregistered_or_unknown_model_is_a_404(self):
        """AuditEvent is deliberately unregistered in the admin, and a model
        name that does not exist at all is refused the same way."""
        for url in (
            "/admin/saved-filters/common/auditevent/save/",
            "/admin/saved-filters/common/no_such_model/save/",
        ):
            with self.subTest(url=url):
                res = save_view(self.client, url, "Nope", "q=rose")
                self.assertEqual(res.status_code, 404)
                self.assertEqual(SavedFilter.objects.count(), 0)


@tag("e2e")
class SavedFilterApplyTests(ApiTestCase):
    """Applying a saved filter — and only ever your own."""

    def setUp(self):
        self.support = make_role_user(ROLE_SUPPORT, "apply-support")
        self.client.force_login(self.support)
        buyer = self.make_user("apply-buyer")
        make_order(buyer, status="pending", full_name="Open One")
        make_order(buyer, status="shipped", full_name="Closed One")
        save_view(self.client, ORDER_SAVE, "Open orders", "status__exact=pending")
        self.saved = SavedFilter.objects.get()

    def _listing(self, url=ORDER_CHANGELIST):
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        return res.content.decode()

    def test_applying_a_saved_filter_narrows_the_changelist(self):
        body = self._listing(f"{ORDER_CHANGELIST}?{APPLY_PARAM}={self.saved.pk}")
        self.assertIn("Open One", body)
        self.assertNotIn("Closed One", body)
        self.assertIn("Listing the saved view", body)

    def test_the_unfiltered_listing_is_untouched(self):
        body = self._listing()
        self.assertIn("Open One", body)
        self.assertIn("Closed One", body)
        self.assertNotIn("Listing the saved view", body)

    def test_another_users_saved_filter_is_not_applied(self):
        """A foreign pk is the same answer as one that does not exist: the
        apply link can neither apply nor confirm another user's view."""
        self.client.force_login(make_role_user(ROLE_ADMIN, "apply-other"))
        body = self._listing(f"{ORDER_CHANGELIST}?{APPLY_PARAM}={self.saved.pk}")
        self.assertIn("Open One", body)
        self.assertIn("Closed One", body)
        self.assertNotIn("Listing the saved view", body)

    def test_a_foreign_or_malformed_pk_is_ignored_not_fatal(self):
        for raw in ("0", "99999", "not-a-pk", ""):
            with self.subTest(pk=raw):
                body = self._listing(f"{ORDER_CHANGELIST}?{APPLY_PARAM}={raw}")
                self.assertIn("Open One", body)
                self.assertIn("Closed One", body)

    def test_a_saved_filter_cannot_reach_rows_the_user_cannot_list(self):
        """The capability gate is the changelist's own, so applying someone
        else's filter is still a 403 for a role without the capability."""
        self.client.force_login(make_role_user(ROLE_MARKETING, "apply-marketing"))
        res = self.client.get(f"{ORDER_CHANGELIST}?{APPLY_PARAM}={self.saved.pk}")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("Open One", res.content.decode())

    def test_a_saved_filter_narrows_the_products_changelist_too(self):
        self.make_product(name="Rose One", category="Floral")
        self.make_product(name="Oud One", category="Woody")
        save_view(self.client, PRODUCT_SAVE, "Floral only", "category__exact=Floral")
        saved = SavedFilter.objects.get(name="Floral only")
        body = self._listing(f"{PRODUCT_CHANGELIST}?{APPLY_PARAM}={saved.pk}")
        self.assertIn("Rose One", body)
        self.assertNotIn("Oud One", body)

    def test_a_saved_filter_does_not_leak_another_models_selection(self):
        """A filter is scoped to its own model: the products apply marker on
        the orders changelist is ignored, never cross-applied."""
        save_view(self.client, PRODUCT_SAVE, "Floral only", "category__exact=Floral")
        products_saved = SavedFilter.objects.get(name="Floral only")
        body = self._listing(f"{ORDER_CHANGELIST}?{APPLY_PARAM}={products_saved.pk}")
        self.assertIn("Open One", body)
        self.assertIn("Closed One", body)


@tag("e2e")
class SavedFilterDeleteTests(ApiTestCase):
    """Deletion is the owner's alone, and needs no capability to be tidy."""

    def setUp(self):
        self.support = make_role_user(ROLE_SUPPORT, "delete-support")
        self.client.force_login(self.support)
        buyer = self.make_user("delete-buyer")
        make_order(buyer)
        save_view(self.client, ORDER_SAVE, "Open orders", "status__exact=pending")
        self.saved = SavedFilter.objects.get()

    def test_owning_user_deletes_their_view_and_returns_to_the_changelist(self):
        res = self.client.post(
            f"/admin/saved-filters/delete/{self.saved.pk}/", follow=True
        )
        self.assertEqual(SavedFilter.objects.count(), 0)
        self.assertRedirects(res, ORDER_CHANGELIST, fetch_redirect_response=False)

    def test_another_users_view_is_a_404_and_survives(self):
        self.client.force_login(make_role_user(ROLE_ADMIN, "delete-other"))
        res = self.client.post(f"/admin/saved-filters/delete/{self.saved.pk}/")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(SavedFilter.objects.count(), 1)

    def test_anonymous_callers_are_sent_to_the_admin_login(self):
        self.client.logout()
        res = self.client.post(f"/admin/saved-filters/delete/{self.saved.pk}/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/admin/login/", res["Location"])
        self.assertEqual(SavedFilter.objects.count(), 1)

    def test_a_user_who_lost_the_capability_can_still_clear_their_view(self):
        """The row is the caller's own preference, not domain data; the
        redirect to the changelist is then refused by its own gate."""
        self.support.groups.clear()
        res = self.client.post(
            f"/admin/saved-filters/delete/{self.saved.pk}/", follow=True
        )
        self.assertEqual(SavedFilter.objects.count(), 0)
        self.assertEqual(res.status_code, 403)

    def test_a_view_of_a_model_without_a_changelist_falls_back_to_the_index(self):
        """AuditEvent is deliberately unregistered in the admin; the row
        outlives its changelist and clearing it must not raise."""
        orphan = SavedFilter.objects.create(
            user=self.support,
            content_type=content_type_of(AuditEvent),
            name="Orphan",
            params={},
        )
        res = self.client.post(f"/admin/saved-filters/delete/{orphan.pk}/", follow=True)
        self.assertFalse(SavedFilter.objects.filter(pk=orphan.pk).exists())
        self.assertRedirects(res, "/admin/", fetch_redirect_response=False)


class SavedFilterSpecTests(ApiTestCase):
    """The stored-spec guards, on rows the UI would not produce."""

    def setUp(self):
        self.support = make_role_user(ROLE_SUPPORT, "spec-support")
        self.client.force_login(self.support)
        buyer = self.make_user("spec-buyer")
        make_order(buyer, status="pending", full_name="Kept One")
        make_order(buyer, status="shipped", full_name="Dropped One")

    def _hand_written(self, params):
        return SavedFilter.objects.create(
            user=self.support,
            content_type=content_type_of(Order),
            name="Hand written",
            params=params,
        )

    def _apply_url(self, saved):
        return f"{ORDER_CHANGELIST}?{APPLY_PARAM}={saved.pk}"

    def test_a_non_string_stored_value_is_skipped_not_crashed_on(self):
        """``params`` is a JSON column, so a hand-edited row can hold a type
        the QueryDict would refuse; the entry is dropped, the rest applies."""
        saved = self._hand_written(
            {"status__exact": "pending", "created_at__year": 2026}
        )
        res = self.client.get(self._apply_url(saved))
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("Kept One", body)
        self.assertNotIn("Dropped One", body)

    def test_an_explicit_url_parameter_wins_over_the_saved_spec(self):
        """A saved view is a starting point an operator can adjust from."""
        saved = self._hand_written({"status__exact": "pending"})
        res = self.client.get(f"{self._apply_url(saved)}&status__exact=shipped")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("Dropped One", body)
        self.assertNotIn("Kept One", body)

    def test_the_save_form_carries_the_selection_on_screen(self):
        """The bar's hidden field is filled from the live query string, minus
        the changelist plumbing, so re-saving captures what is displayed —
        and never a page number."""
        res = self.client.get(f"{ORDER_CHANGELIST}?status__exact=pending&p=2")
        self.assertEqual(res.status_code, 200)
        spec = re.search(r'name="filter_spec" value="([^"]*)"', res.content.decode())
        self.assertIsNotNone(spec)
        self.assertEqual(unescape(spec.group(1)), "status__exact=pending")

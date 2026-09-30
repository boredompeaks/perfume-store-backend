"""SPEC-5-10: the unified global admin search (spec 5.1).

Spec 5.1 draws one search box for the admin — "Search orders, products,
customers…". These tests pin the properties that make it safe and useful:

- one term reaches all three targeted models, and each result links to the
  object's own admin page;
- a model the caller may not view contributes NOTHING — no heading, no
  rows, and (the point of checking the capability before the query) no
  query. A role with products-only authority must not learn that a
  matching order or customer exists;
- a caller who may view none of them is refused with a 403, not handed an
  empty page; anonymous callers go to the admin login; the superuser
  bypass the other admin surfaces keep still works;
- SPEC-20-6b: the admin area requires staff membership as well as the
  capability, so a non-staff account carrying a role group is refused here
  exactly as it is by the changelists the result links point at;
- no matches and no term are both clean 200 pages, not errors;
- the search box is in the admin chrome on every staff page and absent from
  the login page, and the per-model search box is still there.
"""

from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import tag
from django.test.utils import CaptureQueriesContext

from common.admin_search import (
    MAX_TERM_LENGTH,
    RESULTS_PER_MODEL,
    SEARCH_CAPABILITIES,
    SEARCH_TARGETS,
    registered_model_admin,
)
from common.permissions import user_has_capability
from common.roles import ROLE_ADMIN, ROLE_FINANCE, ROLE_MARKETING, ROLE_SUPPORT
from common.testing import ApiTestCase
from orders.models import Order

TEST_PASSWORD = "S3cure-Passphrase!"
SEARCH_URL = "/admin/search/"


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


def search(client, term, expect=200):
    res = client.get(SEARCH_URL, {"q": term})
    assert res.status_code == expect, (res.status_code, term)
    return res.content.decode() if expect == 200 else ""


@tag("e2e")
class GlobalSearchResultTests(ApiTestCase):
    """One term, three models, real result links."""

    def setUp(self):
        self.buyer = self.make_user("zephyrine-buyer")
        self.product = self.make_product(name="Zephyrine Royale")
        self.order = make_order(self.buyer, full_name="Zephyrine Buyer")
        self.client.force_login(make_role_user(ROLE_ADMIN, "search-chief"))

    def test_one_term_reaches_every_targeted_model(self):
        body = search(self.client, "Zephyrine")
        self.assertIn(self.product.name, body)
        self.assertIn(str(self.order), body)
        self.assertIn(self.buyer.username, body)
        for label in ("Products", "Orders", "Customers"):
            self.assertIn(f"<h2>{label} (", body)

    def test_each_result_links_to_the_objects_own_admin_page(self):
        body = search(self.client, "Zephyrine")
        for obj in (self.product, self.order, self.buyer):
            opts = obj._meta
            self.assertIn(
                f'href="/admin/{opts.app_label}/{opts.model_name}/{obj.pk}/change/"',
                body,
            )

    def test_results_are_capped_per_model_with_a_way_on(self):
        """The page stays bounded, and the full set is one click away on the
        changelist — which re-applies its own capability gate."""
        for index in range(RESULTS_PER_MODEL + 3):
            self.make_product(name=f"Marigold {index}")
        body = search(self.client, "Marigold")
        self.assertEqual(
            body.count('<li><a href="/admin/products/products/'), RESULTS_PER_MODEL
        )
        self.assertIn(f"Showing the first {RESULTS_PER_MODEL}", body)
        self.assertIn(f"all {RESULTS_PER_MODEL + 3} in Products", body)
        self.assertIn('href="/admin/products/products/?q=Marigold"', body)

    def test_a_search_with_no_matches_is_a_clean_empty_state(self):
        body = search(self.client, "no-such-thing-anywhere")
        self.assertIn("No matches for", body)
        self.assertNotIn("<h2>", body)

    def test_no_term_is_a_prompt_not_an_error(self):
        for params in ({}, {"q": ""}, {"q": "   "}):
            with self.subTest(params=params):
                res = self.client.get(SEARCH_URL, params)
                self.assertEqual(res.status_code, 200)
                self.assertContains(res, "Type at least one character")

    def test_an_over_long_term_is_bounded_before_it_is_searched(self):
        """The term is an icontains scan, so the bound keeps a pasted wall of
        text from becoming one; the page shows the bounded term it used."""
        body = search(self.client, "Z" * (MAX_TERM_LENGTH + 50))
        self.assertIn(f'value="{"Z" * MAX_TERM_LENGTH}"', body)
        self.assertIn("No matches for", body)


@tag("e2e")
class GlobalSearchCapabilityTests(ApiTestCase):
    """Per-model gating: a gated model's rows never appear, nor its name."""

    def setUp(self):
        self.buyer = self.make_user("nightfall-buyer")
        self.product = self.make_product(name="Nightfall")
        self.order = make_order(self.buyer, full_name="Nightfall Buyer")

    def test_a_products_only_role_sees_no_order_or_customer(self):
        """Marketing holds ``products.read`` and nothing else: the orders and
        customers groups are absent entirely, so the page cannot confirm
        that a matching order or customer exists."""
        self.client.force_login(make_role_user(ROLE_MARKETING, "search-marketing"))
        body = search(self.client, "Nightfall")
        self.assertIn(self.product.name, body)
        self.assertIn("<h2>Products (", body)
        self.assertNotIn("<h2>Orders (", body)
        self.assertNotIn("<h2>Customers (", body)
        self.assertNotIn(str(self.order), body)
        self.assertNotIn(self.buyer.username, body)

    def test_a_gated_model_is_never_even_queried(self):
        """The capability check precedes the query, so a role without
        ``orders.read`` costs a products search and no order query at all."""
        self.client.force_login(make_role_user(ROLE_MARKETING, "search-marketing"))
        with CaptureQueriesContext(connection) as queries:
            search(self.client, "Nightfall")
        issued = " ".join(statement["sql"] for statement in queries)
        self.assertIn("products_products", issued)
        self.assertNotIn("orders_order", issued)

    def test_a_finance_role_sees_orders_and_customers_but_not_products(self):
        self.client.force_login(make_role_user(ROLE_FINANCE, "search-finance"))
        body = search(self.client, "Nightfall")
        self.assertIn(str(self.order), body)
        self.assertIn(self.buyer.username, body)
        self.assertNotIn("<h2>Products (", body)
        # No product change link at all: the term appears on the page (it is
        # in the box), so the leak signal is the result link, not the word.
        self.assertNotIn("/admin/products/products/", body)

    def test_a_role_that_may_view_none_of_them_is_refused(self):
        """Least privilege: a staff account with no mapped read capability
        gets a 403, exactly as the changelists refuse it — not an empty
        page that would read as "nothing exists"."""
        roless = User.objects.create_user(
            username="search-roleless",
            email="search-roleless@example.com",
            password=TEST_PASSWORD,
            is_staff=True,
        )
        self.client.force_login(roless)
        res = self.client.get(SEARCH_URL, {"q": "Nightfall"})
        self.assertEqual(res.status_code, 403)

    def test_anonymous_callers_are_sent_to_the_admin_login(self):
        res = self.client.get(SEARCH_URL, {"q": "Nightfall"})
        self.assertEqual(res.status_code, 302)
        self.assertIn("/admin/login/", res["Location"])

    def test_the_superuser_bypass_still_sees_everything(self):
        root = User.objects.create_superuser(
            username="search-root",
            email="search-root@example.com",
            password=TEST_PASSWORD,
        )
        self.client.force_login(root)
        body = search(self.client, "Nightfall")
        self.assertIn(self.product.name, body)
        self.assertIn(str(self.order), body)
        self.assertIn(self.buyer.username, body)


class GlobalSearchStaffGateTests(ApiTestCase):
    """SPEC-20-6b: this route lives in the admin area, so it needs staff.

    Holding a search capability was once enough to open it, which let a
    non-staff account that carried a role group read ``Orders (n)`` here and
    follow a live change-form link — while ``/admin/orders/order/`` bounced
    the same account to the login, because every Django admin view is
    ``staff_member_required``. The chrome box in ``base_site.html`` is gated
    on ``is_staff``, so the route now agrees with the box above it.

    Only database tampering can produce a non-staff role holder (the product
    refuses roles for non-staff accounts), so these pins are deliberately
    about the route being uniform with the rest of the admin area, not about
    a reachable product flow.
    """

    def setUp(self):
        self.buyer = self.make_user("vellum-buyer")
        self.order = make_order(self.buyer, full_name="Vellum Buyer")
        support = Group.objects.get_or_create(name=ROLE_SUPPORT)[0]
        # The same role, the only difference being is_staff.
        self.support_staff = User.objects.create_user(
            username="support-staff",
            email="support-staff@example.com",
            password=TEST_PASSWORD,
            is_staff=True,
        )
        self.support_staff.groups.add(support)
        self.support_nonstaff = User.objects.create_user(
            username="support-nonstaff",
            email="support-nonstaff@example.com",
            password=TEST_PASSWORD,
        )
        self.support_nonstaff.groups.add(support)

    def test_a_staff_capability_holder_still_searches(self):
        """No regression: the staff flag is an addition to the capability, not
        a replacement for it."""
        self.client.force_login(self.support_staff)
        body = search(self.client, "Vellum")
        self.assertIn(str(self.order), body)
        self.assertIn("<h2>Orders (", body)

    def test_a_non_staff_capability_holder_is_refused(self):
        """Precondition: the capability is genuinely held (support maps
        ``orders.read``), so the refusal is the staff gate and nothing else."""
        self.assertTrue(user_has_capability(self.support_nonstaff, "orders.read"))
        self.client.force_login(self.support_nonstaff)
        res = self.client.get(SEARCH_URL, {"q": "Vellum"})
        self.assertEqual(res.status_code, 403)
        content = res.content.decode()
        self.assertNotIn("<h2>Orders (", content)
        self.assertNotIn(f"/admin/orders/order/{self.order.pk}/change/", content)

    def test_the_route_agrees_with_the_changelists_it_links_to(self):
        """Uniform gate: the non-staff caller is sent to the admin login by
        the sibling changelist, and refused by this page."""
        self.client.force_login(self.support_nonstaff)
        changelist = self.client.get("/admin/orders/order/")
        self.assertEqual(changelist.status_code, 302)
        self.assertIn("/admin/login/", changelist["Location"])
        self.assertEqual(self.client.get(SEARCH_URL, {"q": "Vellum"}).status_code, 403)


class GlobalSearchSurfaceTests(ApiTestCase):
    """The box is in the chrome, and the registry cannot drift."""

    def test_the_registry_capabilities_are_the_model_admins_own(self):
        """``SEARCH_CAPABILITIES`` is a decorator argument, so it is spelled
        out in code — this is the pin that keeps it equal to what each
        ModelAdmin actually enforces."""
        declared = {
            model_admin.capability_map["view"]
            for model_admin in (
                registered_model_admin(target.app_label, target.model_name)
                for target in SEARCH_TARGETS
            )
            if model_admin is not None
        }
        self.assertEqual(declared, set(SEARCH_CAPABILITIES))

    def test_every_search_target_is_a_registered_changelist(self):
        for target in SEARCH_TARGETS:
            with self.subTest(target=target.model_name):
                self.assertIsNotNone(
                    registered_model_admin(target.app_label, target.model_name)
                )

    def test_a_renamed_model_degrades_to_no_results_not_a_crash(self):
        """The registry is resolved by name, so a model or app that goes
        away leaves the page working (one model fewer) instead of 500ing."""
        self.assertIsNone(registered_model_admin("common", "no_such_model"))
        self.assertIsNone(registered_model_admin("no_such_app", "thing"))

    def test_the_search_box_is_in_the_admin_chrome_for_staff(self):
        user = make_role_user(ROLE_ADMIN, "chrome-chief")
        self.client.force_login(user)
        for url in ("/admin/", "/admin/orders/order/", "/admin/dashboard/"):
            with self.subTest(url=url):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 200)
                self.assertContains(res, 'action="/admin/search/"')
                self.assertContains(res, 'id="global-search"')

    def test_the_per_model_search_box_is_untouched(self):
        """The global box is additive chrome: the changelist's own search
        form (its declared ``search_fields``) still renders above the grid."""
        self.client.force_login(make_role_user(ROLE_ADMIN, "chrome-grid"))
        res = self.client.get("/admin/orders/order/")
        self.assertContains(res, 'id="searchbar"')
        self.assertContains(res, 'name="q"')

    def test_the_login_page_shows_no_search_box(self):
        res = self.client.get("/admin/login/")
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, 'id="global-search"')

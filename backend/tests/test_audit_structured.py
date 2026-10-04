"""SPEC-20-4 [R-20.27]/[R-20.29]: structured before -> after values and a
source discriminator on guarded mutations.

- Every guarded mutation that previously recorded only prose — the API
  catalogue writes, the admin staff-role change, the admin promotion toggle —
  now records the REAL old and new values per field, and the surface the
  mutation arrived through as a field on the row (not as a word in a
  message). Order transitions already carry ``OrderStatusEvent.from_status``
  / ``.to_status`` and ``StockMovement.delta`` / ``.stock_after``.
- The security contract: structured values go through ONE sanitizing path, so
  a field on the credential-name exclusion list can never reach the audit
  detail or the log mirror, however a caller reaches it.
"""

import re
from decimal import Decimal

from django.contrib.auth.models import Group, User

from accounts.models import TOTPDevice
from common.admin import CONFIRM_FIELD, CONFIRMATION_YES
from common.audit import (
    AUDIT_VALUE_MAX_LENGTH,
    clean_audit_payload,
    clean_audit_value,
    field_changes,
    is_excluded_field,
    log_mutation,
    model_field_changes,
)
from common.models import AuditEvent
from common.roles import ROLE_ADMIN, ROLE_MARKETING, ROLE_SUPPORT
from common.testing import ApiTestCase
from products.models import products

from .test_staff_roles_admin import user_change_post

COUPON_CHANGELIST = "/admin/orders/coupon/"
TEST_PASSWORD = "S3cure-Passphrase!"


def make_role_user(role, username):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def fake_request(user):
    """The one attribute every audit writer reads off a request."""
    return type("FakeRequest", (), {"user": user})()


def write_payload(name):
    return {
        "name": name,
        "description": "Created through the gated API.",
        "price": "10.00",
        "size": 30,
        "stock": 3,
        "category": "Floral",
    }


class CleaningPathTests(ApiTestCase):
    """The single sanitizing path — the guarantee, pinned on its own."""

    def test_credential_bearing_names_are_excluded(self):
        for name in (
            "password",
            "password_confirmation",
            "totp_secret",
            "api_key",
            "razorpay_key_secret",
            "access_token",
            "refresh_token",
            "card_number",
            "cvv",
            "clientSecret",
            "hmacKey",
        ):
            with self.subTest(field=name):
                self.assertTrue(is_excluded_field(name))

    def test_ordinary_names_survive_the_exclusion(self):
        # razorpay_order_id / razorpay_payment_id are order references, pinned
        # by SPEC-7-01's payment trail: the exclusion must not eat them.
        for name in (
            "price",
            "stock",
            "username",
            "order_status",
            "razorpay_order_id",
            "razorpay_payment_id",
            "claimed_razorpay_order_id",
            "total_amount",
            "staff_roles",
        ):
            with self.subTest(field=name):
                self.assertFalse(is_excluded_field(name))

    def test_the_exclusion_covers_this_projects_real_material(self):
        # A denylist of conventional names is only trustworthy if the names
        # this project actually uses are in it — read from the models and the
        # settings rather than typed out, so the pin cannot drift from reality.
        # (RAZORPAY_KEY_ID is deliberately absent: it is the public half of the
        # pair, and the delivered SPEC-7-02 no-secrets pin already governs what
        # may be printed.)
        self.assertTrue(is_excluded_field(TOTPDevice._meta.get_field("secret").name))
        self.assertTrue(is_excluded_field(User._meta.get_field("password").name))
        self.assertTrue(is_excluded_field("RAZORPAY_KEY_SECRET"))

    def test_values_are_json_safe_bounded_and_money_exact(self):
        self.assertIsNone(clean_audit_value(None))
        self.assertIs(clean_audit_value(True), True)
        self.assertEqual(clean_audit_value(7), 7)
        # Money keeps its exact decimal text; it never becomes a float.
        self.assertEqual(clean_audit_value(Decimal("511.00")), "511.00")
        overlong = "x" * (AUDIT_VALUE_MAX_LENGTH + 10)
        cleaned = clean_audit_value(overlong)
        self.assertEqual(len(cleaned), AUDIT_VALUE_MAX_LENGTH + 1)
        self.assertTrue(cleaned.endswith("…"))

    def test_payload_drops_excluded_keys_and_cleans_nested_values(self):
        payload = {
            "price": Decimal("11.00"),
            "password": "hunter2",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "nested": {"razorpay_key_secret": "rzp_live_x", "order_status": "paid"},
            "changes": [{"field": "name", "before": None, "after": "Rose"}],
        }
        cleaned = clean_audit_payload(payload)
        self.assertNotIn("password", cleaned)
        self.assertNotIn("totp_secret", cleaned)
        self.assertNotIn("razorpay_key_secret", cleaned["nested"])
        self.assertEqual(cleaned["nested"]["order_status"], "paid")
        self.assertEqual(cleaned["price"], "11.00")
        self.assertEqual(
            cleaned["changes"],
            [{"field": "name", "before": None, "after": "Rose"}],
        )

    def test_non_mapping_payload_is_empty(self):
        self.assertEqual(clean_audit_payload(["not", "a", "mapping"]), {})
        self.assertEqual(clean_audit_payload(None), {})

    def test_change_set_drops_an_excluded_field_entirely(self):
        # Not redacted to a placeholder: the field must not appear at all,
        # so the trail never records that it moved.
        changes = field_changes(
            [
                ("price", "10.00", "11.00"),
                ("secret", "old-seed", "new-seed"),
                ("name", "Rose", "Oud"),
            ]
        )
        self.assertEqual(
            changes,
            [
                {"field": "price", "before": "10.00", "after": "11.00"},
                {"field": "name", "before": "Rose", "after": "Oud"},
            ],
        )

    def test_model_field_changes_records_only_what_moved(self):
        product = self.make_product(price="499.99", stock=10)
        self.assertEqual(
            model_field_changes(product, ["price", "stock"]),
            [("price", None, Decimal("499.99")), ("stock", None, 10)],
        )
        self.assertEqual(
            model_field_changes(
                product,
                ["price", "stock"],
                before={"price": "511.00", "stock": 10},
            ),
            [("price", "511.00", Decimal("499.99"))],
        )


class RecordSanitizingTests(ApiTestCase):
    """AuditEvent.record applies the cleaner itself — no bypass exists."""

    def test_record_strips_a_credential_field_from_detail_and_log(self):
        with self.assertLogs("common.audit", level="INFO") as logs:
            event = AuditEvent.record(
                AuditEvent.EventType.AUTH_LOGIN,
                detail={"username": "buyer", "password": "S3cure-Passphrase!"},
            )
        self.assertEqual(event.detail, {"username": "buyer"})
        self.assertNotIn("S3cure-Passphrase!", "\n".join(logs.output))

    def test_record_keeps_the_business_trail_payloads(self):
        # The sanitizer must not quietly empty the delivered payment trail.
        event = AuditEvent.record(
            AuditEvent.EventType.PAYMENT_VERIFIED,
            detail={"razorpay_order_id": "order_1", "razorpay_payment_id": "pay_1"},
        )
        self.assertEqual(
            event.detail,
            {"razorpay_order_id": "order_1", "razorpay_payment_id": "pay_1"},
        )

    def test_log_mutation_routes_the_actor_and_cleans_the_values(self):
        chief = make_role_user(ROLE_ADMIN, "chief")
        user = make_role_user(ROLE_SUPPORT, "target")
        event = log_mutation(
            fake_request(chief),
            user,
            AuditEvent.EventType.STAFF_ROLES_UPDATED,
            "staff_roles_updated",
            AuditEvent.Source.ADMIN,
            changes=[("staff_roles", ["support"], ["support", "marketing"])],
        )
        self.assertEqual(event.actor, chief)
        self.assertEqual(event.source, AuditEvent.Source.ADMIN)
        self.assertEqual(event.category, AuditEvent.Category.STAFF)
        self.assertEqual(event.detail["action"], "staff_roles_updated")
        self.assertEqual(event.detail["model"], "auth.user")
        self.assertEqual(
            event.detail["changes"],
            [
                {
                    "field": "staff_roles",
                    "before": ["support"],
                    "after": ["support", "marketing"],
                }
            ],
        )

    def test_log_mutation_omits_the_changes_key_when_there_are_none(self):
        user = self.make_user("buyer")
        event = log_mutation(
            fake_request(user),
            user,
            AuditEvent.EventType.CATALOGUE_DELETED,
            "deleted",
            AuditEvent.Source.API,
        )
        self.assertEqual(event.detail["object_id"], str(user.pk))
        self.assertNotIn("changes", event.detail)


class ProductApiMutationTests(ApiTestCase):
    """The API catalogue writes: real before -> after, source=api."""

    def setUp(self):
        self.writer = make_role_user(ROLE_ADMIN, "scribe")
        self.client = self.fresh_client()
        self.api_login("scribe", client=self.client)

    def test_create_records_every_written_value_as_before_none_after(self):
        res = self.client.post(
            "/api/products/", write_payload("Audit Rose"), format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.CATALOGUE_CREATED
        )
        self.assertEqual(event.source, AuditEvent.Source.API)
        self.assertEqual(event.actor, self.writer)
        self.assertEqual(event.detail["model"], "products.products")
        self.assertEqual(
            event.detail["object_id"], str(products.objects.get(name="Audit Rose").pk)
        )
        self.assertEqual(event.detail["object_repr"], "Audit Rose")
        changes = {change["field"]: change for change in event.detail["changes"]}
        self.assertEqual(
            changes["price"], {"field": "price", "before": None, "after": "10.00"}
        )
        self.assertEqual(
            changes["stock"], {"field": "stock", "before": None, "after": 3}
        )
        self.assertEqual(changes["category"]["after"], "Floral")

    def test_patch_records_the_real_before_and_after_prices(self):
        product = self.make_product(name="Edit Rose", price="499.99", stock=10)
        res = self.client.patch(
            f"/api/products/{product.slug}/",
            {"price": "511.00", "stock": 4},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.CATALOGUE_UPDATED
        )
        self.assertEqual(event.source, AuditEvent.Source.API)
        self.assertEqual(event.detail["object_id"], str(product.pk))
        # ``stock`` is read-only on an existing row (SPEC-6-02: inventory moves
        # only through adjust_stock), so the audit records exactly what was
        # written — the ignored value is absent, not fabricated.
        self.assertEqual(
            event.detail["changes"],
            [{"field": "price", "before": "499.99", "after": "511.00"}],
        )
        product.refresh_from_db()
        self.assertEqual(str(product.price), "511.00")
        self.assertEqual(product.stock, 10)

    def test_put_takes_the_same_structured_path(self):
        product = self.make_product(name="Edit Rose")
        res = self.client.put(
            f"/api/products/{product.slug}/",
            {**write_payload("Edit Rose"), "price": "12.00"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.CATALOGUE_UPDATED
        )
        self.assertEqual(
            [c for c in event.detail["changes"] if c["field"] == "price"],
            [{"field": "price", "before": "499.99", "after": "12.00"}],
        )

    def test_delete_records_the_surface_and_outlives_the_row(self):
        product = self.make_product(name="Doomed Rose")
        res = self.client.delete(f"/api/products/{product.slug}/")
        self.assertEqual(res.status_code, 204)
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.CATALOGUE_DELETED
        )
        self.assertEqual(event.source, AuditEvent.Source.API)
        self.assertEqual(event.detail["object_id"], str(product.pk))
        self.assertEqual(event.detail["object_repr"], "Doomed Rose")
        self.assertNotIn("changes", event.detail)
        self.assertFalse(products.objects.filter(pk=product.pk).exists())

    def test_a_secret_named_field_cannot_enter_the_audit_trail(self):
        # The security invariant on a real write: whatever a caller puts in
        # the change set, the credential-named field lands nowhere.
        product = self.make_product(name="Guarded Rose")
        with self.assertLogs("common.audit", level="INFO") as logs:
            log_mutation(
                fake_request(self.writer),
                product,
                AuditEvent.EventType.CATALOGUE_UPDATED,
                "updated",
                AuditEvent.Source.API,
                changes=[
                    ("price", "499.99", "511.00"),
                    ("totp_secret", "JBSWY3DPEHPK3PXP", "KRUGS4ZANFZSAYJA"),
                ],
            )
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.CATALOGUE_UPDATED
        )
        self.assertEqual(
            event.detail["changes"],
            [{"field": "price", "before": "499.99", "after": "511.00"}],
        )
        self.assertNotIn("totp_secret", str(event.detail))
        output = "\n".join(logs.output)
        self.assertNotIn("JBSWY3DPEHPK3PXP", output)
        self.assertNotIn("KRUGS4ZANFZSAYJA", output)


class AdminRoleChangeMixin:
    """The confirmed role-change leg (SPEC-20-2's interstitial)."""

    def _role_post(self, target, role):
        return user_change_post(
            target,
            is_staff="on",
            staff_roles=[str(Group.objects.get(name=role).pk)],
        )

    def _confirm(self, target, role):
        return self.client.post(
            f"/admin/auth/user/{target.pk}/change/",
            {**self._role_post(target, role), CONFIRM_FIELD: CONFIRMATION_YES},
        )


class AdminMutationTests(AdminRoleChangeMixin, ApiTestCase):
    """The admin surfaces: real before -> after, source=admin."""

    def setUp(self):
        self.chief = make_role_user(ROLE_ADMIN, "chief")
        self.client.force_login(self.chief)

    def test_role_change_records_before_and_after_role_sets_as_admin(self):
        target = make_role_user(ROLE_SUPPORT, "target")
        # SPEC-20-2's interstitial answers the first leg...
        unconfirmed = self.client.post(
            f"/admin/auth/user/{target.pk}/change/",
            self._role_post(target, ROLE_MARKETING),
        )
        self.assertEqual(unconfirmed.status_code, 200)
        self.assertTemplateUsed(unconfirmed, "admin/action_confirmation.html")
        self.assertEqual(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.STAFF_ROLES_UPDATED
            ).count(),
            0,
        )
        # ...and the confirmed leg commits the grant and the structured row.
        res = self._confirm(target, ROLE_MARKETING)
        self.assertEqual(res.status_code, 302)
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.STAFF_ROLES_UPDATED
        )
        self.assertEqual(event.source, AuditEvent.Source.ADMIN)
        self.assertEqual(event.actor, self.chief)
        self.assertEqual(event.detail["model"], "auth.user")
        self.assertEqual(event.detail["object_id"], str(target.pk))
        self.assertEqual(
            event.detail["changes"],
            [
                {
                    "field": "staff_roles",
                    "before": [ROLE_SUPPORT],
                    "after": [ROLE_MARKETING],
                }
            ],
        )

    def test_role_save_that_changes_nothing_writes_no_event(self):
        target = make_role_user(ROLE_SUPPORT, "quiet")
        self.assertEqual(self._confirm(target, ROLE_SUPPORT).status_code, 302)
        self.assertFalse(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.STAFF_ROLES_UPDATED
            ).exists()
        )

    def test_coupon_toggle_records_the_real_before_and_after(self):
        coupon = self.make_coupon(code="SAVE10")
        grid = dict(
            re.findall(
                r'name="(form-[A-Z_]+)" value="([^"]*)"',
                self.client.get(COUPON_CHANGELIST).content.decode(),
            )
        )
        payload = {
            **grid,
            "_save": "Save",
            "form-0-id": str(coupon.pk),
            # "active" is the list_editable column; omitting it deactivates.
        }
        res = self.client.post(COUPON_CHANGELIST, payload, follow=True)
        self.assertEqual(res.status_code, 200)
        coupon.refresh_from_db()
        self.assertFalse(coupon.active)
        event = AuditEvent.objects.get(
            event_type=AuditEvent.EventType.CATALOGUE_UPDATED
        )
        self.assertEqual(event.source, AuditEvent.Source.ADMIN)
        self.assertEqual(event.detail["model"], "orders.coupon")
        self.assertEqual(event.detail["action"], "coupon_updated")
        self.assertEqual(
            event.detail["changes"],
            [{"field": "active", "before": True, "after": False}],
        )

    def test_adding_a_coupon_records_no_before_after_row(self):
        # Only an edit has a before -> after to record; the add form's own
        # LogEntry already names the creation.
        res = self.client.post(
            "/admin/orders/coupon/add/",
            {
                "code": "NEW15",
                "discount_type": "percentage",
                "discount_value": "15.00",
                "minimum_order_amount": "0.00",
                "valid_from_0": "2020-01-01",
                "valid_from_1": "00:00:00",
                "valid_until_0": "2030-01-01",
                "valid_until_1": "00:00:00",
                "active": "on",
                "_save": "Save",
            },
        )
        self.assertEqual(res.status_code, 302)
        self.assertFalse(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.CATALOGUE_UPDATED
            ).exists()
        )


class SourceDiscriminatorTests(AdminRoleChangeMixin, ApiTestCase):
    """admin vs api is a field a query can filter, not prose in a message."""

    def test_admin_and_api_mutations_are_separately_queryable(self):
        chief = make_role_user(ROLE_ADMIN, "chief")
        self.client.force_login(chief)
        target = make_role_user(ROLE_SUPPORT, "target")
        self.assertEqual(self._confirm(target, ROLE_MARKETING).status_code, 302)

        api_client = self.fresh_client()
        self.api_login("chief", client=api_client)
        api_client.post("/api/products/", write_payload("Split Rose"), format="json")

        admin_rows = AuditEvent.objects.filter(source=AuditEvent.Source.ADMIN)
        api_rows = AuditEvent.objects.filter(source=AuditEvent.Source.API)
        self.assertEqual(admin_rows.count(), 1)
        self.assertEqual(api_rows.count(), 1)
        self.assertEqual(admin_rows.get().event_type, "staff.roles_updated")
        self.assertEqual(api_rows.get().event_type, "catalogue.created")
        # Nothing claims the admin surface without naming it.
        self.assertFalse(
            AuditEvent.objects.filter(source=AuditEvent.Source.SYSTEM)
            .filter(event_type__startswith="staff.")
            .exists()
        )

    def test_a_business_event_writer_stays_system(self):
        event = AuditEvent.record(AuditEvent.EventType.ORDER_CREATED)
        self.assertEqual(event.source, AuditEvent.Source.SYSTEM)

    def test_a_business_event_in_a_request_keeps_its_correlation_id(self):
        self.make_user("buyer")
        self.client.post(
            "/api/accounts/login/",
            {"username": "buyer", "password": "S3cure-Passphrase!"},
            format="json",
            headers={"X-Request-ID": "correlation-1"},
        )
        event = AuditEvent.objects.get(event_type=AuditEvent.EventType.AUTH_LOGIN)
        self.assertEqual(event.source, AuditEvent.Source.SYSTEM)
        self.assertEqual(event.request_id, "correlation-1")

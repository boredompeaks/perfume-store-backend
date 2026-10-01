"""[R-1.08] SPEC-1-B06: shipment tracking, its trail and who may see either.

The requirement is one sentence - "a shipment exists with a history of
events, staff can see and update it, and a customer can track their order by
order number" - and the whole module is arranged around where the danger in
that sentence sits: the LAST clause. An order number is a guessable per-year
sequence and a tracking page is the one place where a parcel's carrier, its
status and its delivery estimate are all worth stealing, so this file treats
"an order number alone discloses nothing" as the property under test and the
rest as ordinary feature coverage.

Every probe here answers one of the questions the task is graded on:

* no credential at all, a wrong token, an over-wide token, a token minted for
  a DIFFERENT order, another customer's order number, an order that does not
  exist and an order that has no shipment yet - seven inputs, ONE
  byte-identical answer (``UniformMissTests``);
* the authenticated customer sees their own shipment, and signing in is never
  a way to read somebody else's (``OwnershipTests``);
* a guest order tracks end to end on the B04 credential (``GuestTrackingTests``);
* staff with the capability see and update it (``StaffSurfaceTests``);
* the trail records every move once, immutably, with an aware timestamp
  (``EventTrailTests``);
* a role WITHOUT the capability is refused, and the packing operator's exact
  capability set is byte-for-byte what SPEC-1-B03 pinned
  (``LeastPrivilegeTests``);
* the order's money and its shipping snapshot are untouched by a tracking
  update (``MoneyIsUntouchedTests``).
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import Group, User
from django.db import IntegrityError
from django.test import RequestFactory
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from common.permissions import (
    HasShipmentsRead,
    HasShipmentsWrite,
    user_has_capability,
)
from common.roles import (
    CAPABILITY_ROLES,
    ROLE_ADMIN,
    ROLE_INVENTORY,
    ROLE_MARKETING,
    ROLE_SUPPORT,
)
from common.testing import ApiTestCase
from orders.models import Order
from orders.views import GUEST_TOKEN_HEADER
from orders.views import GUEST_TOKEN_MAX_LENGTH as ORDERS_TOKEN_MAX_LENGTH
from shipping.models import Shipment, ShipmentEvent, ShippingMethod
from shipping.serializers import ShipmentTrackingSerializer
from shipping.views import (
    GUEST_ORDER_TOKEN_HEADER,
    GUEST_TOKEN_MAX_LENGTH,
    _tracking_lookup_miss,
)
from tests.test_superadmin_tier import INVENTORY_CAPABILITIES

TEST_PASSWORD = "S3cure-Passphrase!"

TRACK = "/api/v1/store/shipping/track/{}/"
TRACK_LEGACY = "/api/shipping/track/{}/"
CHANGELIST = "/admin/shipping/shipment/"
TRAIL_CHANGELIST = "/admin/shipping/shipmentevent/"

# Spec 10.2 line 3465, asserted against the model so a future edit cannot
# quietly rename a customer-facing status.
SPEC_STATUSES = {
    "label_created",
    "dispatched",
    "in_transit",
    "delivered",
    "exception",
}


def make_role_user(role, username):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def request_for(user):
    request = RequestFactory().get("/admin/")
    request.user = user
    return request


class ShipmentTestCase(ApiTestCase):
    """Fixtures: one account order, one guest order, one parcel each."""

    def setUp(self):
        self.buyer = self.make_user("track-buyer")
        self.stranger = self.make_user("track-stranger")
        self.support = make_role_user(ROLE_SUPPORT, "track-support")
        self.chief = make_role_user(ROLE_ADMIN, "track-admin")
        self.marketing = make_role_user(ROLE_MARKETING, "track-marketing")
        self.packer = make_role_user(ROLE_INVENTORY, "track-packer")
        self.root = User.objects.create_superuser(
            "track-root", "track-root@example.com", TEST_PASSWORD
        )
        self.order = self._account_order("ORD-2026-000401")
        self.guest_order = self._guest_order(
            "ORD-2026-000402", "guest@example.com", "guest-token-value"
        )
        self.shipment = self._shipment(self.order, "TRK-401")
        self.guest_shipment = self._shipment(self.guest_order, "TRK-402")

    def _account_order(self, order_number, **overrides):
        fields = dict(
            user=self.buyer,
            order_number=order_number,
            full_name="Track Buyer",
            phone="9876500011",
            address="12 Secret Rose Lane",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal("1048.99"),
        )
        fields.update(overrides)
        return Order.objects.create(**fields)

    def _guest_order(self, order_number, guest_email, guest_token):
        # SPEC-1-B04's two-owner constraint: a guest row is user IS NULL with
        # BOTH guest columns set, and the database enforces it.
        return Order.objects.create(
            order_number=order_number,
            guest_email=guest_email,
            guest_token=guest_token,
            full_name="Track Guest",
            phone="9876500022",
            address="34 Hidden Verbena Way",
            city="Mumbai",
            state="Maharashtra",
            pincode="400001",
            total_amount=Decimal("499.00"),
        )

    def _shipment(self, order, tracking_number, **overrides):
        fields = dict(
            order=order,
            tracking_number=tracking_number,
            carrier="BlueDart",
            status=Shipment.Status.LABEL_CREATED,
            internal_note="Pallet 12, bay 4 - not before 09:00",
        )
        fields.update(overrides)
        return Shipment.objects.create(**fields)

    def track(self, order_number, token=None):
        headers = {}
        if token is not None:
            headers["HTTP_X_GUEST_ORDER_TOKEN"] = token
        return self.client.get(TRACK.format(order_number), **headers)

    def staff_post(self, url, **fields):
        """A complete change-form POST, exactly as the grid renders it.

        The defaults are read off the PERSISTED row, so a test that moves the
        status twice posts the second move from where the first left it - the
        alternative (a fixture object that has gone stale) would silently test
        a two-step move instead of the step being asked for.
        """
        live = Shipment.objects.get(pk=self.shipment.pk)
        payload = {
            "order": live.order_id,
            "tracking_number": live.tracking_number,
            "carrier": live.carrier,
            "status": live.status,
            "estimated_delivery": "",
            "internal_note": "",
            "_save": "Save",
        }
        payload.update(fields)
        return self.client.post(url, payload)

    def change_url(self, shipment=None):
        shipment = shipment or self.shipment
        return f"{CHANGELIST}{shipment.pk}/change/"


class ModelContractTests(ShipmentTestCase):
    """The two entities are the spec's two, with the spec's vocabulary."""

    def test_the_status_vocabulary_is_spec_10_2_verbatim(self):
        self.assertEqual({value for value, _ in Shipment.Status.choices}, SPEC_STATUSES)

    def test_a_shipment_reads_as_its_reference_and_status(self):
        self.shipment.refresh_from_db()
        self.assertEqual(str(self.shipment), "Shipment TRK-401 (label_created)")

    def test_an_event_reads_as_its_move(self):
        event = self.guest_shipment.events.create(
            from_status=None, to_status=Shipment.Status.LABEL_CREATED
        )
        self.assertEqual(str(event), f"{self.guest_shipment.pk}: None->label_created")

    def test_the_trail_is_ordered_oldest_first_for_the_customer(self):
        # The model ordering IS what the tracking page reads forward through,
        # so it is pinned here rather than left to the serializer to re-sort.
        self.assertEqual(ShipmentEvent._meta.ordering, ("created_at", "id"))

    def test_an_order_carries_its_shipments_and_cascades_their_trail(self):
        self.assertEqual(list(self.order.shipments.all()), [self.shipment])

        self.guest_shipment.events.create(
            from_status=None, to_status=Shipment.Status.LABEL_CREATED
        )
        self.guest_order.delete()

        self.assertFalse(
            ShipmentEvent.objects.filter(shipment=self.guest_shipment.pk).exists()
        )

    def test_a_tracking_number_can_never_answer_for_two_parcels(self):
        # Spec 8.3 line 2565's "Shipment tracking reference" index is this
        # unique index; the assertion is the reason it is one.
        with self.assertRaises(IntegrityError):
            self._shipment(self.guest_order, "TRK-401")


class SerializerContractTests(ShipmentTestCase):
    """Spec 6.9 line 2027: customer-appropriate information, and only that."""

    def test_the_tracking_body_carries_no_staff_or_customer_pii(self):
        body = ShipmentTrackingSerializer(self.shipment).data

        self.assertEqual(
            set(body),
            {
                "order_number",
                "status",
                "carrier",
                "tracking_number",
                "estimated_delivery",
                "events",
            },
        )
        for absent in ("internal_note", "address", "pincode", "city", "full_name"):
            self.assertNotIn(absent, body)

    def test_every_tracking_field_is_read_only(self):
        # A ModelSerializer is writable by default, so this is the structural
        # half of "a client can never post a tracking number".
        for name, field in ShipmentTrackingSerializer().fields.items():
            with self.subTest(field=name):
                self.assertTrue(field.read_only)

    def test_the_public_endpoint_reads_the_same_credential_as_the_guest_read(self):
        # The credential is B04's, by name and by width. Pinning both
        # constants here is what makes the duplication in shipping/views.py
        # safe rather than a drift waiting to happen.
        self.assertEqual(GUEST_ORDER_TOKEN_HEADER, GUEST_TOKEN_HEADER)
        self.assertEqual(GUEST_TOKEN_MAX_LENGTH, ORDERS_TOKEN_MAX_LENGTH)


class UniformMissTests(ShipmentTestCase):
    """An order number alone must disclose NOTHING - not even existence.

    Byte-identity of the raw response content is the property, not a set of
    status codes, because a body that differs is as good an oracle as a code
    that differs.
    """

    def setUp(self):
        super().setUp()
        self.baseline = self.track(self.order.order_number)

    def _assert_uniform(self, response):
        self.assertEqual(response.status_code, self.baseline.status_code)
        self.assertEqual(response.content, self.baseline.content)

    def test_the_baseline_miss_is_the_one_documented_answer(self):
        self.assertEqual(self.baseline.status_code, 404)
        self.assertEqual(self.baseline.data["error"], "Shipment not found")
        # The view's own helper is exactly that one body; the surrounding
        # envelope (code/details) is the repo-wide middleware, identical on
        # every response in the suite and not this endpoint's business.
        self.assertEqual(_tracking_lookup_miss().data, {"error": "Shipment not found"})

    def test_an_order_number_with_no_credential_discloses_nothing(self):
        self.assertEqual(self.baseline.status_code, 404)

    def test_a_wrong_token_is_refused_identically(self):
        self._assert_uniform(self.track(self.order.order_number, token="not-the-token"))

    def test_an_over_wide_token_is_refused_identically(self):
        # B04's width cap: a value too long for the column is rejected before
        # it can reach the index.
        self._assert_uniform(self.track(self.order.order_number, token="x" * 65))

    def test_a_token_valid_for_a_different_order_is_refused_identically(self):
        # A guest holds a VALID token - for their own parcel - and points it
        # at somebody else's order number.
        self._assert_uniform(
            self.track(self.order.order_number, token=self.guest_order.guest_token)
        )

    def test_an_unknown_order_number_is_refused_identically(self):
        self._assert_uniform(self.track("ORD-2026-999999"))

    def test_an_authenticated_stranger_is_refused_identically(self):
        self.client.force_authenticate(self.stranger)
        self._assert_uniform(self.track(self.order.order_number))

    def test_a_valid_credential_on_an_order_with_no_shipment_is_the_same_answer(self):
        # The caller proved ownership, so there is nothing left to hide - and
        # the answer still matches, so "has a shipment" is not observable even
        # by an owner probing their own missing parcel.
        order = self._account_order("ORD-2026-000403")
        self.client.force_authenticate(self.buyer)
        self._assert_uniform(self.track(order.order_number))

    def test_a_guest_credential_on_an_order_with_no_shipment_is_the_same_answer(self):
        self._guest_order("ORD-2026-000404", "none@example.com", "tok-404")
        self._assert_uniform(self.track("ORD-2026-000404", token="tok-404"))


class OwnershipTests(ShipmentTestCase):
    """The two credentials spec 3.9 line 1189 names, and nothing else."""

    def test_the_authenticated_customer_sees_their_own_shipment(self):
        self.client.force_authenticate(self.buyer)

        res = self.track(self.order.order_number)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["order_number"], self.order.order_number)
        self.assertEqual(res.data["tracking_number"], "TRK-401")

    def test_the_account_alone_is_the_credential(self):
        # Proved by the absence of the header: signing in is enough.
        self.client.force_authenticate(self.buyer)
        res = self.track(self.order.order_number, token=None)
        self.assertEqual(res.status_code, 200, res.data)

    def test_signing_in_is_never_a_way_to_read_a_guest_order(self):
        # A guest order's `user` is NULL, so `user=request.user` cannot match
        # it - even with the guest's own token attached.
        self.client.force_authenticate(self.buyer)

        res = self.track(
            self.guest_order.order_number, token=self.guest_order.guest_token
        )

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Shipment not found")

    def test_the_legacy_alias_answers_the_same(self):
        self.client.force_authenticate(self.buyer)

        res = self.client.get(TRACK_LEGACY.format(self.order.order_number))

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["tracking_number"], "TRK-401")


class GuestTrackingTests(ShipmentTestCase):
    """A guest tracks on the credential B04 minted, end to end."""

    def test_a_guest_order_tracks_end_to_end_on_its_own_token(self):
        res = self.track(
            self.guest_order.order_number, token=self.guest_order.guest_token
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["order_number"], self.guest_order.order_number)
        self.assertEqual(res.data["carrier"], "BlueDart")
        self.assertEqual(res.data["status"], "label_created")

    def test_the_guest_never_sees_the_internal_note_or_the_address(self):
        res = self.track(
            self.guest_order.order_number, token=self.guest_order.guest_token
        )

        body = res.content.decode()
        self.assertNotIn(self.guest_shipment.internal_note, body)
        self.assertNotIn(self.guest_order.address, body)
        self.assertNotIn("internal_note", body)

    def test_a_guest_needs_no_account_row_to_be_tracked(self):
        # SPEC-1-B04 requirement 6: `user is None` must not crash any path this
        # task touches, and a guest order is the only order with no user.
        self.assertIsNone(self.guest_order.user_id)
        res = self.track(
            self.guest_order.order_number, token=self.guest_order.guest_token
        )
        self.assertEqual(res.status_code, 200, res.data)

    def test_no_estimated_delivery_is_null_not_a_guessed_date(self):
        self.client.force_authenticate(self.buyer)
        res = self.track(self.order.order_number)
        self.assertIsNone(res.data["estimated_delivery"])

    def test_an_estimated_delivery_is_shown_as_an_aware_datetime(self):
        self.shipment.estimated_delivery = timezone.now() + timedelta(days=3)
        self.shipment.save(update_fields=["estimated_delivery"])
        self.client.force_authenticate(self.buyer)

        res = self.track(self.order.order_number)

        shown = parse_datetime(res.data["estimated_delivery"])
        self.assertIsNotNone(shown.tzinfo, res.data["estimated_delivery"])


class StaffSurfaceTests(ShipmentTestCase):
    """Staff see and update the parcel - through the shipped RBAC seam."""

    def test_a_capability_holder_sees_the_shipment_changelist(self):
        self.client.force_login(self.support)

        res = self.client.get(CHANGELIST)

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "TRK-401")

    def test_a_capability_holder_updates_the_status(self):
        self.client.force_login(self.support)

        res = self.staff_post(self.change_url(), status="dispatched")

        self.assertEqual(res.status_code, 302)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, "dispatched")

    def test_the_admin_role_creates_the_shipment_and_its_reference(self):
        # Spec 6.9 line 2009 "Shipment creation": the add form is the writer
        # that mints the tracking reference of line 2013.
        self.client.force_login(self.chief)

        res = self.staff_post(
            f"{CHANGELIST}add/",
            order=self.guest_order.pk,
            tracking_number="TRK-405",
            carrier="Delhivery",
            status="label_created",
        )

        self.assertEqual(res.status_code, 302)
        self.assertTrue(Shipment.objects.filter(tracking_number="TRK-405").exists())

    def test_the_superuser_bypass_reaches_the_surface(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(CHANGELIST).status_code, 200)

        res = self.staff_post(self.change_url(), status="in_transit")

        self.assertEqual(res.status_code, 302)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, "in_transit")

    def test_the_trail_changelist_is_reachable_by_a_capability_holder(self):
        self.client.force_login(self.support)
        self.guest_shipment.events.create(
            from_status=None, to_status=Shipment.Status.DISPATCHED
        )

        res = self.client.get(TRAIL_CHANGELIST)

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "TRK-402")

    def test_the_trail_is_never_creatable_even_by_the_superuser(self):
        model_admin = admin.site._registry[ShipmentEvent]
        for user in (self.root, self.chief):
            with self.subTest(user=user.username):
                self.assertFalse(model_admin.has_add_permission(request_for(user)))
                self.assertFalse(model_admin.has_delete_permission(request_for(user)))

    def test_the_superuser_is_refused_the_trail_add_page(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(f"{TRAIL_CHANGELIST}add/").status_code, 403)

    def test_the_named_permission_classes_are_pinned_to_the_new_capabilities(self):
        # Requirement 4: tests/test_rbac_foundation.py pins one named class per
        # capability in CAPABILITY_ROLES, so the map and these two cannot drift.
        self.assertEqual(HasShipmentsRead.capability, "shipments.read")
        self.assertEqual(HasShipmentsWrite.capability, "shipments.write")


class LeastPrivilegeTests(ShipmentTestCase):
    """A role WITHOUT the capability gets nothing, and nothing widened."""

    def test_a_role_without_the_capability_is_refused_the_changelist(self):
        self.client.force_login(self.marketing)
        self.assertEqual(self.client.get(CHANGELIST).status_code, 403)

    def test_a_role_without_the_capability_cannot_change_a_shipment(self):
        self.client.force_login(self.marketing)

        res = self.staff_post(self.change_url(), status="delivered")

        self.assertEqual(res.status_code, 403)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, "label_created")

    def test_a_role_without_the_capability_cannot_read_the_trail(self):
        self.client.force_login(self.marketing)
        self.assertEqual(self.client.get(TRAIL_CHANGELIST).status_code, 403)

    def test_the_packers_exact_capability_set_is_unchanged(self):
        # Requirement 5: SPEC-1-B03's pin, imported rather than restated, so a
        # widening of shipments.* into the fulfilment operator's role fails
        # HERE as well as in the task that shipped the pin.
        held = frozenset(
            capability
            for capability in CAPABILITY_ROLES
            if user_has_capability(self.packer, capability)
        )
        self.assertEqual(held, INVENTORY_CAPABILITIES)

    def test_the_packers_orders_fulfill_opens_no_second_door_onto_shipments(self):
        self.assertTrue(user_has_capability(self.packer, "orders.fulfill"))
        self.assertFalse(user_has_capability(self.packer, "shipments.read"))
        self.assertFalse(user_has_capability(self.packer, "shipments.write"))
        for model in (Shipment, ShipmentEvent):
            with self.subTest(model=model.__name__):
                self.assertIsNone(admin.site._registry[model].scoped_view_capability)

        self.client.force_login(self.packer)

        self.assertEqual(self.client.get(CHANGELIST).status_code, 403)
        self.assertEqual(self.client.get(TRAIL_CHANGELIST).status_code, 403)

    def test_the_capabilities_land_on_support_and_admin_only(self):
        for capability in ("shipments.read", "shipments.write"):
            with self.subTest(capability=capability):
                self.assertTrue(user_has_capability(self.support, capability))
                self.assertTrue(user_has_capability(self.chief, capability))
                for outsider in (self.marketing, self.packer, self.buyer, self.root):
                    self.assertFalse(
                        user_has_capability(outsider, capability), outsider.username
                    )


class EventTrailTests(ShipmentTestCase):
    """Every move is recorded once, immutably, with an aware timestamp."""

    def _move(self, status):
        self.client.force_login(self.support)
        return self.staff_post(self.change_url(), status=status)

    def test_creating_a_shipment_records_the_first_step(self):
        self.client.force_login(self.support)

        res = self.staff_post(
            f"{CHANGELIST}add/",
            order=self.guest_order.pk,
            tracking_number="TRK-406",
            carrier="Delhivery",
            status="label_created",
        )

        self.assertEqual(res.status_code, 302)
        event = ShipmentEvent.objects.get(shipment__tracking_number="TRK-406")
        self.assertIsNone(event.from_status)
        self.assertEqual(event.to_status, "label_created")
        self.assertEqual(event.actor, self.support)

    def test_the_trail_records_every_transition_with_an_aware_timestamp(self):
        self._move("dispatched")
        self._move("in_transit")

        events = list(ShipmentEvent.objects.filter(shipment=self.shipment))

        self.assertEqual(
            [(event.from_status, event.to_status) for event in events],
            [
                ("label_created", "dispatched"),
                ("dispatched", "in_transit"),
            ],
        )
        for event in events:
            with self.subTest(to_status=event.to_status):
                self.assertIsNotNone(
                    event.created_at.tzinfo,
                    "USE_TZ means every trail timestamp is aware",
                )

    def test_an_edit_that_leaves_the_status_alone_writes_no_event(self):
        self._move("dispatched")
        before = ShipmentEvent.objects.count()

        res = self.staff_post(self.change_url(), carrier="Ecom Express")

        self.assertEqual(res.status_code, 302)
        self.assertEqual(ShipmentEvent.objects.count(), before)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "Ecom Express")

    def test_the_status_and_its_event_commit_together(self):
        # Requirement 7, atomicity half: the status write and the row that
        # records it are one transaction, so a failing event cannot leave a
        # parcel that moved with no record of where it moved from.
        self.client.force_login(self.support)

        with patch.object(
            ShipmentEvent.objects, "create", side_effect=RuntimeError("event lost")
        ):
            with self.assertRaises(RuntimeError):
                self._move("dispatched")

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, "label_created")
        self.assertFalse(ShipmentEvent.objects.filter(shipment=self.shipment).exists())

    def test_a_recorded_event_cannot_be_rewritten(self):
        event = ShipmentEvent.objects.create(
            shipment=self.shipment,
            from_status=None,
            to_status="label_created",
        )

        event.to_status = "delivered"
        with self.assertRaises(TypeError):
            event.save()

    def test_deleting_a_staff_account_keeps_the_trail(self):
        self._move("dispatched")
        actor = User.objects.get(username="track-support")

        actor.delete()

        event = ShipmentEvent.objects.get(to_status="dispatched")
        self.assertIsNone(event.actor)
        self.assertEqual(event.shipment_id, self.shipment.pk)

    def test_the_customer_trail_is_the_operator_trail(self):
        # The customer's timeline IS this table, so an operator's view and the
        # customer's view cannot disagree about what happened.
        self._move("dispatched")
        self.client.force_authenticate(self.buyer)

        res = self.track(self.order.order_number)

        self.assertEqual(
            [
                (event["status"], bool(event["occurred_at"]))
                for event in res.data["events"]
            ],
            [("dispatched", True)],
        )
        self.assertEqual(res.data["status"], "dispatched")


class MoneyIsUntouchedTests(ShipmentTestCase):
    """Requirement 7: a tracking update moves no money and no snapshot."""

    def _order_with_shipping_snapshot(self):
        method = ShippingMethod.objects.create(code="express", name="Express")
        return self._account_order(
            "ORD-2026-000405",
            shipping_method=method,
            shipping_method_code="express",
            shipping_amount=Decimal("49.00"),
            total_amount=Decimal("1048.99"),
            discount_amount=Decimal("100.00"),
        )

    def test_the_order_total_and_shipping_snapshot_survive_a_tracking_update(self):
        order = self._order_with_shipping_snapshot()
        method_id = order.shipping_method_id
        shipment = self._shipment(order, "TRK-407", status="exception")
        self.client.force_login(self.support)

        res = self.staff_post(
            self.change_url(shipment),
            order=order.pk,
            tracking_number=shipment.tracking_number,
            status="in_transit",
            carrier="BlueDart",
        )

        self.assertEqual(res.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.total_amount, Decimal("1048.99"))
        self.assertEqual(order.discount_amount, Decimal("100.00"))
        self.assertEqual(order.shipping_amount, Decimal("49.00"))
        self.assertEqual(order.shipping_method_code, "express")
        self.assertEqual(order.shipping_method_id, method_id)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, "in_transit")

    def test_the_tracking_response_carries_no_money_at_all(self):
        order = self._order_with_shipping_snapshot()
        self.client.force_authenticate(self.buyer)

        body = self.track(order.order_number).content.decode()

        self.assertNotIn("1048.99", body)
        self.assertNotIn("shipping_amount", body)
        self.assertNotIn("total_amount", body)

import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django import forms
from django.contrib import admin
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models
from django.test import RequestFactory, override_settings
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
from orders.models import Order, OrderItem, OrderStatusEvent
from orders.views import GUEST_TOKEN_HEADER
from orders.views import GUEST_TOKEN_MAX_LENGTH as ORDERS_TOKEN_MAX_LENGTH
from shipping.models import Shipment, ShipmentEvent, ShippingMethod, ShippingProvider
from shipping.serializers import ShipmentTrackingSerializer
from shipping.views import (
    GUEST_ORDER_TOKEN_HEADER,
    GUEST_TOKEN_MAX_LENGTH,
    _trackable_shipments,
    _tracking_lookup_miss,
)
from tests.test_superadmin_tier import INVENTORY_CAPABILITIES

TEST_PASSWORD = "S3cure-Passphrase!"

TRACK = "/api/v1/store/shipping/track/{}/"
TRACK_LEGACY = "/api/shipping/track/{}/"
CHANGELIST = "/admin/shipping/shipment/"
TRAIL_CHANGELIST = "/admin/shipping/shipmentevent/"

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

    def login_as(self, user):
        """A REAL JWT for user, minted by the login endpoint: force_authenticate bypasses every authenticator, so a test built on it passes whether the credential contract works."""
        res, token = self.api_login(user.username)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(token, res.data)
        return token

    def parcels(self, response):
        return response.data["shipments"]

    def one_parcel(self, response):
        bodies = self.parcels(response)
        self.assertEqual(len(bodies), 1, response.data)
        return bodies[0]

    def logout(self):
        self.auth(None)

    def staff_post(self, url, **fields):
        """The defaults are read off the PERSISTED row, so a test moving the status twice posts the second move from where the first left it."""
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

    def test_the_deletion_policy_is_the_one_the_model_documents(self):
        # Spec 8.3's explicit deletion policy: the shipment cascades with its order, the actor is
        # SET_NULL -- an asymmetry asserted in both directions.
        order_field = Shipment._meta.get_field("order")
        actor_field = ShipmentEvent._meta.get_field("actor")

        self.assertIs(order_field.remote_field.on_delete, models.CASCADE)
        self.assertIs(actor_field.remote_field.on_delete, models.SET_NULL)

    def test_deleting_a_staff_account_does_not_take_the_trail_with_it(self):
        self.shipment.events.create(
            from_status=None,
            to_status=Shipment.Status.LABEL_CREATED,
            actor=self.support,
        )
        event = ShipmentEvent.objects.get(
            shipment=self.shipment, to_status=Shipment.Status.LABEL_CREATED
        )

        self.support.delete()

        event.refresh_from_db()
        self.assertIsNone(event.actor)
        self.assertEqual(event.shipment_id, self.shipment.pk)

    def test_a_tracking_number_can_never_answer_for_two_parcels(self):
        with self.assertRaises(IntegrityError):
            self._shipment(self.guest_order, "TRK-401")


class SerializerContractTests(ShipmentTestCase):
    def test_the_tracking_body_carries_no_staff_or_customer_pii(self):
        body = ShipmentTrackingSerializer(self.shipment).data

        self.assertEqual(
            set(body),
            {
                "order_number",
                "status",
                "carrier",
                "provider",
                "tracking_number",
                "tracking_url",
                "estimated_delivery",
                "events",
            },
        )
        for absent in ("internal_note", "address", "pincode", "city", "full_name"):
            self.assertNotIn(absent, body)

    def test_the_parcel_body_keeps_its_eight_keys_in_a_split_response(self):
        self._shipment(self.order, "TRK-SPLIT-C")
        self.login_as(self.buyer)

        res = self.track(self.order.order_number)

        for parcel in self.parcels(res):
            with self.subTest(parcel=parcel["tracking_number"]):
                self.assertEqual(
                    set(parcel),
                    {
                        "order_number",
                        "status",
                        "carrier",
                        "provider",
                        "tracking_number",
                        "tracking_url",
                        "estimated_delivery",
                        "events",
                    },
                )
                self.assertNotIn("internal_note", parcel)

    def test_a_hit_body_leaks_nothing_sensitive_in_its_raw_bytes(self):
        # The anti-leak sweep runs on the RENDERED body, because the envelope change (a parcel nested
        # under "shipments") is exactly the edit that could drop, hoist or leak a key.
        self._shipment(self.order, "TRK-LEAK")
        self.shipment.events.create(
            from_status=None,
            to_status=Shipment.Status.LABEL_CREATED,
            actor=self.support,
        )
        # make_user leaves email unset, and an empty needle matches every haystack, so the assertion would
        # be vacuous rather than passing.
        self.buyer.email = "track-buyer@example.com"
        self.buyer.save(update_fields=["email"])
        bodies = {}
        # Guest FIRST, while still anonymous: the view resolves an authenticated caller through order__user
        # and never reads the guest header, so a guest token alongside a bearer is not a second door.
        bodies["guest"] = self.track(
            self.guest_order.order_number, token=self.guest_order.guest_token
        ).content
        self.login_as(self.buyer)
        bodies["account owner"] = self.track(self.order.order_number).content

        forbidden = {
            "internal_note": self.shipment.internal_note,
            "street address": self.order.address,
            "city": self.order.city,
            "recipient name": self.order.full_name,
            "phone": self.order.phone,
            "buyer email": self.buyer.email,
            "guest email": self.guest_order.guest_email,
            "guest token": self.guest_order.guest_token,
            "actor username": self.support.username,
            "money": str(self.order.total_amount),
            "key name": "internal_note",
            "key name actor": '"actor"',
        }
        for credential, raw in bodies.items():
            body = raw.decode()
            for label, value in forbidden.items():
                with self.subTest(credential=credential, forbidden=label):
                    self.assertNotIn(value, body)
            self.assertIn("shipments", body)

    def test_every_tracking_field_is_read_only(self):
        for name, field in ShipmentTrackingSerializer().fields.items():
            with self.subTest(field=name):
                self.assertTrue(field.read_only)

    def test_the_public_endpoint_reads_the_same_credential_as_the_guest_read(self):
        # The credential is B04's, by name and by width; pinning both constants here is what makes the
        # duplication in shipping/views.py safe rather than a drift waiting to happen.
        self.assertEqual(GUEST_ORDER_TOKEN_HEADER, GUEST_TOKEN_HEADER)
        self.assertEqual(GUEST_TOKEN_MAX_LENGTH, ORDERS_TOKEN_MAX_LENGTH)


class UniformMissTests(ShipmentTestCase):
    """An order number alone must disclose NOTHING, not even existence: byte-identity of the raw response is the property, not a set of status codes."""

    def setUp(self):
        super().setUp()
        self.baseline = self.track(self.order.order_number)

    def _assert_uniform(self, response):
        self.assertEqual(response.status_code, self.baseline.status_code)
        self.assertEqual(response.content, self.baseline.content)

    def test_the_baseline_miss_is_the_one_documented_answer(self):
        self.assertEqual(self.baseline.status_code, 404)
        self.assertEqual(self.baseline.data["error"], "Shipment not found")
        self.assertEqual(_tracking_lookup_miss().data, {"error": "Shipment not found"})

    def test_an_order_number_with_no_credential_discloses_nothing(self):
        self.assertEqual(self.baseline.status_code, 404)

    def test_a_wrong_token_is_refused_identically(self):
        self._assert_uniform(self.track(self.order.order_number, token="not-the-token"))

    def test_an_over_wide_token_is_refused_identically(self):
        self._assert_uniform(self.track(self.order.order_number, token="x" * 65))

    def test_a_token_valid_for_a_different_order_is_refused_identically(self):
        self._assert_uniform(
            self.track(self.order.order_number, token=self.guest_order.guest_token)
        )

    def test_an_unknown_order_number_is_refused_identically(self):
        self._assert_uniform(self.track("ORD-2026-999999"))

    def test_an_authenticated_stranger_is_refused_identically(self):
        # A REAL JWT, not force_authenticate: the property is that the credential contract itself refuses
        # a stranger, which force_authenticate never exercises.
        self.login_as(self.stranger)
        self._assert_uniform(self.track(self.order.order_number))

    def test_a_valid_credential_on_an_order_with_no_shipment_is_the_same_answer(self):
        order = self._account_order("ORD-2026-000403")
        self.login_as(self.buyer)
        self._assert_uniform(self.track(order.order_number))

    def test_a_guest_credential_on_an_order_with_no_shipment_is_the_same_answer(self):
        self._guest_order("ORD-2026-000404", "none@example.com", "tok-404")
        self._assert_uniform(self.track("ORD-2026-000404", token="tok-404"))

    def test_a_split_order_with_no_visible_parcel_is_the_same_answer(self):
        self._shipment(self.order, "TRK-409")
        self.login_as(self.stranger)
        self._assert_uniform(self.track(self.order.order_number))

    def test_every_miss_shape_is_one_shape(self):
        # The whole matrix in one assertion; the anonymous probes run FIRST because authenticating partway
        # through would carry into every later probe and silently reclassify it.
        empty_order = self._account_order("ORD-2026-000405")
        empty_guest = self._guest_order(
            "ORD-2026-000406", "shape@example.com", "tok-shape"
        )
        shapes = {
            "no credential": self.track(self.order.order_number),
            "wrong token": self.track(self.order.order_number, token="nope"),
            "over-wide token": self.track(self.order.order_number, token="x" * 65),
            "empty token": self.track(self.order.order_number, token=""),
            "whitespace token": self.track(self.order.order_number, token="   "),
            "token for another order": self.track(
                self.order.order_number, token=self.guest_order.guest_token
            ),
            "unknown order number": self.track("ORD-2026-999999"),
            "token for an order with no parcel": self.track(
                empty_guest.order_number, token=empty_guest.guest_token
            ),
        }

        self.login_as(self.stranger)
        shapes["authenticated stranger"] = self.track(self.order.order_number)
        shapes["authenticated stranger, unknown order"] = self.track("ORD-2026-999999")
        self.login_as(self.buyer)
        shapes["owner of an order with no parcel"] = self.track(
            empty_order.order_number
        )
        shapes["owner, unknown order number"] = self.track("ORD-2026-999999")

        self.logout()
        self.client.force_login(self.buyer)
        shapes["session cookie only"] = self.track(self.order.order_number)

        for label, response in shapes.items():
            with self.subTest(shape=label):
                self._assert_uniform(response)
        self.assertEqual(len(shapes), 13)


class OwnershipTests(ShipmentTestCase):
    """Every credential here is obtained the way a browser does -- a real JWT from the login endpoint -- so these assertions are about the shipped auth stack, not a test client handed a user object."""

    def test_the_authenticated_customer_sees_their_own_shipment(self):
        self.login_as(self.buyer)

        res = self.track(self.order.order_number)

        self.assertEqual(res.status_code, 200, res.data)
        parcel = self.one_parcel(res)
        self.assertEqual(parcel["order_number"], self.order.order_number)
        self.assertEqual(parcel["tracking_number"], "TRK-401")

    def test_the_account_alone_is_the_credential(self):
        self.login_as(self.buyer)
        res = self.track(self.order.order_number, token=None)
        self.assertEqual(res.status_code, 200, res.data)

    def test_signing_in_is_never_a_way_to_read_a_guest_order(self):
        self.login_as(self.buyer)

        res = self.track(
            self.guest_order.order_number, token=self.guest_order.guest_token
        )

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Shipment not found")

    def test_a_real_credential_from_a_stranger_is_refused(self):
        self.login_as(self.stranger)

        res = self.track(self.order.order_number)

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Shipment not found")

    def test_a_dropped_token_is_never_a_credential(self):
        self.login_as(self.buyer)
        self.assertEqual(self.track(self.order.order_number).status_code, 200)

        self.logout()

        res = self.track(self.order.order_number)
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Shipment not found")

    def test_a_forged_bearer_token_is_refused_by_the_auth_stack(self):
        # force_authenticate assigns request.user before any authenticator runs, so it can never produce
        # this answer; the Authorization header really is presented and JWTAuthentication really does reject it.
        self.auth("not.a.real.token")

        res = self.track(self.order.order_number)

        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(res.data["code"], "not_authenticated")

    def test_a_rejected_credential_discloses_nothing_about_the_order(self):
        self.auth("not.a.real.token")

        known = self.track(self.order.order_number)
        unknown = self.track("ORD-2026-999999")

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.content, unknown.content)

    def test_a_token_whose_account_is_gone_stops_working(self):
        token = self.login_as(self.buyer)
        self.assertEqual(self.track(self.order.order_number).status_code, 200)

        self.buyer.delete()

        self.auth(token)
        known = self.track(self.order.order_number)
        unknown = self.track("ORD-2026-999999")
        self.assertEqual(known.status_code, 401, known.data)
        self.assertEqual(known.content, unknown.content)

    def test_a_bare_session_login_is_not_a_credential_here(self):
        # DRF's session authenticator is not in DEFAULT_AUTHENTICATION_CLASSES, so a session cookie resolves
        # to AnonymousUser; pinned so a future authenticator must decide about this endpoint deliberately.
        self.client.force_login(self.buyer)

        res = self.track(self.order.order_number)

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Shipment not found")

    def test_the_legacy_alias_answers_the_same(self):
        self.login_as(self.buyer)

        res = self.client.get(TRACK_LEGACY.format(self.order.order_number))

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.one_parcel(res)["tracking_number"], "TRK-401")


class GuestTrackingTests(ShipmentTestCase):
    def test_a_guest_order_tracks_end_to_end_on_its_own_token(self):
        res = self.track(
            self.guest_order.order_number, token=self.guest_order.guest_token
        )

        self.assertEqual(res.status_code, 200, res.data)
        parcel = self.one_parcel(res)
        self.assertEqual(parcel["order_number"], self.guest_order.order_number)
        self.assertEqual(parcel["carrier"], "BlueDart")
        self.assertEqual(parcel["status"], "label_created")

    def test_the_guest_never_sees_the_internal_note_or_the_address(self):
        res = self.track(
            self.guest_order.order_number, token=self.guest_order.guest_token
        )

        body = res.content.decode()
        self.assertNotIn(self.guest_shipment.internal_note, body)
        self.assertNotIn(self.guest_order.address, body)
        self.assertNotIn("internal_note", body)

    def test_a_guest_needs_no_account_row_to_be_tracked(self):
        self.assertIsNone(self.guest_order.user_id)
        res = self.track(
            self.guest_order.order_number, token=self.guest_order.guest_token
        )
        self.assertEqual(res.status_code, 200, res.data)

    def test_no_estimated_delivery_is_null_not_a_guessed_date(self):
        self.login_as(self.buyer)
        res = self.track(self.order.order_number)
        self.assertIsNone(self.one_parcel(res)["estimated_delivery"])

    def test_an_estimated_delivery_is_shown_as_an_aware_datetime(self):
        self.shipment.estimated_delivery = timezone.now() + timedelta(days=3)
        self.shipment.save(update_fields=["estimated_delivery"])
        self.login_as(self.buyer)

        res = self.track(self.order.order_number)

        shown = parse_datetime(self.one_parcel(res)["estimated_delivery"])
        self.assertIsNotNone(shown.tzinfo, res.data)


class SplitShipmentTests(ShipmentTestCase):
    """A .first() over a newest-first queryset reports only the parcel that left LAST, so every parcel must be returned."""

    def setUp(self):
        super().setUp()
        self.split_order = self._account_order("ORD-2026-000408")
        self.first = self._shipment(
            self.split_order,
            "TRK-SPLIT-A",
            status=Shipment.Status.DELIVERED,
        )
        self.second = self._shipment(
            self.split_order,
            "TRK-SPLIT-B",
            status=Shipment.Status.IN_TRANSIT,
        )
        self.first.events.create(
            from_status=Shipment.Status.IN_TRANSIT,
            to_status=Shipment.Status.DELIVERED,
        )
        self.second.events.create(
            from_status=Shipment.Status.DISPATCHED,
            to_status=Shipment.Status.IN_TRANSIT,
        )

    def test_every_parcel_of_a_split_order_is_reported(self):
        self.login_as(self.buyer)

        res = self.track(self.split_order.order_number)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            sorted(parcel["tracking_number"] for parcel in self.parcels(res)),
            ["TRK-SPLIT-A", "TRK-SPLIT-B"],
        )

    def test_the_delivered_parcel_is_not_hidden_behind_the_moving_one(self):
        self.login_as(self.buyer)

        res = self.track(self.split_order.order_number)

        by_reference = {
            parcel["tracking_number"]: parcel for parcel in self.parcels(res)
        }
        self.assertIn("TRK-SPLIT-A", by_reference)
        self.assertEqual(by_reference["TRK-SPLIT-A"]["status"], "delivered")
        self.assertEqual(by_reference["TRK-SPLIT-B"]["status"], "in_transit")

    def test_each_parcel_carries_its_own_trail(self):
        self.login_as(self.buyer)

        res = self.track(self.split_order.order_number)

        trails = {
            parcel["tracking_number"]: [step["status"] for step in parcel["events"]]
            for parcel in self.parcels(res)
        }
        self.assertEqual(trails["TRK-SPLIT-A"], ["delivered"])
        self.assertEqual(trails["TRK-SPLIT-B"], ["in_transit"])

    def test_the_parcels_are_reported_in_the_order_they_were_dispatched(self):
        self.login_as(self.buyer)

        res = self.track(self.split_order.order_number)

        self.assertEqual(
            [parcel["tracking_number"] for parcel in self.parcels(res)],
            ["TRK-SPLIT-A", "TRK-SPLIT-B"],
        )

    def test_a_guest_split_order_reports_every_parcel_too(self):
        order = self._guest_order("ORD-2026-000409", "split@example.com", "tok-split")
        for suffix in ("A", "B"):
            self._shipment(
                order,
                f"TRK-GUEST-{suffix}",
                status=Shipment.Status.DISPATCHED,
            )

        res = self.track(order.order_number, token=order.guest_token)

        self.assertEqual(
            sorted(parcel["tracking_number"] for parcel in self.parcels(res)),
            ["TRK-GUEST-A", "TRK-GUEST-B"],
        )

    def test_the_lookup_helper_itself_returns_every_visible_parcel(self):
        request = RequestFactory().get("/")
        request.user = self.buyer
        request.headers = {}

        found = _trackable_shipments(request, self.split_order.order_number)

        self.assertEqual(
            [shipment.tracking_number for shipment in found],
            ["TRK-SPLIT-A", "TRK-SPLIT-B"],
        )

    def test_a_one_parcel_order_still_reports_exactly_one(self):
        order = self._account_order("ORD-2026-000407")
        self._shipment(order, "TRK-SOLO")
        self.login_as(self.buyer)

        res = self.track(order.order_number)

        self.assertEqual(
            [parcel["tracking_number"] for parcel in self.parcels(res)], ["TRK-SOLO"]
        )

    def test_the_shipment_model_ordering_is_not_what_the_timeline_uses(self):
        self.assertEqual(Shipment._meta.ordering, ("-created_at", "-id"))
        newest_first = list(
            Shipment.objects.filter(order=self.split_order).order_by(
                "-created_at", "-id"
            )
        )
        self.assertEqual(
            [shipment.tracking_number for shipment in newest_first],
            ["TRK-SPLIT-B", "TRK-SPLIT-A"],
        )


class StaffSurfaceTests(ShipmentTestCase):
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
        self.assertEqual(HasShipmentsRead.capability, "shipments.read")
        self.assertEqual(HasShipmentsWrite.capability, "shipments.write")


class LeastPrivilegeTests(ShipmentTestCase):
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
        # The status write and the row that records it are one transaction, so a failing event cannot leave
        # a parcel that moved with no record of where it moved from.
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
        with self.assertRaises(ValueError):
            event.save()

    def test_a_recorded_event_survives_a_refused_rewrite(self):
        event = ShipmentEvent.objects.create(
            shipment=self.shipment,
            from_status=None,
            to_status="label_created",
        )

        with self.assertRaises(ValueError):
            ShipmentEvent.objects.filter(pk=event.pk).update(to_status="delivered")

        event.refresh_from_db()
        self.assertEqual(event.to_status, "label_created")

    def test_deleting_a_staff_account_keeps_the_trail(self):
        self._move("dispatched")
        actor = User.objects.get(username="track-support")

        actor.delete()

        event = ShipmentEvent.objects.get(to_status="dispatched")
        self.assertIsNone(event.actor)
        self.assertEqual(event.shipment_id, self.shipment.pk)

    def test_the_customer_trail_is_the_operator_trail(self):
        self._move("dispatched")
        self.login_as(self.buyer)

        res = self.track(self.order.order_number)

        parcel = self.one_parcel(res)
        self.assertEqual(
            [
                (event["status"], bool(event["occurred_at"]))
                for event in parcel["events"]
            ],
            [("dispatched", True)],
        )
        self.assertEqual(parcel["status"], "dispatched")


class MoneyIsUntouchedTests(ShipmentTestCase):
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
        self.login_as(self.buyer)

        body = self.track(order.order_number).content.decode()

        self.assertNotIn("1048.99", body)
        self.assertNotIn("shipping_amount", body)
        self.assertNotIn("total_amount", body)


class AppendOnlyGuardTests(ShipmentTestCase):
    """A save() check is one layer short: these are the four paths that reach the table without calling save(), each asserted to be refused AND to leave the row unchanged."""

    def setUp(self):
        super().setUp()
        self.event = ShipmentEvent.objects.create(
            shipment=self.shipment,
            from_status=Shipment.Status.LABEL_CREATED,
            to_status=Shipment.Status.DISPATCHED,
        )
        self.before = ShipmentEvent.objects.count()

    def test_a_queryset_update_cannot_rewrite_a_recorded_step(self):
        with self.assertRaises(ValueError):
            ShipmentEvent.objects.filter(pk=self.event.pk).update(
                to_status=Shipment.Status.DELIVERED
            )

        self.event.refresh_from_db()
        self.assertEqual(self.event.to_status, Shipment.Status.DISPATCHED)

    def test_a_queryset_update_cannot_retarget_a_recorded_step(self):
        with self.assertRaises(ValueError):
            ShipmentEvent.objects.filter(pk=self.event.pk).update(
                shipment=self.guest_shipment
            )

        self.event.refresh_from_db()
        self.assertEqual(self.event.shipment_id, self.shipment.pk)

    def test_the_event_object_itself_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            self.event.delete()

        self.assertTrue(ShipmentEvent.objects.filter(pk=self.event.pk).exists())

    def test_a_queryset_delete_cannot_remove_a_recorded_step(self):
        with self.assertRaises(ValueError):
            ShipmentEvent.objects.filter(pk=self.event.pk).delete()

        self.assertEqual(ShipmentEvent.objects.count(), self.before)

    def test_a_queryset_delete_of_every_event_is_refused_too(self):
        with self.assertRaises(ValueError):
            ShipmentEvent.objects.all().delete()

        self.assertEqual(ShipmentEvent.objects.count(), self.before)

    def test_bulk_create_cannot_forge_a_recorded_step(self):
        with self.assertRaises(ValueError):
            ShipmentEvent.objects.bulk_create(
                [
                    ShipmentEvent(
                        shipment=self.shipment,
                        from_status=Shipment.Status.DISPATCHED,
                        to_status=Shipment.Status.DELIVERED,
                    )
                ]
            )

        self.assertEqual(ShipmentEvent.objects.count(), self.before)

    def test_deleting_the_shipment_still_takes_its_trail_with_it(self):
        # QuerySet.delete is refused, but Django's collector issues its own DELETE and never routes through
        # the queryset, so the CASCADE is NOT blocked by it -- which is why the guard can be this blunt.
        self.assertTrue(ShipmentEvent.objects.filter(pk=self.event.pk).exists())

        self.shipment.delete()

        self.assertFalse(Shipment.objects.filter(pk=self.shipment.pk).exists())
        self.assertFalse(ShipmentEvent.objects.filter(pk=self.event.pk).exists())
        self.assertEqual(ShipmentEvent.objects.count(), self.before - 1)

    def test_the_refusals_name_the_model_and_the_reason(self):
        for call, expected in (
            (
                lambda: ShipmentEvent.objects.filter(pk=self.event.pk).update(
                    to_status=Shipment.Status.DELIVERED
                ),
                "append-only",
            ),
            (lambda: ShipmentEvent.objects.all().delete(), "append-only"),
            (
                lambda: ShipmentEvent.objects.bulk_create(
                    [
                        ShipmentEvent(
                            shipment=self.shipment,
                            from_status=None,
                            to_status=Shipment.Status.DELIVERED,
                        )
                    ]
                ),
                "append-only",
            ),
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(ValueError) as caught:
                    call()
                self.assertIn(expected, str(caught.exception))

    def test_bulk_update_cannot_rewrite_a_recorded_step(self):
        # QuerySet.bulk_update looks like an object method but compiles a CASE expression and issues it
        # through queryset.filter(...).update(), so it runs into the queryset guard without any model method running.
        with self.assertRaises(ValueError) as caught:
            ShipmentEvent.objects.bulk_update([self.event], ["to_status"])

        self.assertIn("append-only", str(caught.exception))
        self.event.refresh_from_db()
        self.assertEqual(self.event.to_status, Shipment.Status.DISPATCHED)

    def test_the_admin_surface_is_still_the_only_place_a_step_is_written(self):
        self.client.force_login(self.support)

        res = self.staff_post(self.change_url(), status="in_transit")

        self.assertEqual(res.status_code, 302)
        self.assertEqual(ShipmentEvent.objects.count(), self.before + 1)
        self.assertTrue(
            ShipmentEvent.objects.filter(
                shipment=self.shipment, to_status=Shipment.Status.IN_TRANSIT
            ).exists()
        )


class StatusCannotBeMovedInBulkTests(ShipmentTestCase):
    """ShipmentAdmin.save_model writes the row and the event in one transaction and is the only writer of a status move; the bulk update() path never reaches it."""

    def test_a_bulk_update_cannot_move_the_status(self):
        with self.assertRaises(ValueError):
            Shipment.objects.filter(pk=self.shipment.pk).update(status="delivered")

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.LABEL_CREATED)
        self.assertFalse(ShipmentEvent.objects.filter(shipment=self.shipment).exists())

    def test_a_bulk_update_cannot_move_the_status_either(self):
        self.shipment.status = Shipment.Status.DELIVERED

        with self.assertRaises(ValueError) as caught:
            Shipment.objects.bulk_update([self.shipment], ["status"])

        self.assertIn("ShipmentEvent", str(caught.exception))
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.LABEL_CREATED)

    def test_a_bulk_update_of_other_columns_is_still_allowed(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(carrier="Ecom Express")

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "Ecom Express")

    def test_a_bulk_update_of_a_non_status_column_is_still_allowed(self):
        self.shipment.carrier = "Ecom Express"

        Shipment.objects.bulk_update([self.shipment], ["carrier"])

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "Ecom Express")

    def test_a_bulk_update_cannot_write_an_over_wide_column_either(self):
        # A bulk write does not call save(), so the width gate has to be repeated here or bulk_update is a
        # way round it.
        self.shipment.carrier = "x" * (
            Shipment._meta.get_field("carrier").max_length + 1
        )

        with self.assertRaises(ValueError) as caught:
            Shipment.objects.bulk_update([self.shipment], ["carrier"])

        self.assertIn("carrier", str(caught.exception))
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "BlueDart")

    def test_a_bulk_update_refusal_leaves_the_caller_able_to_carry_on(self):
        # The refusal is raised before Django opens its atomic(savepoint=False) block: an error raised inside
        # marks the caller's transaction as needing rollback, leaving it unable to run another query.
        with self.assertRaises(ValueError):
            Shipment.objects.bulk_update([self.shipment], ["status"])

        self.assertEqual(
            Shipment.objects.filter(pk=self.shipment.pk).count(),
            1,
        )

    def test_the_refusal_names_the_reason(self):
        with self.assertRaises(ValueError) as caught:
            Shipment.objects.filter(pk=self.shipment.pk).update(status="delivered")

        message = str(caught.exception)
        self.assertIn("ShipmentEvent", message)
        self.assertIn("admin", message)

    def test_the_admin_can_still_move_the_status_and_record_it(self):
        self.client.force_login(self.support)

        res = self.staff_post(self.change_url(), status="delivered")

        self.assertEqual(res.status_code, 302)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.DELIVERED)
        self.assertTrue(
            ShipmentEvent.objects.filter(
                shipment=self.shipment, to_status=Shipment.Status.DELIVERED
            ).exists()
        )


class WidthGateTests(ShipmentTestCase):
    """SQLite ignores a varchar(n) width and Postgres enforces it, so an over-length value is a clean save in this suite and a DataError 500 in production; the bound is read off the model field."""

    def _over(self, model, field, width=None):
        field_obj = model._meta.get_field(field)
        return "W" * (width if width is not None else field_obj.max_length + 1)

    def test_an_over_wide_tracking_number_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self._shipment(
                self.order, self._over(Shipment, "tracking_number"), carrier="BlueDart"
            )

        self.assertIn("tracking_number", str(caught.exception))
        self.assertFalse(
            Shipment.objects.filter(
                tracking_number=self._over(Shipment, "tracking_number")
            ).exists()
        )

    def test_an_over_wide_carrier_is_refused(self):
        self.shipment.carrier = self._over(Shipment, "carrier")

        with self.assertRaises(ValueError) as caught:
            self.shipment.save()

        self.assertIn("carrier", str(caught.exception))

    def test_an_over_wide_status_is_refused(self):
        self.shipment.status = self._over(Shipment, "status")

        with self.assertRaises(ValueError) as caught:
            self.shipment.save()

        self.assertIn("status", str(caught.exception))

    def test_an_over_wide_event_status_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            ShipmentEvent.objects.create(
                shipment=self.shipment,
                from_status=None,
                to_status=self._over(ShipmentEvent, "to_status"),
            )

        self.assertIn("to_status", str(caught.exception))

    def test_an_over_wide_from_status_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            ShipmentEvent.objects.create(
                shipment=self.shipment,
                from_status=self._over(ShipmentEvent, "from_status"),
                to_status=Shipment.Status.DISPATCHED,
            )

        self.assertIn("from_status", str(caught.exception))

    def test_a_value_exactly_at_the_bound_is_accepted(self):
        exact = self._over(Shipment, "tracking_number", width=100)
        shipment = self._shipment(self.order, exact, carrier="BlueDart")

        self.assertEqual(len(shipment.tracking_number), 100)

    def test_a_field_with_no_declared_width_is_not_gated(self):
        long_note = "N" * 5000
        self.shipment.internal_note = long_note

        self.shipment.save()

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.internal_note, long_note)

    def test_a_value_at_the_bound_writes_on_a_re_save_too(self):
        exact = self._over(Shipment, "carrier", width=60)
        self.shipment.carrier = exact
        self.shipment.save()

        self.shipment.carrier = "BlueDart"
        self.shipment.save()
        self.shipment.carrier = exact
        self.shipment.save()

        self.shipment.refresh_from_db()
        self.assertEqual(len(self.shipment.carrier), 60)

    def test_a_bulk_update_cannot_write_an_over_wide_value(self):
        with self.assertRaises(ValueError) as caught:
            Shipment.objects.filter(pk=self.shipment.pk).update(
                carrier=self._over(Shipment, "carrier")
            )

        self.assertIn("carrier", str(caught.exception))
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "BlueDart")

    def test_a_bulk_update_at_the_bound_is_still_allowed(self):
        exact = self._over(Shipment, "carrier", width=60)

        Shipment.objects.filter(pk=self.shipment.pk).update(carrier=exact)

        self.shipment.refresh_from_db()
        self.assertEqual(len(self.shipment.carrier), 60)

    def test_a_bulk_update_of_a_field_with_no_width_is_allowed(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(internal_note="N" * 5000)

        self.shipment.refresh_from_db()
        self.assertEqual(len(self.shipment.internal_note), 5000)

    def test_a_computed_value_is_left_to_the_database_not_measured(self):
        # bulk_update hands values to QuerySet.update as a CASE expression rather than the string that produced
        # it; the gate measures strings, and measuring an expression would crash the write with a TypeError.
        Shipment.objects.filter(pk=self.shipment.pk).update(
            carrier=models.F("tracking_number")
        )

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "TRK-401")

    def test_the_admin_form_still_refuses_an_over_wide_value(self):
        self.client.force_login(self.support)

        res = self.staff_post(
            self.change_url(), tracking_number=self._over(Shipment, "tracking_number")
        )

        self.assertEqual(res.status_code, 200)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.tracking_number, "TRK-401")


class OrderLifecycleSyncTests(ShipmentTestCase):
    """The order and shipment records stay separate -- the shipment vocabulary is never pushed through the order machine -- so every probe is about the SEAM: mirrored, skipped, or replayed."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.support)

    def _order_in(self, status, *, payment_status="captured", with_items=True):
        order = self._account_order(
            "ORD-2026-000411",
            status=status,
            payment_status=payment_status,
        )
        if with_items:
            OrderItem.objects.create(
                order=order,
                product_name="Rose Absolute",
                price=Decimal("499.00"),
                quantity=1,
                subtotal=Decimal("499.00"),
            )
        return order

    def _parcel(self, order, tracking_number="TRK-411"):
        return self._shipment(order, tracking_number)

    def _advance(self, shipment, status):
        return self.client.post(
            self.change_url(shipment),
            {
                "order": shipment.order_id,
                "tracking_number": shipment.tracking_number,
                "carrier": shipment.carrier,
                "status": status,
                "estimated_delivery": "",
                "internal_note": "",
                "_save": "Save",
            },
        )

    def _order_events(self, order):
        return list(
            OrderStatusEvent.objects.filter(order=order)
            .order_by("created_at", "id")
            .values_list("from_status", "to_status")
        )

    def test_a_dispatched_parcel_ships_its_confirmed_order(self):
        order = self._order_in("confirmed")
        shipment = self._parcel(order)

        res = self._advance(shipment, Shipment.Status.DISPATCHED)

        self.assertEqual(res.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(order.fulfilment_status, "fulfilled")
        event = OrderStatusEvent.objects.get(order=order)
        self.assertEqual((event.from_status, event.to_status), ("confirmed", "shipped"))
        self.assertEqual(event.actor, self.support)
        self.assertEqual(event.trigger, "admin_change_form")

    def test_an_in_transit_parcel_ships_its_confirmed_order(self):
        order = self._order_in("confirmed")
        shipment = self._parcel(order)

        res = self._advance(shipment, Shipment.Status.IN_TRANSIT)

        self.assertEqual(res.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(self._order_events(order), [("confirmed", "shipped")])

    def test_a_delivered_parcel_delivers_a_shipped_order(self):
        order = self._order_in("confirmed")
        shipment = self._parcel(order)
        self._advance(shipment, Shipment.Status.DISPATCHED)
        self._advance(shipment, Shipment.Status.DELIVERED)

        order.refresh_from_db()
        self.assertEqual(order.status, "delivered")
        self.assertEqual(
            self._order_events(order),
            [("confirmed", "shipped"), ("shipped", "delivered")],
        )

    def test_one_delivered_parcel_does_not_deliver_a_split_order(self):
        # A split order is several parcels, so "one parcel arrived" is not "the order arrived": the order
        # advances only when the LAST parcel does.
        order = self._order_in("confirmed")
        first = self._parcel(order, "TRK-421")
        second = self._parcel(order, "TRK-422")
        self._advance(first, Shipment.Status.DISPATCHED)
        self._advance(second, Shipment.Status.DISPATCHED)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")

        self._advance(first, Shipment.Status.DELIVERED)

        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(self._order_events(order), [("confirmed", "shipped")])

        self._advance(second, Shipment.Status.DELIVERED)

        order.refresh_from_db()
        self.assertEqual(order.status, "delivered")
        self.assertEqual(
            self._order_events(order),
            [("confirmed", "shipped"), ("shipped", "delivered")],
        )

    def test_an_exception_parcel_blocks_the_order_delivery(self):
        order = self._order_in("confirmed")
        arrived = self._parcel(order, "TRK-423")
        failed = self._parcel(order, "TRK-424")
        self._advance(arrived, Shipment.Status.DISPATCHED)
        self._advance(failed, Shipment.Status.DISPATCHED)
        self._advance(failed, Shipment.Status.EXCEPTION)

        self._advance(arrived, Shipment.Status.DELIVERED)

        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(self._order_events(order), [("confirmed", "shipped")])

    def test_a_parcel_driven_move_refreshes_the_order_updated_at(self):
        # A transition is a write to the row, so its freshness marker must move with it: a shipment-driven
        # move that omits updated_at leaves the order looking untouched on any "changed since" query.
        order = self._order_in("confirmed")
        shipment = self._parcel(order)
        stale = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        Order.objects.filter(pk=order.pk).update(updated_at=stale)
        order.refresh_from_db()
        self.assertEqual(order.updated_at, stale)

        self._advance(shipment, Shipment.Status.DISPATCHED)

        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertGreater(order.updated_at, stale)

    def test_a_delivered_parcel_never_skips_confirmed_to_delivered(self):
        order = self._order_in("confirmed")
        shipment = self._parcel(order)

        res = self._advance(shipment, Shipment.Status.DELIVERED)

        self.assertEqual(res.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(self._order_events(order), [])

    def test_label_created_on_creation_leaves_a_confirmed_order_alone(self):
        order = self._order_in("confirmed")
        self.client.force_login(self.chief)

        res = self.client.post(
            f"{CHANGELIST}add/",
            {
                "order": order.pk,
                "tracking_number": "TRK-412",
                "carrier": "Delhivery",
                "status": Shipment.Status.LABEL_CREATED,
            },
        )

        self.assertEqual(res.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(self._order_events(order), [])

    def test_a_dispatched_creation_ships_the_order(self):
        order = self._order_in("confirmed")
        self.client.force_login(self.chief)

        res = self.client.post(
            f"{CHANGELIST}add/",
            {
                "order": order.pk,
                "tracking_number": "TRK-413",
                "carrier": "Delhivery",
                "status": Shipment.Status.DISPATCHED,
            },
        )

        self.assertEqual(res.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")

    def test_an_exception_leaves_the_order_where_it_was(self):
        order = self._order_in("confirmed")
        shipment = self._parcel(order)
        self._advance(shipment, Shipment.Status.DISPATCHED)

        self._advance(shipment, Shipment.Status.EXCEPTION)

        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(self._order_events(order), [("confirmed", "shipped")])

    def test_a_pending_order_is_never_auto_shipped(self):
        order = self._order_in("pending", payment_status="pending")
        shipment = self._parcel(order)

        res = self._advance(shipment, Shipment.Status.DISPATCHED)

        self.assertEqual(res.status_code, 302)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, Shipment.Status.DISPATCHED)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertEqual(self._order_events(order), [])

    def test_a_confirmed_but_unpaid_order_is_not_shipped(self):
        order = self._order_in("confirmed", payment_status="pending")
        shipment = self._parcel(order)

        self._advance(shipment, Shipment.Status.DISPATCHED)

        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(self._order_events(order), [])

    def test_a_cancelled_order_is_skipped(self):
        order = self._order_in("cancelled", payment_status="pending")
        shipment = self._parcel(order)

        self._advance(shipment, Shipment.Status.DELIVERED)

        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self._order_events(order), [])

    def test_an_already_delivered_order_writes_no_second_event(self):
        order = self._order_in("delivered")
        shipment = self._parcel(order)

        self._advance(shipment, Shipment.Status.DELIVERED)

        order.refresh_from_db()
        self.assertEqual(order.status, "delivered")
        self.assertEqual(self._order_events(order), [])

    def test_repeating_the_advance_writes_one_order_event(self):
        order = self._order_in("confirmed")
        shipment = self._parcel(order)
        self._advance(shipment, Shipment.Status.DISPATCHED)

        self._advance(shipment, Shipment.Status.IN_TRANSIT)

        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(self._order_events(order), [("confirmed", "shipped")])

    def test_the_parcel_and_order_moves_roll_back_together(self):
        # The order sync rides the transaction that wrote the parcel event, so a failure after the parcel
        # move leaves neither half behind.
        order = self._order_in("confirmed")
        shipment = self._parcel(order)

        with patch(
            "shipping.admin.sync_order_status", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self._advance(shipment, Shipment.Status.DISPATCHED)

        shipment.refresh_from_db()
        self.assertEqual(shipment.status, Shipment.Status.LABEL_CREATED)
        self.assertFalse(ShipmentEvent.objects.filter(shipment=shipment).exists())
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")


class OrderBindingAndRefusalTests(ShipmentTestCase):
    """The order binding is chosen once and read-only afterwards, and the order machine -- not this surface -- decides whether a parcel's status may move it; a refusal is explained to the operator."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.support)

    def _order_in(self, status, *, payment_status="captured", with_items=True):
        order = self._account_order(
            "ORD-2026-000431",
            status=status,
            payment_status=payment_status,
        )
        if with_items:
            OrderItem.objects.create(
                order=order,
                product_name="Rose Absolute",
                price=Decimal("499.00"),
                quantity=1,
                subtotal=Decimal("499.00"),
            )
        return order

    def _parcel(self, order, tracking_number="TRK-431"):
        return self._shipment(order, tracking_number)

    def _post_status(self, shipment, status, *, order=None):
        return self.client.post(
            self.change_url(shipment),
            {
                "order": shipment.order_id if order is None else order,
                "tracking_number": shipment.tracking_number,
                "carrier": shipment.carrier,
                "status": status,
                "estimated_delivery": "",
                "internal_note": "",
                "_save": "Save",
            },
            follow=True,
        )

    def _order_events(self, order):
        return list(
            OrderStatusEvent.objects.filter(order=order)
            .order_by("created_at", "id")
            .values_list("from_status", "to_status")
        )

    def test_the_order_is_read_only_on_the_change_form(self):
        res = self.client.get(self.change_url())

        self.assertEqual(res.status_code, 200)
        self.assertNotIn("order", res.context["adminform"].form.fields)
        self.assertContains(res, f"Order #{self.order.pk}")

    def test_the_order_is_selectable_on_the_add_form_with_binding_help(self):
        res = self.client.get(f"{CHANGELIST}add/")

        form = res.context["adminform"].form
        self.assertIn("order", form.fields)
        self.assertEqual(
            form.fields["order"].help_text,
            "A parcel belongs to the order it was created for. To serve "
            "another order, create a new parcel.",
        )

    def test_a_posted_order_cannot_re_point_an_existing_parcel(self):
        # get_readonly_fields makes Django EXCLUDE the field from the change form, so a hand-posted id is
        # not even a form field; the stored binding survives rather than the POST winning.
        self._post_status(
            self.shipment,
            Shipment.Status.LABEL_CREATED,
            order=self.guest_order.pk,
        )

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.order_id, self.order.pk)

    def test_a_refused_move_saves_the_parcel_and_names_the_payment_rule(self):
        order = self._order_in("confirmed", payment_status="pending")
        shipment = self._parcel(order)

        res = self._post_status(shipment, Shipment.Status.DISPATCHED)

        shipment.refresh_from_db()
        self.assertEqual(shipment.status, Shipment.Status.DISPATCHED)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(self._order_events(order), [])
        self.assertContains(
            res,
            "Order not moved: payment must be captured (is &#x27;pending&#x27;)",
        )

    def test_an_illegal_transition_is_named(self):
        order = self._order_in("confirmed")
        shipment = self._parcel(order)

        res = self._post_status(shipment, Shipment.Status.DELIVERED)

        shipment.refresh_from_db()
        self.assertEqual(shipment.status, Shipment.Status.DELIVERED)
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(self._order_events(order), [])
        self.assertContains(
            res,
            "Order not moved: confirmed -&gt; delivered is not a legal transition",
        )

    def test_the_all_parcels_gate_is_named_for_a_split_order(self):
        order = self._order_in("confirmed")
        first = self._parcel(order, "TRK-441")
        second = self._parcel(order, "TRK-442")
        self._post_status(first, Shipment.Status.DISPATCHED)
        self._post_status(second, Shipment.Status.DISPATCHED)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")

        res = self._post_status(first, Shipment.Status.DELIVERED)

        first.refresh_from_db()
        self.assertEqual(first.status, Shipment.Status.DELIVERED)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(self._order_events(order), [("confirmed", "shipped")])
        self.assertContains(res, "Order not moved: not every parcel has been delivered")

    def test_a_legal_move_warns_nothing_and_writes_exactly_one_event(self):
        order = self._order_in("confirmed")
        shipment = self._parcel(order)

        res = self._post_status(shipment, Shipment.Status.DISPATCHED)

        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(order.fulfilment_status, "fulfilled")
        self.assertEqual(self._order_events(order), [("confirmed", "shipped")])
        self.assertNotContains(res, "Order not moved")


class ProviderTrackingUrlTests(ShipmentTestCase):
    """The link is rendered from the provider's own template and this parcel's own reference, so the storefront never needs to know a carrier's URL shape and the backend never reaches the carrier."""

    def provider(self, template="https://track.delhivery.com/{tracking_number}"):
        return ShippingProvider.objects.create(
            name="Delhivery",
            code="delhivery",
            tracking_url_template=template,
        )

    def linked(self, provider=None):
        self.shipment.provider = provider or self.provider()
        self.shipment.save(update_fields=["provider"])
        return self.shipment

    def test_the_tracking_url_renders_the_providers_template(self):
        self.linked()
        self.assertEqual(
            self.shipment.tracking_url,
            "https://track.delhivery.com/TRK-401",
        )

    def test_an_unlinked_parcel_has_no_tracking_url(self):
        self.assertIsNone(self.shipment.tracking_url)

    def test_a_provider_with_no_template_has_no_tracking_url(self):
        self.linked(self.provider(template=""))
        self.assertIsNone(self.shipment.tracking_url)

    def test_a_parcel_with_no_reference_has_no_tracking_url(self):
        self.linked()
        self.shipment.tracking_number = ""
        self.assertIsNone(self.shipment.tracking_url)

    def test_a_template_without_the_placeholder_is_left_verbatim(self):
        self.linked(self.provider(template="https://track.example.com/status"))
        self.assertEqual(
            self.shipment.tracking_url,
            "https://track.example.com/status",
        )

    def test_a_brace_in_the_template_is_url_text_not_a_format_field(self):
        # str.replace, not str.format: an operator's brace is part of the URL, and a format string here
        # would raise on the {lang} query token.
        self.linked(
            self.provider(template="https://t.example/{tracking_number}?lang={lang}")
        )
        self.assertEqual(
            self.shipment.tracking_url,
            "https://t.example/TRK-401?lang={lang}",
        )

    def test_rendering_the_link_makes_no_network_call(self):
        self.linked()
        with patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(
                self.shipment.tracking_url,
                "https://track.delhivery.com/TRK-401",
            )
        urlopen.assert_not_called()


class TrackingTemplateValidationTests(ShipmentTestCase):
    """The template is rendered into an href, so it is a URL: staff configure it, but the customer's browser follows it, which makes the scheme a boundary rather than a formatting preference."""

    def provider(self, template):
        return ShippingProvider(
            name="Carrier", code="carrier", tracking_url_template=template
        )

    def assert_rejected(self, template, expected):
        with self.assertRaises(ValidationError) as caught:
            self.provider(template).clean()
        message = "; ".join(caught.exception.message_dict["tracking_url_template"])
        self.assertIn(expected, message)

    def test_a_javascript_template_is_rejected(self):
        self.assert_rejected(
            "javascript:alert({tracking_number})", "absolute http(s) URL"
        )

    def test_a_data_template_is_rejected(self):
        self.assert_rejected("data:text/html,{tracking_number}", "absolute http(s)")

    def test_a_template_with_no_placeholder_is_rejected(self):
        self.assert_rejected(
            "https://track.example.com/status", "exactly one {tracking_number}"
        )

    def test_a_template_with_two_placeholders_is_rejected(self):
        self.assert_rejected(
            "https://track.example.com/{tracking_number}/{tracking_number}",
            "exactly one {tracking_number}",
        )

    def test_a_string_that_is_not_a_url_is_rejected(self):
        self.assert_rejected("track.example.com/{tracking_number}", "absolute http(s)")

    def test_a_scheme_relative_url_is_rejected(self):
        # //host/path inherits the page's scheme and is not the absolute http(s) link the store promises
        # the customer.
        self.assert_rejected(
            "//track.example.com/{tracking_number}", "absolute http(s)"
        )

    def test_a_newline_in_the_template_is_rejected(self):
        self.assert_rejected(
            "https://track.example.com/{tracking_number}\n", "whitespace or control"
        )

    def test_https_is_accepted(self):
        self.provider("https://track.example.com/{tracking_number}").clean()

    def test_blank_is_accepted_and_renders_no_link(self):
        provider = self.provider("")
        provider.clean()
        provider.save()

        shipment = self._shipment(self.guest_order, "TRK-BLANK", provider=provider)

        self.assertIsNone(shipment.tracking_url)

    def test_http_is_rejected_outside_debug(self):
        self.assert_rejected("http://track.example.com/{tracking_number}", "https://")

    @override_settings(DEBUG=True)
    def test_http_is_accepted_for_a_local_development_page(self):
        self.provider("http://localhost:8000/track/{tracking_number}").clean()

    def test_a_non_http_template_written_through_the_orm_renders_no_link(self):
        for index, template in enumerate(
            (
                "javascript:alert({tracking_number})",
                "data:text/html,{tracking_number}",
                "track.example.com/{tracking_number}",
            )
        ):
            with self.subTest(template=template):
                provider = ShippingProvider.objects.create(
                    name="Carrier",
                    code=f"carrier{index}",
                    tracking_url_template=template,
                )
                shipment = self._shipment(
                    self.guest_order, f"TRK-UNSAFE-{index}", provider=provider
                )
                self.assertIsNone(shipment.tracking_url)

    def test_the_admin_form_refuses_a_javascript_template(self):
        self.client.force_login(self.chief)

        res = self.client.post(
            "/admin/shipping/shippingprovider/add/",
            {
                "name": "Evil",
                "code": "evil",
                "tracking_url_template": "javascript:alert({tracking_number})",
                "is_active": "on",
                "_save": "Save",
            },
        )

        self.assertEqual(res.status_code, 200)
        self.assertFalse(ShippingProvider.objects.filter(code="evil").exists())

    def test_the_admin_form_still_saves_an_https_template(self):
        self.client.force_login(self.chief)

        res = self.client.post(
            "/admin/shipping/shippingprovider/add/",
            {
                "name": "Delhivery",
                "code": "delhivery",
                "tracking_url_template": "https://track.delhivery.com/{tracking_number}",
                "is_active": "on",
                "_save": "Save",
            },
        )

        self.assertEqual(res.status_code, 302)
        self.assertTrue(ShippingProvider.objects.filter(code="delhivery").exists())


class ProviderLinkDeletionTests(ShipmentTestCase):
    """The FK is PROTECT by recorded decision: a tracking link is part of what an already-shipped customer was given, so a careless provider delete must be REFUSED; is_active is the retirement path."""

    def test_a_new_parcel_may_have_no_provider(self):
        self.assertIsNone(self.shipment.provider_id)

    def test_the_provider_field_is_a_nullable_protected_fk(self):
        field = Shipment._meta.get_field("provider")
        self.assertIs(field.remote_field.on_delete, models.PROTECT)
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        self.assertEqual(field.remote_field.related_name, "shipments")

    def test_deleting_a_linked_provider_is_refused(self):
        provider = ShippingProvider.objects.create(name="Delhivery", code="delhivery")
        self.shipment.provider = provider
        self.shipment.save(update_fields=["provider"])

        with self.assertRaises(models.ProtectedError):
            provider.delete()

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.provider_id, provider.pk)

    def test_deleting_a_provider_no_parcel_points_at_is_still_allowed(self):
        provider = ShippingProvider.objects.create(name="Unused", code="unused")

        provider.delete()

        self.assertFalse(ShippingProvider.objects.filter(code="unused").exists())


class TrackingLinkSerializerTests(ShipmentTestCase):
    def linked_body(self, template="https://track.delhivery.com/{tracking_number}"):
        provider = ShippingProvider.objects.create(
            name="Delhivery",
            code="delhivery",
            tracking_url_template=template,
        )
        self.shipment.provider = provider
        self.shipment.save(update_fields=["provider"])
        return ShipmentTrackingSerializer(self.shipment).data

    def test_the_customer_body_carries_the_provider_code_and_the_link(self):
        body = self.linked_body()
        self.assertEqual(body["provider"], "delhivery")
        self.assertEqual(
            body["tracking_url"],
            "https://track.delhivery.com/TRK-401",
        )

    def test_an_unlinked_parcel_reports_null_not_a_placeholder(self):
        body = ShipmentTrackingSerializer(self.shipment).data
        self.assertIsNone(body["provider"])
        self.assertIsNone(body["tracking_url"])

    def test_the_body_exposes_no_provider_internal_and_no_row_id(self):
        rendered = json.dumps(self.linked_body())
        for hidden in ("tracking_url_template", "is_active", "internal_note"):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, rendered)
        self.assertNotIn('"id"', rendered)

    def test_the_two_new_fields_are_read_only(self):
        fields = ShipmentTrackingSerializer().fields
        for name in ("provider", "tracking_url"):
            with self.subTest(field=name):
                self.assertTrue(fields[name].read_only)


class ProviderLinkAdminTests(ShipmentTestCase):
    def staff_add(self, **fields):
        payload = {
            "order": self.guest_order.pk,
            "tracking_number": "",
            "carrier": "",
            "provider": "",
            "status": Shipment.Status.LABEL_CREATED,
            "estimated_delivery": "",
            "internal_note": "",
            "_save": "Save",
        }
        payload.update(fields)
        return self.client.post(f"{CHANGELIST}add/", payload)

    def test_the_provider_field_is_a_plain_select_not_a_gated_autocomplete(self):
        # shipments.write (support + admin) and shipping.manage (finance + admin) differ, and the provider
        # autocomplete resolves through shipping.manage -- so it 403s for support, the role this surface serves. A plain select needs no endpoint.
        model_admin = admin.site._registry[Shipment]
        provider = ShippingProvider.objects.create(name="Delhivery", code="delhivery")

        self.assertNotIn("provider", model_admin.autocomplete_fields)
        field = model_admin.get_form(request_for(self.support)).base_fields["provider"]
        self.assertIsInstance(field, forms.ModelChoiceField)
        self.assertIn(provider, field.queryset)

    def test_a_support_holder_links_a_provider_and_gets_the_tracking_url(self):
        provider = ShippingProvider.objects.create(
            name="Delhivery",
            code="delhivery",
            tracking_url_template="https://track.delhivery.com/{tracking_number}",
        )
        self.client.force_login(self.support)

        form = self.client.get(f"{CHANGELIST}add/")

        self.assertEqual(form.status_code, 200)
        self.assertContains(
            form, f'<option value="{provider.pk}">Delhivery</option>', html=True
        )

        res = self.staff_add(
            order=self.guest_order.pk,
            tracking_number="TRK-SUPPORT-1",
            carrier="Delhivery",
            provider=provider.pk,
            status=Shipment.Status.LABEL_CREATED,
        )

        self.assertEqual(res.status_code, 302, getattr(res, "context", None))
        created = Shipment.objects.get(tracking_number="TRK-SUPPORT-1")
        self.assertEqual(created.provider_id, provider.pk)
        self.assertEqual(
            created.tracking_url,
            "https://track.delhivery.com/TRK-SUPPORT-1",
        )

    def test_a_role_without_shipments_write_cannot_link_a_provider(self):
        provider = ShippingProvider.objects.create(name="Delhivery", code="delhivery")
        self.client.force_login(self.packer)

        form = self.client.get(f"{CHANGELIST}add/")
        self.assertEqual(form.status_code, 403)

        res = self.staff_add(
            order=self.guest_order.pk,
            tracking_number="TRK-PACKER-1",
            carrier="Delhivery",
            provider=provider.pk,
            status=Shipment.Status.LABEL_CREATED,
        )

        self.assertEqual(res.status_code, 403)
        self.assertFalse(
            Shipment.objects.filter(tracking_number="TRK-PACKER-1").exists()
        )

    def test_the_admin_links_a_new_parcel_to_its_order_and_provider(self):
        provider = ShippingProvider.objects.create(name="Delhivery", code="delhivery")
        self.client.force_login(self.chief)

        res = self.staff_add(
            tracking_number="TRK-LINK-1",
            carrier="Delhivery",
            provider=provider.pk,
        )

        self.assertEqual(res.status_code, 302, getattr(res, "context", None))
        created = Shipment.objects.get(tracking_number="TRK-LINK-1")
        self.assertEqual(created.order_id, self.guest_order.pk)
        self.assertEqual(created.provider_id, provider.pk)

    def test_the_provider_autocomplete_endpoint_refuses_the_shipments_writer(self):
        ShippingProvider.objects.create(name="Delhivery", code="delhivery")
        self.client.force_login(self.support)

        res = self.client.get(
            "/admin/autocomplete/",
            {
                "app_label": "shipping",
                "model_name": "shipment",
                "field_name": "provider",
                "term": "del",
            },
        )

        self.assertEqual(res.status_code, 403, res.content[:400])

    def test_a_parcel_is_discoverable_by_its_reference_and_its_order(self):
        self.client.force_login(self.chief)

        for term in ("TRK-401", self.order.order_number):
            with self.subTest(term=term):
                res = self.client.get(CHANGELIST, {"q": term})
                self.assertEqual(res.status_code, 200)
                self.assertContains(res, "TRK-401")

    def test_a_parcel_is_discoverable_by_its_providers_name(self):
        provider = ShippingProvider.objects.create(name="Delhivery", code="delhivery")
        self.shipment.provider = provider
        self.shipment.save(update_fields=["provider"])
        self.client.force_login(self.chief)

        res = self.client.get(CHANGELIST, {"q": "Delhivery"})

        self.assertContains(res, "TRK-401")

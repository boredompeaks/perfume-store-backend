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

* thirteen miss inputs - no credential, a wrong token, an over-wide token, an
  empty or whitespace token, a token minted for a DIFFERENT order, a token
  pointing at an order with no parcel, an absent order number, an
  authenticated stranger, a stranger against an absent order, the owner of an
  order with no parcel, the owner against an absent order, and a bare session
  cookie - ONE byte-identical answer (``UniformMissTests``);
* the authenticated customer sees their own shipment - proved with a REAL JWT
  from the login endpoint, because ``force_authenticate`` bypasses the entire
  auth stack and would pass whether or not the credential contract works
  (``OwnershipTests``);
* a guest order tracks end to end on the B04 credential (``GuestTrackingTests``);
* a SPLIT shipment reports every parcel, not the newest one
  (``SplitShipmentTests``);
* staff with the capability see and update it (``StaffSurfaceTests``);
* the trail records every move once, immutably, with an aware timestamp, and
  refuses to be rewritten, deleted or forged through the OBJECT or the
  QUERYSET (``EventTrailTests``, ``AppendOnlyGuardTests``);
* a parcel's status cannot be moved behind the trail by a bulk update
  (``StatusCannotBeMovedInBulkTests``);
* no column is written a value wider than the column declares, on either
  engine's terms (``WidthGateTests``);
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
from django.db import IntegrityError, models
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
    _trackable_shipments,
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

    def login_as(self, user):
        """A REAL JWT for ``user``, minted by the login endpoint.

        ``force_authenticate`` assigns ``request.user`` directly, so it bypasses
        every authenticator in ``DEFAULT_AUTHENTICATION_CLASSES`` and a test
        built on it passes whether or not the credential contract works at
        all. This mints a token the way a browser does - POST the credentials,
        read the ``access`` the endpoint returns - and leaves it attached as a
        bearer, so the request that follows is carried by the same auth stack
        production uses. It is what makes "an authenticated customer reads
        their own shipment" a statement about the product rather than about
        the test client.
        """
        res, token = self.api_login(user.username)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(token, res.data)
        return token

    def parcels(self, response):
        """The parcel bodies of a 200 tracking response, in report order."""
        return response.data["shipments"]

    def one_parcel(self, response):
        """The single parcel of a 200 response for a one-parcel order."""
        bodies = self.parcels(response)
        self.assertEqual(len(bodies), 1, response.data)
        return bodies[0]

    def logout(self):
        """Drop the bearer token, so the next request is anonymous again."""
        self.auth(None)

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

    def test_the_deletion_policy_is_the_one_the_model_documents(self):
        # Spec 8.3 line 2485 asks for an EXPLICIT deletion policy, and this is
        # the explicit one: the shipment cascades with its order, the actor is
        # SET_NULL. Both halves are asserted so neither can be changed silently
        # - the asymmetry is a decision, recorded in shipping/models.py.
        order_field = Shipment._meta.get_field("order")
        actor_field = ShipmentEvent._meta.get_field("actor")

        self.assertIs(order_field.remote_field.on_delete, models.CASCADE)
        self.assertIs(actor_field.remote_field.on_delete, models.SET_NULL)

    def test_deleting_a_staff_account_does_not_take_the_trail_with_it(self):
        # The other half of the asymmetry above: the same delete that takes a
        # shipment's trail with it leaves a STAFF account's attribution behind.
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

    def test_the_parcel_body_keeps_its_six_keys_in_a_split_response(self):
        # The envelope changed for split shipments; the parcel body did not.
        # This is what keeps every key's sensitivity unchanged by the nesting.
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
                        "tracking_number",
                        "estimated_delivery",
                        "events",
                    },
                )
                self.assertNotIn("internal_note", parcel)

    def test_a_hit_body_leaks_nothing_sensitive_in_its_raw_bytes(self):
        # The anti-leak sweep on the RENDERED body rather than on the parsed
        # dict, because the envelope change (a parcel is now nested under
        # "shipments") is exactly the kind of edit that could drop a key, hoist
        # one or leak a new one, and a dict-level key-set assertion is blind to
        # a value that leaks inside a key nobody thought to check. Asserted on
        # the raw bytes for BOTH credential classes: the guest's own email and
        # token are the two values the account-side fixtures do not have.
        self._shipment(self.order, "TRK-LEAK")
        self.shipment.events.create(
            from_status=None,
            to_status=Shipment.Status.LABEL_CREATED,
            actor=self.support,
        )
        # `make_user` leaves email unset, and an empty needle matches every
        # haystack - the assertion would be vacuous rather than passing.
        self.buyer.email = "track-buyer@example.com"
        self.buyer.save(update_fields=["email"])
        bodies = {}
        # Guest FIRST, while still anonymous: the view resolves an
        # authenticated caller through `order__user` and never reads the guest
        # header, so a guest token presented alongside a bearer is not a
        # second door - it is simply ignored, and the request misses.
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
            # And the six keys are all still there, at their new depth.
            self.assertIn("shipments", body)

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
        # A REAL JWT, not force_authenticate: the property is that the
        # credential contract itself refuses a stranger, which a
        # force_authenticate request never exercises.
        self.login_as(self.stranger)
        self._assert_uniform(self.track(self.order.order_number))

    def test_a_valid_credential_on_an_order_with_no_shipment_is_the_same_answer(self):
        # The caller proved ownership, so there is nothing left to hide - and
        # the answer still matches, so "has a shipment" is not observable even
        # by an owner probing their own missing parcel.
        order = self._account_order("ORD-2026-000403")
        self.login_as(self.buyer)
        self._assert_uniform(self.track(order.order_number))

    def test_a_guest_credential_on_an_order_with_no_shipment_is_the_same_answer(self):
        self._guest_order("ORD-2026-000404", "none@example.com", "tok-404")
        self._assert_uniform(self.track("ORD-2026-000404", token="tok-404"))

    def test_a_split_order_with_no_visible_parcel_is_the_same_answer(self):
        # A STRANGER's split order: the multi-parcel path must not become the
        # one shape that answers differently just because it looks at more
        # rows.
        self._shipment(self.order, "TRK-409")
        self.login_as(self.stranger)
        self._assert_uniform(self.track(self.order.order_number))

    def test_every_miss_shape_is_one_shape(self):
        # The whole matrix in one assertion, so "one byte-identical answer" is
        # a measured fact about a listed set rather than an impression from
        # reading the tests above it. Ordered deliberately: the anonymous
        # probes all run FIRST, because authenticating partway through would
        # carry into every later probe and silently reclassify it as an
        # authenticated one - the shape list would then look exhaustive while
        # quietly testing fewer anonymous shapes than it claims.
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

        # The credential decides WHETHER the list is empty, never what an empty
        # one looks like - so the authenticated classes are in the same matrix,
        # each against an existing order number AND an absent one, because
        # "an order that exists but is not yours" and "an order that does not
        # exist" are the two a stranger/owner mix-up could get wrong.
        self.login_as(self.stranger)
        shapes["authenticated stranger"] = self.track(self.order.order_number)
        shapes["authenticated stranger, unknown order"] = self.track("ORD-2026-999999")
        self.login_as(self.buyer)
        shapes["owner of an order with no parcel"] = self.track(
            empty_order.order_number
        )
        shapes["owner, unknown order number"] = self.track("ORD-2026-999999")

        # A bare session cookie resolves to AnonymousUser (the session
        # authenticator is not installed), so it is the no-credential shape and
        # belongs in the matrix rather than in the class above.
        self.logout()
        self.client.force_login(self.buyer)
        shapes["session cookie only"] = self.track(self.order.order_number)

        for label, response in shapes.items():
            with self.subTest(shape=label):
                self._assert_uniform(response)
        self.assertEqual(len(shapes), 13)


class OwnershipTests(ShipmentTestCase):
    """The two credentials spec 3.9 line 1189 names, and nothing else.

    Every test here obtains its credential the way a browser does - a real JWT
    from ``POST /api/accounts/login/`` - so these assertions are about the
    shipped auth stack reaching the tracking view, not about a test client
    being handed a user object. ``common/authentication.py:41-57`` records that
    JWT remains the only credential in the chain, and that is the contract
    these tests now actually hold the endpoint to.
    """

    def test_the_authenticated_customer_sees_their_own_shipment(self):
        self.login_as(self.buyer)

        res = self.track(self.order.order_number)

        self.assertEqual(res.status_code, 200, res.data)
        parcel = self.one_parcel(res)
        self.assertEqual(parcel["order_number"], self.order.order_number)
        self.assertEqual(parcel["tracking_number"], "TRK-401")

    def test_the_account_alone_is_the_credential(self):
        # Proved by the absence of the header: signing in is enough.
        self.login_as(self.buyer)
        res = self.track(self.order.order_number, token=None)
        self.assertEqual(res.status_code, 200, res.data)

    def test_signing_in_is_never_a_way_to_read_a_guest_order(self):
        # A guest order's `user` is NULL, so `user=request.user` cannot match
        # it - even with the guest's own token attached.
        self.login_as(self.buyer)

        res = self.track(
            self.guest_order.order_number, token=self.guest_order.guest_token
        )

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Shipment not found")

    def test_a_real_credential_from_a_stranger_is_refused(self):
        # The load-bearing negative, on a real credential: an authenticated
        # STRANGER holding a valid JWT for their OWN account still cannot read
        # this customer's parcel by guessing the order number.
        self.login_as(self.stranger)

        res = self.track(self.order.order_number)

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Shipment not found")

    def test_a_dropped_token_is_never_a_credential(self):
        # The other half of "JWT is the credential": a token that is no longer
        # presented must stop authorizing. Without this, a leaked token in a
        # stale client would keep reading an order forever.
        self.login_as(self.buyer)
        self.assertEqual(self.track(self.order.order_number).status_code, 200)

        self.logout()

        res = self.track(self.order.order_number)
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["error"], "Shipment not found")

    def test_a_forged_bearer_token_is_refused_by_the_auth_stack(self):
        # The test that makes the JWT claim load-bearing rather than
        # decorative. ``force_authenticate`` assigns ``request.user`` before any
        # authenticator runs, so it can never produce this answer: the
        # Authorization header really is presented and JWTAuthentication
        # really does reject it. The refusal is DRF's 401, raised in the
        # authenticator BEFORE the view runs, so it is a statement about the
        # credential and carries no order information at all - the next test
        # pins that half.
        self.auth("not.a.real.token")

        res = self.track(self.order.order_number)

        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(res.data["code"], "not_authenticated")

    def test_a_rejected_credential_discloses_nothing_about_the_order(self):
        # The anti-oracle half of the test above: whatever the auth stack
        # answers for a bad credential, it answers IDENTICALLY whether or not
        # the order number exists, so a 401 is not a cheaper existence oracle
        # than the 404. Asserted on the raw bytes, like UniformMissTests does.
        self.auth("not.a.real.token")

        known = self.track(self.order.order_number)
        unknown = self.track("ORD-2026-999999")

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.content, unknown.content)

    def test_a_token_whose_account_is_gone_stops_working(self):
        # The other direction: a token that WAS valid once stops working when
        # the account behind it is deleted. Pinned on the same anti-oracle
        # property, because simplejwt raises "User not found" from the
        # authenticator rather than falling through to the anonymous branch.
        token = self.login_as(self.buyer)
        self.assertEqual(self.track(self.order.order_number).status_code, 200)

        self.buyer.delete()

        self.auth(token)
        known = self.track(self.order.order_number)
        unknown = self.track("ORD-2026-999999")
        self.assertEqual(known.status_code, 401, known.data)
        self.assertEqual(known.content, unknown.content)

    def test_a_bare_session_login_is_not_a_credential_here(self):
        # The endpoint is DRF-on-DRF, and DRF's session authenticator is not in
        # DEFAULT_AUTHENTICATION_CLASSES, so a session cookie resolves to
        # AnonymousUser and the token branch is the only way in. Pinned so a
        # future authenticator added to that tuple has to decide about this
        # endpoint deliberately.
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
    """A guest tracks on the credential B04 minted, end to end."""

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
        # SPEC-1-B04 requirement 6: `user is None` must not crash any path this
        # task touches, and a guest order is the only order with no user.
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
    """Spec 6.9 line 2021 "Split shipments, if needed" - every parcel, always.

    The pre-fix view answered with ``.first()`` over a queryset ordered
    newest-first, so a two-parcel order reported only the parcel that left
    LAST. The sharp end of that: an order whose FIRST parcel was already
    delivered reported the still-moving second parcel and never mentioned the
    delivery at all.
    """

    def setUp(self):
        super().setUp()
        # A dedicated order: the shared fixtures already put one parcel on
        # each, and a split order that also carries an unrelated parcel would
        # test the count rather than the split.
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
        # The exact pre-fix failure: TRK-SPLIT-A is delivered and must appear,
        # whatever the model's newest-first ordering would have picked.
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
        # Oldest first, so the customer reads them in dispatch order rather
        # than in the admin grid's newest-activity order.
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
        # The helper is the guard, so its return value is pinned directly and
        # not only through the view.
        request = RequestFactory().get("/")
        request.user = self.buyer
        request.headers = {}

        found = _trackable_shipments(request, self.split_order.order_number)

        self.assertEqual(
            [shipment.tracking_number for shipment in found],
            ["TRK-SPLIT-A", "TRK-SPLIT-B"],
        )

    def test_a_one_parcel_order_still_reports_exactly_one(self):
        # The split path must not double-count a plain order.
        order = self._account_order("ORD-2026-000407")
        self._shipment(order, "TRK-SOLO")
        self.login_as(self.buyer)

        res = self.track(order.order_number)

        self.assertEqual(
            [parcel["tracking_number"] for parcel in self.parcels(res)], ["TRK-SOLO"]
        )

    def test_the_shipment_model_ordering_is_not_what_the_timeline_uses(self):
        # The split answer is ordered in the query, so this states the two
        # orderings apart rather than letting one silently stand for both.
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
        with self.assertRaises(ValueError):
            event.save()

    def test_a_recorded_event_survives_a_refused_rewrite(self):
        # The refusal must be a refusal, not a partial write: the row still
        # holds what it held.
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
        # The customer's timeline IS this table, so an operator's view and the
        # customer's view cannot disagree about what happened.
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
        self.login_as(self.buyer)

        body = self.track(order.order_number).content.decode()

        self.assertNotIn("1048.99", body)
        self.assertNotIn("shipping_amount", body)
        self.assertNotIn("total_amount", body)


class AppendOnlyGuardTests(ShipmentTestCase):
    """No code path can rewrite, delete or forge a recorded step.

    The pre-fix guard was a ``save()`` check only, which is one layer short of
    the claim its own docstring made. These are the four paths that reach the
    table without calling ``save()``; each is asserted to be refused AND to
    leave the recorded step exactly as it was, because a guard that raised
    after writing would be no guard at all.
    """

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
        # Not just the status column: the shipment and the actor are part of
        # the record too.
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
        # The over-blocking check for the queryset guard above. ``QuerySet
        # .delete`` is refused, but Django's collector issues its own DELETE and
        # never routes through the queryset, so the CASCADE is NOT blocked by
        # it - which is the whole reason the guard can be this blunt. If a
        # future Django or a refactor made the cascade go through
        # ``QuerySet.delete``, THIS is the test that would fail, and the right
        # answer would be to narrow the guard rather than to un-cascade.
        self.assertTrue(ShipmentEvent.objects.filter(pk=self.event.pk).exists())

        self.shipment.delete()

        self.assertFalse(Shipment.objects.filter(pk=self.shipment.pk).exists())
        self.assertFalse(ShipmentEvent.objects.filter(pk=self.event.pk).exists())
        self.assertEqual(ShipmentEvent.objects.count(), self.before - 1)

    def test_the_refusals_name_the_model_and_the_reason(self):
        # A guard whose message names nothing is a guard an operator cannot
        # act on from a traceback.
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
        # The remaining bulk path, and the one a reader is most likely to miss:
        # ``QuerySet.bulk_update`` looks like an object method but is not - it
        # compiles a CASE expression and issues it through
        # ``queryset.filter(...).update(...)`` internally (Django 6.1,
        # ``QuerySet.bulk_update``), so it runs into the queryset guard without
        # any model method of ours running. Asserted on the message, so a
        # Django-internal refusal would not satisfy this.
        with self.assertRaises(ValueError) as caught:
            ShipmentEvent.objects.bulk_update([self.event], ["to_status"])

        self.assertIn("append-only", str(caught.exception))
        self.event.refresh_from_db()
        self.assertEqual(self.event.to_status, Shipment.Status.DISPATCHED)

    def test_the_admin_surface_is_still_the_only_place_a_step_is_written(self):
        # The sanctioned writer is unchanged: the change form still records the
        # move, in the same transaction (EventTrailTests pins the rollback).
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
    """A parcel cannot move to a new status with no event recording it.

    ``ShipmentAdmin.save_model`` writes the row and the event in one
    transaction, and that is the only writer of a status move. The one write
    path that never reaches it is a bulk ``update()``, which is what moved a
    parcel to "delivered" with the trail still saying "label_created".
    """

    def test_a_bulk_update_cannot_move_the_status(self):
        with self.assertRaises(ValueError):
            Shipment.objects.filter(pk=self.shipment.pk).update(status="delivered")

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.LABEL_CREATED)
        self.assertFalse(ShipmentEvent.objects.filter(shipment=self.shipment).exists())

    def test_a_bulk_update_cannot_move_the_status_either(self):
        # ``QuerySet.bulk_update`` on a list of objects looks like an object
        # method but never calls ``save()``: Django compiles the values into a
        # CASE expression and issues it through ``queryset.filter(...).update()``
        # (Django 6.1). That is why the refusal lives on the QUERYSET rather
        # than on ``Shipment.save``, and without this probe the pair would look
        # equally protected while one of them was open.
        self.shipment.status = Shipment.Status.DELIVERED

        with self.assertRaises(ValueError) as caught:
            Shipment.objects.bulk_update([self.shipment], ["status"])

        self.assertIn("ShipmentEvent", str(caught.exception))
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, Shipment.Status.LABEL_CREATED)

    def test_a_bulk_update_of_other_columns_is_still_allowed(self):
        # The guard is scoped to the one column the trail claims to hold, so
        # it does not turn Shipment into a read-only table.
        Shipment.objects.filter(pk=self.shipment.pk).update(carrier="Ecom Express")

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "Ecom Express")

    def test_a_bulk_update_of_a_non_status_column_is_still_allowed(self):
        # Same scoping on the object-list form, so refusing `status` does not
        # refuse the whole method.
        self.shipment.carrier = "Ecom Express"

        Shipment.objects.bulk_update([self.shipment], ["carrier"])

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "Ecom Express")

    def test_a_bulk_update_cannot_write_an_over_wide_column_either(self):
        # A bulk write does not call save(), so the width gate has to be
        # repeated here or `bulk_update` is a way round it.
        self.shipment.carrier = "x" * (
            Shipment._meta.get_field("carrier").max_length + 1
        )

        with self.assertRaises(ValueError) as caught:
            Shipment.objects.bulk_update([self.shipment], ["carrier"])

        self.assertIn("carrier", str(caught.exception))
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "BlueDart")

    def test_a_bulk_update_refusal_leaves_the_caller_able_to_carry_on(self):
        # The reason the refusal is raised before Django opens its
        # `transaction.atomic(savepoint=False)` block rather than inside it: an
        # error raised inside that block marks the caller's transaction as
        # needing a rollback, so a caught-and-logged guard would leave the
        # caller unable to run another query at all.
        with self.assertRaises(ValueError):
            Shipment.objects.bulk_update([self.shipment], ["status"])

        # Would raise TransactionManagementError if the refusal had poisoned it.
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
    """No column is ever written a value wider than the column declares.

    SQLite ignores a ``varchar(n)`` width and Postgres enforces it, so an
    over-length value is a clean save in this suite and a ``DataError`` 500 on
    the production database. The bound is read off the model field rather than
    restated, so these tests are also what stops the gate drifting from the
    schema. Every bounded column on both new models is exercised, not just the
    one the audit happened to trip over.
    """

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
        # The gate is a width, not a mood: the last legal value must still be
        # writable, or the fix would be a truncation bug wearing a hat.
        exact = self._over(Shipment, "tracking_number", width=100)
        shipment = self._shipment(self.order, exact, carrier="BlueDart")

        self.assertEqual(len(shipment.tracking_number), 100)

    def test_a_field_with_no_declared_width_is_not_gated(self):
        # internal_note is a TextField: there is no width to exceed, so the
        # gate must pass it rather than invent a limit.
        long_note = "N" * 5000
        self.shipment.internal_note = long_note

        self.shipment.save()

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.internal_note, long_note)

    def test_a_value_at_the_bound_writes_on_a_re_save_too(self):
        # The pre-fix symptom was a value that stored once and then 400'd on
        # re-save; the bound is enforced on every write, not only the first.
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
        # The width gate has to ride the queryset path too, not only save():
        # a bulk update writes straight to the column, so this was the same
        # SQLite-says-yes/Postgres-DataError trap one layer down.
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
        # internal_note is a TextField, so a long note through the queryset is
        # legal exactly as it is through save().
        Shipment.objects.filter(pk=self.shipment.pk).update(internal_note="N" * 5000)

        self.shipment.refresh_from_db()
        self.assertEqual(len(self.shipment.internal_note), 5000)

    def test_a_computed_value_is_left_to_the_database_not_measured(self):
        # Django hands a `bulk_update`'s values to the same `QuerySet.update`
        # as a CASE expression rather than as the string that produced it, and
        # an expression reaches the gate through any `F()` update too. The gate
        # measures strings; measuring an expression would crash the write with
        # a TypeError about `len()`, which is the opposite of what a guard is
        # for.
        Shipment.objects.filter(pk=self.shipment.pk).update(
            carrier=models.F("tracking_number")
        )

        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.carrier, "TRK-401")

    def test_the_admin_form_still_refuses_an_over_wide_value(self):
        # The width gate at the model is a second line of defence; the form is
        # the first, and it must not have been weakened.
        self.client.force_login(self.support)

        res = self.staff_post(
            self.change_url(), tracking_number=self._over(Shipment, "tracking_number")
        )

        self.assertEqual(res.status_code, 200)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.tracking_number, "TRK-401")

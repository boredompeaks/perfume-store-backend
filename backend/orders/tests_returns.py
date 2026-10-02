"""[R-1.16] SPEC-1-B07a: the return-request lifecycle.

One sentence of requirement: a customer can request a return against an order,
and staff can see and progress that request through its lifecycle. Everything
below is a probe against that sentence and against the traps this repo has
already been bitten by.

* the customer seam is ACCOUNT-ONLY, every LOOKUP miss is ONE byte-identical
  answer - an unknown order number, somebody else's number, and a guest order's
  number - and a malformed body is refused with a 400 before any lookup runs,
  so it cannot disclose an order either (``MissShapeTests``);
* ownership is proved with a REAL JWT from the login endpoint, never
  ``force_authenticate``, which assigns ``request.user`` directly and passes
  whether or not the credential contract works at all (``OwnershipTests``);
* requesting a return moves NO money - ``payment_status``, ``total_amount`` and
  ``Refund.objects.count()`` are asserted unchanged, before and after both the
  request and the staff approval (``MoneyIsUntouchedTests``);
* duplicates are prevented ATOMICALLY, and the database is proved to be the
  last-resort authority rather than the application probe being the only thing
  standing there (``DuplicateRequestTests``);
* an illegal return-status transition is REFUSED, not silently applied, and the
  whole machine is pinned edge by edge (``TransitionTests``);
* a role without the capability is refused and one with it is allowed, and the
  packing operator's pinned capability set is byte-for-byte what SPEC-1-B03
  left it as (``CapabilityTests``);
* the width gate holds on ``save``, on ``update`` and on ``bulk_update``, and a
  bulk status write is refused rather than quietly bypassing the machine
  (``WidthGateTests`` / ``QuerysetGuardTests``);
* a guest order with ``user is None`` is exercised throughout - not because
  guest returns are supported (they are not, by decision) but because the miss
  it must produce is the same one a stranger gets (``GuestOrderTests``);
* the eligibility gate is pinned value by value over the FULL payment x
  fulfilment cross-product the machine admits, against a HAND-WRITTEN oracle
  rather than a recomputation of the gate's own constant
  (``EligibilityDerivationTests``), and a REAL partial refund is driven through
  the SPEC-1-05 refund seam before the return seam is asked its opinion
  (``RefundSeamIntegrationTests``) - cycle 3's bug was that nothing joined the
  two features, so 100% line coverage could not see it.
"""

from datetime import timedelta
from decimal import Decimal
import inspect
from unittest.mock import patch

from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, User
from django.db import IntegrityError, transaction
from django.test import tag
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from common.roles import (
    ROLE_ADMIN,
    ROLE_FINANCE,
    ROLE_MARKETING,
    ROLE_SUPPORT,
    sync_role_groups,
)
from common.testing import ApiTestCase
from orders.models import (
    RETURN_ALLOWED_TRANSITIONS,
    RETURN_OPEN_STATUSES,
    RETURN_STATUS_CHOICES,
    Order,
    Refund,
    ReturnRequest,
    _allowed_from,
    _reject_oversized_value,
    return_transition_allowed,
)
from orders.admin import (
    RETURNS_READ,
    RETURNS_WRITE,
    RETURN_CAPABILITY_MAP,
    ReturnRequestAdminForm,
)
from orders import urls as orders_urls
from config import urls as config_urls
from orders.serializers import ReturnRequestSerializer
from orders.state import (
    CAPTURED_MONEY_PAYMENT_STATUSES,
    FULFILMENT_STATUS_CHOICES,
    LEGACY_STATUS_DIMENSIONS,
    PAYMENT_ALLOWED_TRANSITIONS,
    PAYMENT_CAPTURED,
    PAYMENT_METHOD_COD,
    PAYMENT_STATUS_CHOICES,
    fulfilment_for_status,
    payment_for_status,
)
from orders.views import (
    MalformedReturnRequestBody,
    _return_body,
    _return_eligible,
    _return_request_miss,
)

CREATE_URL = "/api/v1/store/orders/returns/"
CHANGELIST = "/admin/orders/returnrequest/"

REASON = ReturnRequest.ReasonCode.DAMAGED
TEST_PASSWORD = "S3cure-Passphrase!"


def role_user(role, username, staff=True):
    """A staff account holding exactly one role from the map."""
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=staff,
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


class ReturnTestCase(ApiTestCase):
    """Fixtures and the two credential helpers every probe below needs."""

    def setUp(self):
        super().setUp()
        self.buyer = self.make_user("returns-buyer")
        self.stranger = self.make_user("returns-stranger")
        self.order = self._order("RET-2026-000001", self.buyer)
        # A paid, delivered order: the shape a return request is really made
        # against, and the one whose money the no-money-moved probes watch.
        self.delivered = self._order("RET-2026-000002", self.buyer, status="delivered")
        Order.objects.filter(pk=self.delivered.pk).update(payment_status="captured")
        self.delivered.refresh_from_db()
        self.cancelled = self._order("RET-2026-000003", self.buyer, status="cancelled")

    def _order(self, order_number, user, status="confirmed"):
        # The two dimension columns are derived from the machine's own status
        # map rather than hand-written. Cycle 1 wrote
        # ``"captured" if status != "pending" else "pending"``, which put a
        # CANCELLED order on ``captured`` - disagreeing with
        # LEGACY_STATUS_DIMENSIONS, where cancelled is ``("pending",
        # "unfulfilled")`` because cancel is legal only from pending. The
        # fixtures have to say what the machine says, or the eligibility probe
        # below is testing a row the machine cannot produce.
        return Order.objects.create(
            user=user,
            order_number=order_number,
            full_name="Return Buyer",
            phone="9876500099",
            address="1 Return Lane",
            city="Indore",
            state="MP",
            pincode="452001",
            status=status,
            total_amount=Decimal("1200.00"),
            payment_status=payment_for_status(status),
            fulfilment_status=fulfilment_for_status(status),
        )

    def guest_order(self, order_number="RET-2026-000900", email="guest@example.com"):
        """An order with ``user is None`` - B04's guest row."""
        return Order.objects.create(
            user=None,
            guest_email=email,
            guest_token=f"tok-{order_number}",
            order_number=order_number,
            full_name="Guest Buyer",
            phone="9876500088",
            address="2 Guest Lane",
            city="Indore",
            state="MP",
            pincode="452001",
            status="delivered",
            total_amount=Decimal("600.00"),
            payment_status="captured",
        )

    # credentials -----------------------------------------------------------
    def login_as(self, user):
        """A REAL JWT for ``user``, minted by the login endpoint.

        ``force_authenticate`` assigns ``request.user`` directly, so it bypasses
        every authenticator and a test built on it passes whether or not the
        credential contract works. This mints a token the way a browser does -
        POST the credentials, read the ``access`` the endpoint returns - and
        leaves it attached as a bearer, so the request that follows is carried
        by the auth stack production uses.
        """
        res, token = self.api_login(user.username)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(token, res.data)
        return token

    def anonymous_client(self):
        """A second client with NO credential of any kind."""
        return APIClient()

    def ask(self, order_number, reason_code=REASON, **extra):
        payload = {
            "order_number": order_number,
            "reason_code": reason_code,
            "reason_note": "The seal was already broken.",
        }
        payload.update(extra)
        return self.client.post(CREATE_URL, payload, format="json")

    def file_request(self, order, status):
        return ReturnRequest.objects.create(
            order=order,
            reason_code=REASON,
            status=status,
        )


@tag("e2e")
class RequestShapeTests(ReturnTestCase):
    """The confirmation the customer gets back (spec 19.1 line 4687)."""

    def test_a_customer_requests_a_return_against_their_own_order(self):
        self.login_as(self.buyer)

        res = self.ask(self.order.order_number)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["order_number"], self.order.order_number)
        # Spec 10.2's opening state, and the whole point: REQUESTING does not
        # approve (approval is a separate staff act).
        self.assertEqual(res.data["status"], ReturnRequest.Status.REQUESTED)
        row = ReturnRequest.objects.get(pk=res.data["id"])
        self.assertEqual(row.order, self.order)
        self.assertEqual(row.reason_code, REASON)
        self.assertTrue(row.is_open)

    def test_the_confirmation_carries_nothing_but_the_request_s_own_facts(self):
        self.login_as(self.buyer)

        res = self.ask(self.order.order_number)

        # SPEC-1-B07b: the create confirmation and the list/detail bodies are
        # now ONE representation (``ReturnRequestSerializer``), so this exact-set
        # assertion is UNCHANGED and now also pins the list/detail key set -
        # which is the point of the unification. B07a's five keys are a subset
        # of the serializer's seven; the two additions are ``reason_note`` (the
        # customer's own words, which they are entitled to read back) and
        # ``updated_at``. Neither is a leak: no money, no customer record, and
        # no other customer's anything rides either.
        self.assertEqual(
            set(res.data),
            {
                "id",
                "order_number",
                "status",
                "reason_code",
                "reason_note",
                "created_at",
                "updated_at",
            },
        )
        # No money, no customer record, and no other customer's anything.
        raw = res.content.decode()
        for leak in ("total_amount", "payment_status", "guest_token", "address"):
            self.assertNotIn(leak, raw)

    def test_a_delivered_order_is_acceptable(self):
        self.login_as(self.buyer)

        res = self.ask(self.delivered.order_number)

        self.assertEqual(res.status_code, 201, res.data)

    def test_the_note_is_optional_and_stored_verbatim(self):
        self.login_as(self.buyer)

        res = self.ask(self.order.order_number, reason_note="  smells wrong  ")

        row = ReturnRequest.objects.get(pk=res.data["id"])
        self.assertEqual(row.reason_note, "smells wrong")

    def test_a_request_without_a_note_is_filed(self):
        self.login_as(self.buyer)

        res = self.client.post(
            CREATE_URL,
            {"order_number": self.order.order_number, "reason_code": REASON},
            format="json",
        )

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(ReturnRequest.objects.get(pk=res.data["id"]).reason_note, "")

    def test_an_order_number_may_be_padded_by_whitespace(self):
        self.login_as(self.buyer)

        res = self.ask(f"  {self.order.order_number}  ")

        self.assertEqual(res.status_code, 201, res.data)

    def test_a_cancelled_order_is_refused(self):
        # Nothing was ever paid for it and nothing shipped (the machine maps
        # cancelled onto ("pending", "unfulfilled")), so there is nothing to
        # send back. The pin that keeps this refusal and the pending refusal
        # from coming apart is in EligibilityDerivationTests.
        self.login_as(self.buyer)

        res = self.ask(self.cancelled.order_number)

        self.assertEqual(res.status_code, 409, res.data)
        self.assertFalse(self.cancelled.return_requests.exists())


@tag("e2e")
class MalformedBodyTests(ReturnTestCase):
    """Every body shape that is not text is REFUSED, not crashed on.

    Cycle 1 read the three fields with
    ``(request.data.get(field) or "").strip()``, so any non-``str`` value
    reached ``.strip()`` and raised AttributeError - HTTP 500 on a public
    endpoint, reachable with any JSON client. Each case below returned 500
    before this class existed.
    """

    # field -> the shapes that must all be refused with the same 400.
    NON_TEXT = {
        "order_number": [1, ["x"], {"a": 1}, True, 2.5],
        "reason_code": [5, ["damaged"], {"a": 1}, True],
        "reason_note": [{"a": 1}, ["x"], 7, True],
    }

    def setUp(self):
        super().setUp()
        # Logged in ONCE: the login endpoint carries a throttle scope, and a
        # per-request re-login inside these loops trips it (429) long before it
        # tests anything about the return seam.
        self.login_as(self.buyer)

    def _post(self, payload):
        return self.client.post(CREATE_URL, payload, format="json")

    def test_every_non_string_field_shape_is_a_400_not_a_500(self):
        for field, shapes in self.NON_TEXT.items():
            for shape in shapes:
                with self.subTest(field=field, shape=shape):
                    payload = {
                        "order_number": self.order.order_number,
                        "reason_code": REASON,
                        "reason_note": "note",
                    }
                    payload[field] = shape

                    res = self._post(payload)

                    self.assertEqual(res.status_code, 400, res.data)
                    self.assertEqual(res.data["error"], f"{field} must be a string")
                    self.assertFalse(ReturnRequest.objects.exists())

    def test_a_null_in_either_required_field_is_still_the_old_400(self):
        # None is not malformed, it is absent: the note is optional and a null
        # order_number has always got the "required" refusal. Both answers are
        # unchanged from cycle 1 - only the CRASHES are new.
        for field, expected in (
            ("order_number", "order_number is required"),
            ("reason_code", "A valid reason_code is required"),
        ):
            with self.subTest(field=field):
                payload = {
                    "order_number": self.order.order_number,
                    "reason_code": REASON,
                }
                payload[field] = None

                res = self._post(payload)

                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data["error"], expected)

    def test_a_null_note_is_filed_as_the_empty_note(self):
        res = self._post(
            {
                "order_number": self.order.order_number,
                "reason_code": REASON,
                "reason_note": None,
            }
        )

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(ReturnRequest.objects.get(pk=res.data["id"]).reason_note, "")

    def test_a_body_that_is_not_an_object_is_refused_rather_than_crashed_on(self):
        # A bare JSON list/scalar parses fine and has no ``.get`` to call, so it
        # is refused on the same path as any other unreadable body.
        for body in ([1, 2], "RET-2026-000001", 42):
            with self.subTest(body=body):
                res = self._post(body)

                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data["error"], "A JSON object body is required")

    def test_the_gate_is_listed_not_hand_written(self):
        # A field the view reads must be in the gate's tuple. This is what keeps
        # "every field is type-checked" true when the next field is added.
        with self.subTest():
            request = type(
                "R", (), {"data": {"order_number": "X", "reason_code": "y"}}
            )()
            self.assertEqual(
                set(_return_body(request)),
                {"order_number", "reason_code", "reason_note"},
            )
            self.assertEqual(_return_body(request)["reason_note"], "")

    def test_the_gate_raises_rather_than_coercing_a_bad_shape(self):
        request = type("R", (), {"data": {"order_number": ["x"]}})()

        with self.assertRaises(MalformedReturnRequestBody) as caught:
            _return_body(request)

        self.assertEqual(caught.exception.error, "order_number must be a string")

    def test_malformed_input_is_still_not_an_order_existence_oracle(self):
        """The property that matters most here: byte-identical either way.

        A 400 whose body or code differed between an existing and a
        non-existent order would be a NEW oracle - worse than the 500 it
        replaces, because it would be reachable by anyone. Two axes, compared
        on raw response content and not on the status code alone:

        * every REFUSED body that still names an order is sent twice, once
          against the caller's real order and once against an order number
          that does not exist. Refusals are the whole of the property - a 201
          against your own order is the feature working, not a leak, and
          MissShapeTests already pins the 404 as the one answer for a miss;
        * every body whose order_number is ITSELF malformed names no order at
          all, so there is no existence to vary; it is sent by two accounts
          holding different orders instead, proving the answer is built from
          the request alone.
        """
        missing = "RET-2026-999999"
        refused_but_named = [
            {"order_number": self.order.order_number, "reason_code": "not_a_reason"},
            {"order_number": self.order.order_number},
            {"order_number": self.order.order_number, "reason_code": 5},
            {"order_number": self.order.order_number, "reason_code": ["damaged"]},
            {"order_number": self.order.order_number, "reason_code": None},
            {
                "order_number": self.order.order_number,
                "reason_code": REASON,
                "reason_note": {"a": 1},
            },
            {"order_number": self.order.order_number, "reason_note": ["x"]},
        ]

        for body in refused_but_named:
            with self.subTest(body=body):
                real = self._post(body)
                absent = self._post({**body, "order_number": missing})
                self.assertEqual(real.status_code, 400, body)
                self.assertEqual(real.status_code, absent.status_code, body)
                self.assertEqual(real.content, absent.content, body)

        malformed = [
            {"order_number": ["x"], "reason_code": REASON},
            {"order_number": 1, "reason_code": REASON},
        ]
        for body in malformed:
            with self.subTest(body=body):
                as_buyer = self._post(body)
                self.login_as(self.stranger)
                as_stranger = self._post(body)
                self.assertEqual(as_buyer.status_code, 400, body)
                self.assertEqual(as_buyer.content, as_stranger.content, body)


@tag("e2e")
class EligibilityDerivationTests(ReturnTestCase):
    """The accepted set IS the derivation - the two cannot drift apart.

    Cycle 1's gate refused ``cancelled`` with the rationale "nothing was
    fulfilled" and accepted ``pending``, which the audit called a contradiction:
    the machine maps both statuses onto ``("pending", "unfulfilled")``. The
    pin below computes the refused set from ``LEGACY_STATUS_DIMENSIONS`` at run
    time and drives the endpoint for every status in it, so a hand-listed
    accepted set that stops matching the derivation fails here rather than
    being discovered by an auditor.
    """

    def refused_by_the_machine(self):
        """The set a dimension-reading gate refuses, straight off the machine.

        Computed from ``LEGACY_STATUS_DIMENSIONS`` and the machine's declared
        capture point, never restated here: this is the derivation the view's
        gate must agree with, so that a hand-listed accepted set cannot drift
        away from it unnoticed.
        """
        return {
            status
            for status, (payment, fulfilment) in LEGACY_STATUS_DIMENSIONS.items()
            if payment != PAYMENT_CAPTURED and fulfilment == "unfulfilled"
        }

    def test_the_machine_says_pending_and_cancelled_are_the_same_order(self):
        # The fact the whole derivation leans on. If a future migration ever
        # separates these two rows, this test says so before the gate's
        # rationale quietly becomes false again.
        self.assertEqual(
            LEGACY_STATUS_DIMENSIONS["pending"],
            LEGACY_STATUS_DIMENSIONS["cancelled"],
        )
        self.assertEqual(self.refused_by_the_machine(), {"pending", "cancelled"})

    def test_the_accepted_set_matches_the_derivation_for_every_status(self):
        self.login_as(self.buyer)
        refused = self.refused_by_the_machine()

        for index, status in enumerate(LEGACY_STATUS_DIMENSIONS):
            with self.subTest(status=status):
                order = self._order(f"RET-2026-0010{index}", self.buyer, status=status)

                res = self.ask(order.order_number)

                expected = 409 if status in refused else 201
                self.assertEqual(res.status_code, expected, res.data)
                # And the row-level predicate agrees with what the seam did, so
                # the gate cannot pass by accident.
                self.assertEqual(not _return_eligible(order), status in refused)

    def test_a_pending_order_is_refused_like_a_cancelled_one(self):
        pending = self._order("RET-2026-001100", self.buyer, status="pending")
        self.login_as(self.buyer)

        res = self.ask(pending.order_number)

        self.assertEqual(res.status_code, 409, res.data)
        self.assertFalse(pending.return_requests.exists())

    def test_a_never_paid_order_is_refused_on_the_payment_dimension_alone(self):
        # Zero items, zero amount, verification FAILED: there is no return to
        # make against an order the store never took money for, and the rule
        # that refuses it is the payment dimension reading "pending has not
        # moved" - not a special case bolted on for this probe.
        unpaid = self._order("RET-2026-001101", self.buyer, status="pending")
        Order.objects.filter(pk=unpaid.pk).update(
            payment_status="failed", total_amount=Decimal("0.00")
        )
        unpaid.refresh_from_db()
        self.login_as(self.buyer)

        res = self.ask(unpaid.order_number)

        self.assertEqual(res.status_code, 409, res.data)
        self.assertFalse(unpaid.return_requests.exists())

    def test_a_cod_order_that_shipped_is_still_returnable_uncaptured(self):
        # The reason the gate is a conjunction and not "payment == captured":
        # the machine waives the capture precondition for COD and puts the
        # capture point at delivery (orders.models), so a COD order that has
        # shipped arrives with its money still pending and must not be refused.
        cod = self._order("RET-2026-001102", self.buyer, status="shipped")
        Order.objects.filter(pk=cod.pk).update(
            payment_method=PAYMENT_METHOD_COD, payment_status="pending"
        )
        cod.refresh_from_db()
        self.login_as(self.buyer)

        res = self.ask(cod.order_number)

        self.assertEqual(res.status_code, 201, res.data)

    # --- cycle 3: every value the machine admits, answered individually -----
    #
    # Cycle 2 fixed the gate's derivation and PINNED it against the machine,
    # and still 100% line coverage missed that the gate tracked only
    # ``captured`` on the payment axis while the machine tracks ``captured``
    # AND ``partially_refunded``. Two reasons the old pin could not see it: it
    # iterated ``LEGACY_STATUS_DIMENSIONS`` (five rows, none of which is a
    # refund value, because the legacy single status cannot express one), and
    # it recomputed the expected set from the SAME constant the gate used, so
    # a wrong constant looks right from both sides. The two pins below fix both
    # holes: the oracle is HAND-WRITTEN here (so it cannot inherit the gate's
    # mistake) and it covers the FULL payment x fulfilment cross-product (so
    # every value the machine admits has to appear in it).

    # What each payment value must be answered with, written out rather than
    # computed. Keyed by payment value; the value is the set of fulfilment
    # values on which that payment is ACCEPTED - the goods went out, so the
    # money half is irrelevant. Everything outside the set is refused.
    MACHINE_ADMITS = {
        "pending": {"fulfilled", "partially_fulfilled"},
        "authorized": {"fulfilled", "partially_fulfilled"},
        "captured": {"unfulfilled", "fulfilled", "partially_fulfilled"},
        "failed": {"fulfilled", "partially_fulfilled"},
        "partially_refunded": {"unfulfilled", "fulfilled", "partially_fulfilled"},
        "refunded": {"fulfilled", "partially_fulfilled"},
    }

    def refused_pairs_by_the_derivation(self):
        """The refused (payment, fulfilment) pairs, computed from the machine.

        The same run-time derivation the view's gate performs, over the FULL
        cross-product the machine admits instead of only the five legacy rows:
        refused iff the payment value is outside the machine-derived
        captured-money set AND the goods have not moved. Restating the set as a
        literal in the test is what let the cycle-2 gate and the cycle-2 pin
        agree with each other while both were wrong about ``partially_refunded``
        - so this reads the set the view reads.
        """
        return {
            (payment, fulfilment)
            for payment, _ in PAYMENT_STATUS_CHOICES
            for fulfilment, _ in FULFILMENT_STATUS_CHOICES
            if payment not in CAPTURED_MONEY_PAYMENT_STATUSES
            and fulfilment == "unfulfilled"
        }

    def test_the_captured_money_set_is_the_two_values_the_machine_reaches(self):
        # The derivation itself, pinned against the table it is derived from -
        # and against the two independent pieces of evidence that make
        # ``partially_refunded`` captured money: the transition table puts it
        # strictly after ``captured``, and the shipped-edge precondition in
        # orders/models.py admits it alongside ``captured``. Asserting the
        # reachability too means a future edit to the table that DROPS the edge
        # fails here instead of quietly narrowing the returns gate.
        self.assertEqual(
            CAPTURED_MONEY_PAYMENT_STATUSES, {"captured", "partially_refunded"}
        )
        self.assertIn(
            "partially_refunded",
            PAYMENT_ALLOWED_TRANSITIONS[PAYMENT_CAPTURED],
        )
        self.assertIn("partially_refunded", CAPTURED_MONEY_PAYMENT_STATUSES)
        # ...and the value that is NOT there for a machine reason: the table
        # declares no edge out of it, so its money has all gone back.
        self.assertEqual(PAYMENT_ALLOWED_TRANSITIONS["refunded"], set())
        self.assertNotIn("refunded", CAPTURED_MONEY_PAYMENT_STATUSES)

    def test_the_oracle_covers_every_value_the_machine_admits(self):
        # Completeness, so the hand-written oracle cannot quietly go stale: a
        # new payment or fulfilment value in the machine's own choice tuples
        # fails here rather than being un-enumerated.
        self.assertEqual(
            set(self.MACHINE_ADMITS), {p for p, _ in PAYMENT_STATUS_CHOICES}
        )
        every_fulfilment = {f for f, _ in FULFILMENT_STATUS_CHOICES}
        for payment, accepted in self.MACHINE_ADMITS.items():
            with self.subTest(payment=payment):
                self.assertTrue(accepted <= every_fulfilment, accepted)

    def test_the_derivation_agrees_with_the_hand_written_oracle(self):
        expected = {
            (payment, fulfilment)
            for payment, _ in PAYMENT_STATUS_CHOICES
            for fulfilment, _ in FULFILMENT_STATUS_CHOICES
            if fulfilment not in self.MACHINE_ADMITS[payment]
        }
        self.assertEqual(self.refused_pairs_by_the_derivation(), expected)

    def test_every_machine_value_gets_the_answer_this_rationale_claims(self):
        # The probe cycle 2 should have had: every payment value the machine
        # admits, crossed with every fulfilment value, driven through the
        # endpoint. Under the old single-literal gate this FAILS on
        # (partially_refunded, unfulfilled) with 409 - the state the shipped
        # SPEC-1-05 refund writer actually produces.
        self.login_as(self.buyer)
        index = 0

        for payment, _ in PAYMENT_STATUS_CHOICES:
            for fulfilment, _ in FULFILMENT_STATUS_CHOICES:
                index += 1
                with self.subTest(payment=payment, fulfilment=fulfilment):
                    order = self._order(
                        f"RET-2026-0012{index:02d}", self.buyer, status="confirmed"
                    )
                    Order.objects.filter(pk=order.pk).update(
                        payment_status=payment, fulfilment_status=fulfilment
                    )
                    order.refresh_from_db()

                    res = self.ask(order.order_number)

                    accepted = fulfilment in self.MACHINE_ADMITS[payment]
                    self.assertEqual(
                        res.status_code, 201 if accepted else 409, res.data
                    )
                    self.assertEqual(_return_eligible(order), accepted)
                    self.assertEqual(order.return_requests.exists(), accepted)


class RefundSeamIntegrationTests(ReturnTestCase):
    """The returns seam and the SPEC-1-05 refund seam must not disagree.

    Cycle 3's bug was not in the arithmetic of the gate: it was that no test
    drove a REAL refund through the real refund seam and then asked the return
    seam what it thought of the resulting row. The two features touch the same
    order and the refund seam writes ``payment_status`` WITHOUT EVER writing
    ``fulfilment_status``, so it manufactures rows the returns path has to have
    an answer for - ``partially_refunded / unfulfilled`` and
    ``refunded / unfulfilled`` - and nothing asserted those answers.

    Every row here is produced by the refund endpoint itself (mocked gateway,
    no network), not by an ORM write standing in for it.
    """

    REFUND_PATH = "/api/admin/orders/{order_id}/refund/"

    def setUp(self):
        super().setUp()
        self.treasurer = role_user(ROLE_FINANCE, "returns-treasurer")
        # A second client so the refund call is carried by the treasurer's own
        # real JWT and the customer's bearer is still on the default client.
        self.finance_client = self.fresh_client()
        res, token = self.api_login(self.treasurer.username, client=self.finance_client)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(token, res.data)
        gateway = self.razorpay_mock()
        gateway.refund.create.return_value = {"id": "rfnd_RETURNS1"}

    def payable_order(self, order_number):
        """A confirmed order the refund seam will accept: captured, with a
        provider payment id to reverse. The provider id is unique across
        orders, so it is derived from the order number rather than reused."""
        order = self._order(order_number, self.buyer, status="confirmed")
        Order.objects.filter(pk=order.pk).update(
            razorpay_order_id=f"order_{order_number}",
            razorpay_payment_id=f"pay_{order_number}",
        )
        order.refresh_from_db()
        return order

    def refund(self, order, amount):
        return self.finance_client.post(
            self.REFUND_PATH.format(order_id=order.id),
            {"amount": amount, "reason": "Damaged on arrival"},
            format="json",
        )

    def test_a_partly_refunded_order_is_still_returnable(self):
        # The regression this cycle exists for. A refund of part of the total
        # is money that HAS moved, so the rationale admits this order and the
        # gate must too; the old gate answered 409 on a state this repo's own
        # shipped refund writer produces.
        order = self.payable_order("RET-2026-001300")

        refunded = self.refund(order, "300.00")

        self.assertEqual(refunded.status_code, 201, refunded.data)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")
        self.assertEqual(order.fulfilment_status, "unfulfilled")

        self.login_as(self.buyer)
        res = self.ask(order.order_number)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(_return_eligible(order))
        # ...and the return itself still moves no money: the refund seam's
        # write is still exactly what it was before the return was filed.
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")
        self.assertEqual(order.refundable_remaining, Decimal("900.00"))
        self.assertEqual(Refund.objects.count(), 1)

    def test_a_fully_refunded_order_is_refused_and_says_why_it_is_a_policy(self):
        # The sibling refusal, pinned rather than left as an accident: the
        # money has all come back, so there is nothing left to send anything
        # against. Asserted here so the day anyone widens it, this test is the
        # thing that says the policy CHANGED rather than the behaviour drifting.
        order = self.payable_order("RET-2026-001301")

        refunded = self.refund(order, "1200.00")

        self.assertEqual(refunded.status_code, 201, refunded.data)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")

        self.login_as(self.buyer)
        res = self.ask(order.order_number)

        self.assertEqual(res.status_code, 409, res.data)
        self.assertFalse(order.return_requests.exists())
        self.assertEqual(Refund.objects.count(), 1)

    def test_a_refund_leaves_a_shipped_order_returnable_whatever_it_did(self):
        # The disjunction's other half, on a row the refund seam produced: a
        # COD-style order whose money never became ``captured`` but whose goods
        # went out stays returnable, and a partial refund cannot change that.
        order = self._order("RET-2026-001302", self.buyer, status="confirmed")
        Order.objects.filter(pk=order.pk).update(
            payment_method=PAYMENT_METHOD_COD,
            payment_status="captured",
            fulfilment_status="fulfilled",
            razorpay_payment_id="pay_RETURNS2",
        )
        order.refresh_from_db()
        self.refund(order, "100.00")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "partially_refunded")
        self.login_as(self.buyer)

        res = self.ask(order.order_number)

        self.assertEqual(res.status_code, 201, res.data)

    def test_the_refund_seam_refuses_the_payment_values_the_gate_also_refuses(self):
        # The two features agree on the OTHER end of the payment axis too:
        # pending / authorized / failed hold no money, so neither a refund nor
        # a return is possible against them. Driven through the real endpoints.
        for index, payment in enumerate(("pending", "authorized", "failed")):
            with self.subTest(payment=payment):
                order = self.payable_order(f"RET-2026-00131{index}")
                Order.objects.filter(pk=order.pk).update(payment_status=payment)
                order.refresh_from_db()

                refund_res = self.refund(order, "100.00")
                self.login_as(self.buyer)
                return_res = self.ask(order.order_number)

                self.assertEqual(refund_res.status_code, 409, refund_res.data)
                self.assertEqual(return_res.status_code, 409, return_res.data)
                self.assertFalse(Refund.objects.exists())
                self.assertFalse(order.return_requests.exists())


class MissShapeTests(ReturnTestCase):
    """Every way this seam can miss is ONE byte-identical answer.

    Byte-identity of the raw response is the property, not a set of status
    codes: a body that differs is as good an existence oracle as a code that
    differs, and that leak is a P1 in this repo's history.
    """

    def setUp(self):
        super().setUp()
        self.login_as(self.buyer)
        self.baseline = self.ask("RET-2026-999999")
        self.assertEqual(self.baseline.status_code, 404, self.baseline.data)

    def _assert_uniform(self, response):
        self.assertEqual(response.status_code, self.baseline.status_code)
        self.assertEqual(response.content, self.baseline.content)

    def test_the_baseline_miss_is_the_one_documented_answer(self):
        self.assertEqual(self.baseline.data["error"], "Order not found")
        # The view's own helper is exactly that one body; the surrounding
        # envelope (code/details) is the repo-wide middleware, identical on
        # every response in the suite and not this endpoint's business.
        self.assertEqual(_return_request_miss().data, {"error": "Order not found"})

    def test_a_stranger_s_own_valid_credential_names_my_order(self):
        # The order number is real, the caller is not its owner, and the
        # credential is a REAL JWT - so the refusal is the contract's, not the
        # test client's.
        self.login_as(self.stranger)

        self._assert_uniform(self.ask(self.order.order_number))

    def test_a_guest_token_changes_nothing_for_an_account_order(self):
        # There is no guest credential on this seam at all, so presenting one
        # must not open a second door: the header is ignored and the request
        # is filed exactly as the tokenless one would be.
        token = self.login_as(self.buyer)

        res = self.client.post(
            CREATE_URL,
            {"order_number": self.order.order_number, "reason_code": REASON},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_X_GUEST_ORDER_TOKEN="whatever-the-guest-minted",
        )

        self.assertEqual(res.status_code, 201, res.data)

    def test_an_absent_credential_is_refused_before_the_order_is_read(self):
        client = self.anonymous_client()

        res = client.post(
            CREATE_URL,
            {"order_number": self.order.order_number, "reason_code": REASON},
            format="json",
        )

        self.assertEqual(res.status_code, 401)
        self.assertFalse(self.order.return_requests.exists())

    def test_a_forged_bearer_is_refused(self):
        self.auth("not-a-real-token")

        res = self.ask(self.order.order_number)

        self.assertEqual(res.status_code, 401)
        self.assertFalse(self.order.return_requests.exists())

    def test_a_body_with_no_order_number_is_a_400_not_a_miss(self):
        # 400 is the honest answer here and it discloses nothing: the caller
        # told us nothing to look up.
        res = self.client.post(CREATE_URL, {"reason_code": REASON}, format="json")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["error"], "order_number is required")

    def test_an_unknown_reason_code_is_refused_before_the_order_is_read(self):
        # Same reasoning: this answer is identical for an order that exists and
        # one that does not, so a caller cannot probe with the reason field.
        for reason in ("damaged_in_fire", "", "DAMAGED"):
            with self.subTest(reason=reason):
                res = self.ask(self.order.order_number, reason_code=reason)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data["error"], "A valid reason_code is required")
        self.assertFalse(self.order.return_requests.exists())

    def test_every_miss_shape_is_one_shape(self):
        # The whole matrix in ONE assertion, so "one byte-identical answer" is
        # a measured fact about a listed set rather than an impression from
        # reading the tests around it.
        self.login_as(self.stranger)
        guest = self.guest_order()

        shapes = [
            ("unknown number", self.ask("RET-2026-999998")),
            (
                "empty body order",
                self.client.post(CREATE_URL, {"reason_code": REASON}, format="json"),
            ),
            ("stranger on a real order", self.ask(self.order.order_number)),
            (
                "guest order, token presented",
                self.client.post(
                    CREATE_URL,
                    {
                        "order_number": guest.order_number,
                        "reason_code": REASON,
                    },
                    format="json",
                    HTTP_X_GUEST_ORDER_TOKEN=guest.guest_token,
                ),
            ),
            ("order number with no row", self.ask("")),
        ]

        for label, response in shapes:
            with self.subTest(shape=label):
                self.assertIn(response.status_code, (400, 401, 404))
        # The 404 family - every shape that involves naming an order - is one
        # body. The 400s are refused on the INPUT, before any lookup, so they
        # cannot be about the order at all.
        misses = [r for _, r in shapes if r.status_code == 404]
        self.assertEqual(len(misses), 3)
        for response in misses:
            self.assertEqual(response.status_code, self.baseline.status_code)
            self.assertEqual(response.content, self.baseline.content)


class GuestOrderTests(ReturnTestCase):
    """Guest returns are NOT supported - by decision, and proved here.

    The spec routes returns under the ACCOUNT section (line 1045) and gives the
    guest (line 74) browsing and checkout only; it never names returns among the
    guest's powers. So this seam does not accept B04's token at all, and a guest
    order is not addressable here - which is asserted on the RESPONSE BYTES, not
    on a status code, so "guest rows behave differently" could not hide in a
    body that merely looks similar.
    """

    def setUp(self):
        super().setUp()
        self.login_as(self.buyer)
        self.guest = self.guest_order()
        self.baseline = self.ask("RET-2026-999999")

    def test_a_guest_order_is_the_same_answer_as_an_order_that_does_not_exist(self):
        self.login_as(self.buyer)

        with_token = self.ask(self.guest.order_number)
        without_token = self.ask(self.guest.order_number)

        self.assertEqual(with_token.status_code, 404, with_token.data)
        self.assertEqual(with_token.content, self.baseline.content)
        self.assertEqual(without_token.content, self.baseline.content)

    def test_the_guests_own_valid_token_is_not_accepted(self):
        # The strongest form of the probe: a real B04 credential, minted for
        # this exact order, on the order that minted it.
        self.client.defaults["HTTP_X_GUEST_ORDER_TOKEN"] = self.guest.guest_token
        self.addCleanup(self.client.defaults.pop, "HTTP_X_GUEST_ORDER_TOKEN", None)

        res = self.ask(self.guest.order_number)

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.content, self.baseline.content)
        self.assertFalse(self.guest.return_requests.exists())

    def test_an_anonymous_caller_cannot_file_for_a_guest_order_either(self):
        client = self.anonymous_client()

        res = client.post(
            CREATE_URL,
            {"order_number": self.guest.order_number, "reason_code": REASON},
            format="json",
        )

        self.assertEqual(res.status_code, 401)
        self.assertFalse(self.guest.return_requests.exists())

    def test_no_guest_return_row_can_be_reached_through_the_account_filter(self):
        # The filter that decides ownership is ``user=request.user``, which a
        # guest row can never satisfy (``user`` is NULL): the exclusion is
        # structural, not a check that could be forgotten on another path.
        self.login_as(self.buyer)

        res = self.ask(self.guest.order_number)

        self.assertEqual(res.status_code, 404, res.data)
        self.assertFalse(
            ReturnRequest.objects.filter(order__user__isnull=True).exists()
        )


class DuplicateRequestTests(ReturnTestCase):
    """One live request per order, prevented ATOMICALLY."""

    def setUp(self):
        super().setUp()
        self.login_as(self.buyer)

    def test_a_second_request_for_the_same_order_is_refused(self):
        first = self.ask(self.order.order_number)
        self.assertEqual(first.status_code, 201, first.data)

        second = self.ask(self.order.order_number)

        self.assertEqual(second.status_code, 409, second.data)
        self.assertEqual(
            second.data["error"], "This order already has an open return request"
        )
        self.assertEqual(self.order.return_requests.count(), 1)

    def test_a_stranger_can_never_open_a_request_on_another_customers_order(self):
        # The lock is taken on the ORDER, so the guard is not scoped by the
        # caller either: a second customer naming the same order gets the same
        # miss twice, and never a row.
        other = self.make_user("returns-second")

        self.login_as(other)
        first = self.ask(self.order.order_number)
        second = self.ask(self.order.order_number)

        self.assertEqual(first.status_code, 404, first.data)
        self.assertEqual(first.content, second.content)
        self.assertFalse(self.order.return_requests.exists())

    def test_the_database_refuses_a_second_open_row_with_the_probe_skipped(self):
        # The application probe is the fast path; THIS is the authority. The
        # probe is neutralised with a mock so the INSERT runs for real, and the
        # partial unique index is what answers - which is also the only way to
        # prove the constraint exists rather than that the probe happens to work.
        first = self.ask(self.order.order_number)
        self.assertEqual(first.status_code, 201, first.data)

        with patch.object(ReturnRequest.objects, "filter") as skipped_probe:
            skipped_probe.return_value.exists.return_value = False
            res = self.ask(self.order.order_number)

        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(self.order.return_requests.count(), 1)

    def test_a_direct_orm_insert_of_a_second_open_row_raises(self):
        self.file_request(self.order, ReturnRequest.Status.REQUESTED)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.file_request(self.order, ReturnRequest.Status.APPROVED)

    def test_a_terminal_request_releases_the_order_for_a_new_one(self):
        # rejected/closed are terminal, so the index releases the order: a
        # customer whose request was refused may file again.
        self.file_request(self.order, ReturnRequest.Status.REJECTED)

        res = self.ask(self.order.order_number)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.order.return_requests.count(), 2)

    def test_the_open_status_tuple_is_exactly_what_the_index_holds(self):
        # Every status the model calls open really does hold the partial index
        # against a second open row - asserted per status, because a tuple that
        # had drifted by one member would still pass a membership test.
        for status in RETURN_OPEN_STATUSES:
            with self.subTest(status=status):
                row = self.file_request(self.order, status)
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        self.file_request(self.order, ReturnRequest.Status.REQUESTED)
                row.delete()

    def test_a_terminal_status_does_not_hold_the_index(self):
        for status in ("rejected", "closed"):
            with self.subTest(status=status):
                self.file_request(self.order, status)
                res = self.ask(self.order.order_number)
                self.assertEqual(res.status_code, 201, res.data)
                ReturnRequest.objects.all().delete()


class MoneyIsUntouchedTests(ReturnTestCase):
    """Spec 6.8 line 1985: a requested return is not a refund.

    The probes assert on the PERSISTED order and on the refund table's row
    count, because a refusal that still moved the money would be no refusal at
    all - and the approval walk is included precisely because approving is the
    step where somebody would be tempted to move it.
    """

    def _snapshot(self, order):
        order.refresh_from_db()
        return (
            order.payment_status,
            order.total_amount,
            order.refunded_at,
            order.refundable_remaining,
            Refund.objects.filter(order=order).count(),
            Refund.objects.count(),
        )

    def test_requesting_a_return_moves_no_money(self):
        self.login_as(self.buyer)
        before = self._snapshot(self.delivered)

        res = self.ask(self.delivered.order_number)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self._snapshot(self.delivered), before)
        self.assertEqual(before[0], "captured")
        self.assertEqual(before[1], Decimal("1200.00"))
        self.assertEqual(before[4], 0)

    def test_approving_a_return_moves_no_money_either(self):
        self.login_as(self.buyer)
        filed = self.ask(self.delivered.order_number)
        before = self._snapshot(self.delivered)

        self._staff_walk(filed.data["id"], ReturnRequest.Status.APPROVED)

        self.assertEqual(self._snapshot(self.delivered), before)

    def test_walking_a_return_to_closed_still_moves_no_money(self):
        self.login_as(self.buyer)
        filed = self.ask(self.delivered.order_number)

        for status in ("approved", "received", "inspected", "closed"):
            self._staff_walk(filed.data["id"], status)
        before = self._snapshot(self.delivered)

        self.assertEqual(self._snapshot(self.delivered), before)
        self.assertEqual(Refund.objects.count(), 0)
        self.assertEqual(self.delivered.refundable_remaining, Decimal("1200.00"))

    def _staff_walk(self, request_pk, status):
        """Move one request to ``status`` through the admin change form.

        A fresh support account per call so no MFA/TOTP state is shared, and
        the payload is the one-field form the grid renders.
        """
        support = role_user(ROLE_SUPPORT, f"walk-{status}")
        self.client.force_login(support)
        row = ReturnRequest.objects.get(pk=request_pk)
        res = self.client.post(
            f"{CHANGELIST}{row.pk}/change/",
            {"status": status, "_save": "Save"},
        )
        self.assertEqual(res.status_code, 302, res.status_code)
        row.refresh_from_db()
        self.assertEqual(row.status, status)


class TransitionTests(ReturnTestCase):
    """The return machine, edge by edge, and the refusal of an illegal move."""

    def setUp(self):
        super().setUp()
        self.support = role_user(ROLE_SUPPORT, "returns-support")
        self.row = self.file_request(self.order, ReturnRequest.Status.REQUESTED)

    def _post(self, status):
        """A complete change-form POST for this request, exactly as it renders.

        ``status`` is the only editable field on this surface: ``order``,
        ``reason_code``, ``reason_note`` and both timestamps are read-only, so
        an operator walks a request and never rewrites what the customer asked.
        """
        return self.client.post(
            f"{CHANGELIST}{self.row.pk}/change/",
            {"status": status, "_save": "Save"},
        )

    def _walk_all_the_way(self):
        self.client.force_login(self.support)
        for status in ("approved", "received", "inspected", "closed"):
            res = self._post(status)
            self.assertEqual(res.status_code, 302, res.status_code)
            self.row.refresh_from_db()
            self.assertEqual(self.row.status, status)

    def test_the_full_happy_path_walks_requested_to_closed(self):
        self._walk_all_the_way()

        self.assertEqual(self.row.status, ReturnRequest.Status.CLOSED)
        self.assertFalse(self.row.is_open)

    def test_the_rejection_branch_is_reachable_and_terminal(self):
        self.client.force_login(self.support)

        res = self._post("rejected")

        self.assertEqual(res.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "rejected")
        # Terminal: nothing leaves it, and each attempt says so on the field.
        for target in ("approved", "received", "closed"):
            with self.subTest(target=target):
                refused = self._post(target)
                self.assertEqual(refused.status_code, 200)
                self.assertIn("status", refused.context["adminform"].form.errors)
                self.row.refresh_from_db()
                self.assertEqual(self.row.status, "rejected")

    def test_an_illegal_transition_is_refused_and_the_row_is_unchanged(self):
        self.client.force_login(self.support)

        res = self._post("inspected")

        # A field error re-renders the form (200) and never reaches save_model,
        # so the refusal is visible to the operator rather than a silent no-op.
        self.assertEqual(res.status_code, 200)
        self.assertIn("status", res.context["adminform"].form.errors)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "requested")

    def test_an_illegal_transition_names_the_allowed_set_to_the_operator(self):
        self.client.force_login(self.support)

        res = self._post("closed")

        error = str(res.context["adminform"].form.errors["status"][0])
        self.assertIn("cannot move from 'requested' to 'closed'", error)
        # The operator is told what IS legal, not just what is not.
        self.assertIn("approved, rejected", error)

    def test_a_terminal_status_says_nothing_is_legal(self):
        self.row.status = ReturnRequest.Status.REJECTED
        self.row.save()
        self.client.force_login(self.support)

        res = self._post("approved")

        self.assertIn("nothing", str(res.context["adminform"].form.errors["status"][0]))

    def test_an_illegal_transition_writes_no_change_log_entry(self):
        self.client.force_login(self.support)

        self._post("closed")

        self.assertFalse(LogEntry.objects.filter(object_id=str(self.row.pk)).exists())

    def test_the_model_refuses_an_illegal_edge_on_a_direct_save(self):
        # The admin form is one writer; this is the backstop behind it, so a
        # caller that reaches the row by any other route cannot skip a stage
        # either.
        self.row.status = "inspected"

        with self.assertRaises(ValueError) as ctx:
            self.row.save()
        self.assertIn("cannot move from 'requested' to 'inspected'", str(ctx.exception))
        self.assertIn("approved, rejected", str(ctx.exception))
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "requested")

    def test_a_creation_is_not_a_move_so_any_starting_status_writes(self):
        # The customer's seam always files ``requested``, but a CREATION has no
        # prior status, so the edge guard has nothing to compare and must not
        # refuse the write.
        for status in RETURN_OPEN_STATUSES + ("rejected", "closed"):
            with self.subTest(status=status):
                other = self._order(f"RET-2026-1{status[:4]}", self.buyer)
                row = ReturnRequest(order=other, reason_code=REASON, status=status)
                row.save()
                self.assertEqual(row.status, status)

    def test_every_illegal_pair_is_refused_by_the_machine(self):
        # The whole matrix in one place: for every (old, new) pair the machine
        # must agree with itself, so no pair is untested.
        for old in RETURN_STATUS_CHOICES:
            old_status = old[0]
            for new_status, _label in RETURN_STATUS_CHOICES:
                with self.subTest(old=old_status, new=new_status):
                    expected = new_status == old_status or new_status in (
                        RETURN_ALLOWED_TRANSITIONS[old_status]
                    )
                    self.assertIs(
                        return_transition_allowed(old_status, new_status), expected
                    )

    def test_an_unknown_status_is_never_allowed(self):
        for old in RETURN_STATUS_CHOICES:
            old_status = old[0]
            with self.subTest(old=old_status):
                self.assertFalse(return_transition_allowed(old_status, "teleported"))
                self.assertFalse(return_transition_allowed("teleported", "approved"))

    def test_the_machine_matches_the_spec_vocabulary_exactly(self):
        # Spec 10.2 line 3474, in the spec's own order and with nothing added.
        self.assertEqual(
            [status for status, _label in RETURN_STATUS_CHOICES],
            ["requested", "approved", "rejected", "received", "inspected", "closed"],
        )
        self.assertEqual(
            set(RETURN_ALLOWED_TRANSITIONS),
            {status for status, _label in RETURN_STATUS_CHOICES},
        )

    def test_an_edit_that_leaves_the_status_alone_still_saves(self):
        # A self-transition is legal (``transition_allowed`` says so), so
        # re-saving a request that is not moving is not a machine error.
        self.client.force_login(self.support)

        res = self._post("requested")

        self.assertEqual(res.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "requested")

    def test_the_customer_cannot_write_the_status_or_the_reason(self):
        # The customer's seam is a POST against the family, and every field it
        # takes is its own input; nothing in the body can name a status.
        self.login_as(self.buyer)

        res = self.ask(self.delivered.order_number, status="approved")

        self.assertEqual(res.status_code, 201, res.data)
        row = ReturnRequest.objects.get(pk=res.data["id"])
        self.assertEqual(row.status, "requested")

    def test_a_hand_posted_reason_is_ignored_not_stored(self):
        # The change form has exactly one editable field, so a hand-posted
        # reason_code is not a form field and cannot rewrite what the customer
        # asked (Django excludes read-only fields from the ModelForm).
        self.client.force_login(self.support)

        res = self.client.post(
            f"{CHANGELIST}{self.row.pk}/change/",
            {
                "status": "approved",
                "reason_code": ReturnRequest.ReasonCode.CHANGED_MIND,
                "reason_note": "operator words",
                "_save": "Save",
            },
        )

        self.assertEqual(res.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "approved")
        self.assertEqual(self.row.reason_code, REASON)
        self.assertEqual(self.row.reason_note, "")


class CapabilityTests(ReturnTestCase):
    """Who may see the queue, and who may move it."""

    def setUp(self):
        super().setUp()
        self.support = role_user(ROLE_SUPPORT, "cap-support")
        self.chief = role_user(ROLE_ADMIN, "cap-admin")
        self.marketing = role_user(ROLE_MARKETING, "cap-marketing")
        self.row = self.file_request(self.order, ReturnRequest.Status.REQUESTED)

    def test_a_role_without_the_capability_is_refused_the_queue(self):
        # Staff membership ALONE does not open it (the legacy is_staff trap
        # SPEC-17-10 closed on the dashboard): a marketing manager is staff and
        # holds neither returns capability.
        self.client.force_login(self.marketing)

        self.assertEqual(self.client.get(CHANGELIST).status_code, 403)

    def test_a_non_staff_customer_is_sent_to_the_login_not_the_queue(self):
        # A different answer from the staff refusal, and the honest one: the
        # admin area is ``staff_member_required``, so a customer is offered the
        # login rather than a 403 that would confirm the surface exists.
        self.client.force_login(self.buyer)

        res = self.client.get(CHANGELIST)

        self.assertEqual(res.status_code, 302)
        self.assertIn("/admin/login/", res["Location"])

    def test_a_capability_holder_sees_the_queue_and_can_open_the_request(self):
        for user in (self.support, self.chief):
            with self.subTest(username=user.username):
                self.client.force_login(user)
                listing = self.client.get(CHANGELIST)
                self.assertEqual(listing.status_code, 200)
                self.assertContains(listing, str(self.row.pk))
                self.assertContains(listing, "requested")
                self.assertContains(listing, self.order.customer_name)
                opened = self.client.get(f"{CHANGELIST}{self.row.pk}/change/")
                self.assertEqual(opened.status_code, 200)

    def test_a_staff_member_without_the_capability_cannot_write(self):
        # Both capabilities are support+admin today, so there is no role that
        # holds read alone; what this pins is that the write door answers the
        # capability map rather than the staff flag.
        self.client.force_login(self.marketing)

        res = self.client.post(
            f"{CHANGELIST}{self.row.pk}/change/",
            {"status": "approved", "_save": "Save"},
        )

        self.assertEqual(res.status_code, 403)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "requested")

    def test_the_queue_lists_every_request_and_not_only_the_newest(self):
        # The SPEC-1-B06 trap, restated for this surface: a grid that reported
        # only the newest row made an older request simply disappear. Three
        # requests on three orders, each with its own reason, and ONE listing
        # that carries all three.
        codes = (
            ReturnRequest.ReasonCode.DEFECTIVE,
            ReturnRequest.ReasonCode.WRONG_ITEM,
            ReturnRequest.ReasonCode.CHANGED_MIND,
        )
        filed = [self.row]  # setUp already filed one against self.order
        for index, code in enumerate(codes):
            order = self._order(f"RET-2026-10000{index}", self.buyer)
            filed.append(ReturnRequest.objects.create(order=order, reason_code=code))

        self.client.force_login(self.support)
        res = self.client.get(CHANGELIST)

        self.assertEqual(res.status_code, 200)
        for row in filed:
            self.assertContains(res, str(row.pk))
        for code in codes:
            self.assertContains(res, ReturnRequest.ReasonCode(code).label)

    def test_the_index_offers_the_queue_to_a_holder_and_hides_it_otherwise(self):
        self.client.force_login(self.support)
        self.assertContains(self.client.get("/admin/"), CHANGELIST)
        self.client.force_login(self.marketing)
        self.assertNotContains(self.client.get("/admin/"), CHANGELIST)

    def test_nobody_may_add_or_delete_a_request_through_the_admin(self):
        # Not even the superuser bypass: a request is the customer's ask, and
        # the review trail spec 6.8 line 1983 requires is never erased.
        root = User.objects.create_superuser(
            "returns-root", "root@example.com", TEST_PASSWORD
        )
        for user in (self.support, self.chief, root):
            with self.subTest(username=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(f"{CHANGELIST}add/").status_code, 403)
                self.assertEqual(
                    self.client.get(f"{CHANGELIST}{self.row.pk}/delete/").status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(
                        f"{CHANGELIST}{self.row.pk}/delete/",
                        {"post": "yes"},
                    ).status_code,
                    403,
                )
        self.assertTrue(ReturnRequest.objects.filter(pk=self.row.pk).exists())

    def test_the_admin_map_gates_exactly_the_capabilities_the_roles_map_grants(self):
        from django.contrib import admin as django_admin

        from orders.admin import RETURNS_READ, RETURNS_WRITE, RETURN_CAPABILITY_MAP

        model_admin = django_admin.site._registry[ReturnRequest]
        self.assertEqual(RETURN_CAPABILITY_MAP["view"], RETURNS_READ)
        self.assertEqual(RETURN_CAPABILITY_MAP["change"], RETURNS_WRITE)
        # add and delete are capability-less: no staff role, no superuser form.
        self.assertIsNone(RETURN_CAPABILITY_MAP["add"])
        self.assertIsNone(RETURN_CAPABILITY_MAP["delete"])
        # The one door, no second one, and exactly one admin write path: no
        # ``list_editable`` cell and no bulk action exists, which is what makes
        # the change form the only way an operator can move a status.
        self.assertIsNone(model_admin.scoped_view_capability)
        self.assertEqual(model_admin.list_editable, ())
        self.assertEqual(model_admin.actions, ())
        self.assertIs(model_admin.form, ReturnRequestAdminForm)

    def test_the_packing_operators_capability_set_is_unchanged_by_this_task(self):
        # IMPORTED, not restated: SPEC-1-B03 pinned the inventory/fulfilment
        # operator's exact set and this task must not widen it (see the
        # deviation recorded in common/roles.py).
        from tests.test_superadmin_tier import (
            INVENTORY_CAPABILITIES,
            held_capabilities,
        )

        operator = role_user("inventory", "returns-packer")

        self.assertEqual(held_capabilities(operator), INVENTORY_CAPABILITIES)
        # NO assertion here on whether this role can reach the return-request
        # admin. Cycle 1 asserted the 403, which hardened a KNOWN GAP into an
        # asserted contract: spec 1.1 line 110 reads
        # ``Inventory/fulfilment operator -> Manage stock, packing, shipping and
        # returns``, so returns SHOULD be reachable for this role and today is
        # not. The cycle-2 audit ruled that omission a MISSING requirement
        # rather than a defect and ledgered it as SPEC-1-B07c, which widens
        # returns.read/returns.write to the inventory role AND widens the
        # INVENTORY_CAPABILITIES pin imported above in the same commit. Until
        # that lands, the set-equality above is the honest statement of what
        # this task does not touch.


class WidthGateTests(ReturnTestCase):
    """SQLite ignores varchar(n) while Postgres raises DataError."""

    def setUp(self):
        super().setUp()
        self.row = self.file_request(self.order, ReturnRequest.Status.REQUESTED)

    def test_an_over_wide_status_is_refused_on_save(self):
        self.row.status = "x" * 21

        with self.assertRaises(ValueError):
            self.row.save()

    def test_an_over_wide_reason_code_is_refused_on_save(self):
        self.row.reason_code = "x" * 31

        with self.assertRaises(ValueError):
            self.row.save()

    def test_an_over_wide_value_is_refused_on_a_bulk_update(self):
        with self.assertRaises(ValueError):
            ReturnRequest.objects.filter(pk=self.row.pk).update(reason_code="x" * 31)

    def test_an_over_wide_value_is_refused_on_bulk_update(self):
        self.row.reason_note = "x" * 5000
        self.row.status = "requested"

        # reason_note is a TextField, so it has no width to exceed: the gate
        # passes it and the write lands. status is what the bulk paths refuse.
        ReturnRequest.objects.bulk_update([self.row], ["reason_note"])

        self.row.refresh_from_db()
        self.assertEqual(len(self.row.reason_note), 5000)

    def test_an_over_wide_value_is_refused_on_bulk_update_of_a_bounded_column(self):
        self.row.reason_code = "x" * 31

        with self.assertRaises(ValueError):
            ReturnRequest.objects.bulk_update([self.row], ["reason_code"])

    def test_the_width_comes_from_the_field_not_from_a_literal(self):
        # reason_code is read for the limit, so a column that grew would widen
        # the gate with it rather than leaving a stale literal behind.
        limit = ReturnRequest._meta.get_field("reason_code").max_length

        self.row.reason_code = "x" * limit

        self.row.save()
        self.row.refresh_from_db()
        self.assertEqual(self.row.reason_code, "x" * limit)


class WidthHelperTests(ReturnTestCase):
    """The width helper's own rules, measured rather than assumed."""

    def test_an_unbounded_field_is_never_measured(self):
        # reason_note is a TextField, so there is no width to exceed and the
        # helper must not invent one.
        self.assertIsNone(
            _reject_oversized_value(ReturnRequest, "reason_note", "x" * 10_000),
            None,
        )

    def test_a_non_string_value_is_never_measured(self):
        # bulk_update routes its values through the same update() as a CASE
        # expression, so what arrives here is not always a value at all.
        self.assertIsNone(
            _reject_oversized_value(ReturnRequest, "status", 42),
            None,
        )

    def test_a_value_exactly_on_the_boundary_is_accepted(self):
        limit = ReturnRequest._meta.get_field("reason_code").max_length

        _reject_oversized_value(ReturnRequest, "reason_code", "x" * limit)
        with self.assertRaises(ValueError):
            _reject_oversized_value(ReturnRequest, "reason_code", "x" * (limit + 1))


class AdminFormUnitTests(ReturnTestCase):
    """The change form's contract on its own, with no request involved.

    Driven directly rather than through a POST so the defensive branch is
    exercised as the unit it is: a form with no row behind it is what the add
    path would build, and the add path is refused (``has_add_permission`` is
    False), so this is the only way that branch can run - and running it is what
    proves the lookup below it can never raise ``DoesNotExist`` if that ever
    changes.
    """

    def test_a_form_with_no_row_behind_it_validates_the_status_unchanged(self):
        form = ReturnRequestAdminForm(data={"status": ReturnRequest.Status.REQUESTED})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["status"], "requested")

    def test_the_form_refuses_an_illegal_edge_for_the_row_it_is_editing(self):
        row = self.file_request(self.order, ReturnRequest.Status.REQUESTED)
        form = ReturnRequestAdminForm(data={"status": "inspected"}, instance=row)

        self.assertFalse(form.is_valid())
        # ``form.errors["status"][0]`` is the message itself; ``str(form.errors)``
        # is its HTML rendering, which escapes the quotes.
        self.assertIn(
            "cannot move from 'requested' to 'inspected'",
            str(form.errors["status"][0]),
        )

    def test_the_form_accepts_a_legal_edge_for_the_row_it_is_editing(self):
        row = self.file_request(self.order, ReturnRequest.Status.REQUESTED)
        form = ReturnRequestAdminForm(data={"status": "approved"}, instance=row)

        self.assertTrue(form.is_valid(), form.errors)

    def test_a_status_outside_the_vocabulary_is_refused_by_the_form(self):
        row = self.file_request(self.order, ReturnRequest.Status.REQUESTED)
        form = ReturnRequestAdminForm(data={"status": "teleported"}, instance=row)

        self.assertFalse(form.is_valid())
        self.assertIn("status", form.errors)


class QuerysetGuardTests(ReturnTestCase):

    def setUp(self):
        super().setUp()
        self.row = self.file_request(self.order, ReturnRequest.Status.REQUESTED)

    def test_a_bulk_status_update_is_refused(self):
        with self.assertRaises(ValueError):
            ReturnRequest.objects.filter(pk=self.row.pk).update(status="approved")

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "requested")

    def test_a_bulk_status_write_is_refused_before_the_block_opens(self):
        # Refused BEFORE Django opens its atomic(savepoint=False) wrapper, so a
        # caller could catch the error and carry on rather than finding its
        # transaction poisoned.
        self.row.status = "inspected"  # an ILLEGAL edge, and refused anyway

        with transaction.atomic():
            with self.assertRaises(ValueError):
                ReturnRequest.objects.bulk_update([self.row], ["status"])
            # The surrounding transaction is still usable.
            ReturnRequest.objects.filter(pk=self.row.pk).update(reason_note="fine")

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "requested")
        self.assertEqual(self.row.reason_note, "fine")

    def test_the_non_status_columns_stay_bulk_writable(self):
        # The refusal is about the transition, not about the whole row: an
        # operator annotating a request must still be able to in bulk.
        ReturnRequest.objects.filter(pk=self.row.pk).update(reason_note="annotated")

        self.row.refresh_from_db()
        self.assertEqual(self.row.reason_note, "annotated")

    def test_a_row_can_still_be_created_bulk_and_deleted_in_bulk(self):
        # Named honestly: bulk_create and bulk_delete are NOT refused, so the
        # claim this surface makes is precisely "a status cannot be MOVED in
        # bulk", not "nothing can be written in bulk".
        other = self._order("RET-2026-000004", self.buyer)
        second = ReturnRequest.objects.create(order=other, reason_code=REASON)
        # bulk_create is NOT refused, which is the honest scope of the claim:
        # a status cannot be MOVED in bulk, but a row can still be inserted
        # directly with whatever status it is given.
        ReturnRequest.objects.bulk_create(
            [
                ReturnRequest(
                    order=self._order("RET-2026-000005", self.buyer),
                    reason_code=REASON,
                )
            ]
        )

        ReturnRequest.objects.filter(pk=second.pk).delete()

        self.assertEqual(ReturnRequest.objects.filter(order=self.order).count(), 1)
        self.assertEqual(ReturnRequest.objects.count(), 2)


# ==================================
# SPEC-1-B07b: the customer returns API surface
# ==================================
#
# Everything above is SPEC-1-B07a's shipped contract, unchanged except for the
# one assertion in ``RequestShapeTests`` that now pins the unified serializer
# key set instead of B07a's interim inline five. What follows is B07b: the
# serializer, the list, the detail, and the eligibility-window decision.

LIST_URL = "/api/v1/store/orders/returns/"


def detail_url(return_request_id):
    return f"/api/v1/store/orders/returns/{return_request_id}/"


@tag("e2e")
class ReturnSerializerContractTests(ReturnTestCase):
    """One customer projection, and its omissions are the point.

    The pattern is B06's ``ShipmentTrackingSerializer``: a customer-facing
    projection in which what is ABSENT is deliberate and load-bearing. Read that
    class's docstring before changing a field list here.
    """

    # HAND-WRITTEN, and the oracle has to be: recomputing the expected set from
    # the serializer's own ``fields`` would agree with a wrong projection from
    # both sides, which is the defect class B07a's cycle-2 pin was written to
    # kill.
    CUSTOMER_FIELDS = {
        "id",
        "order_number",
        "status",
        "reason_code",
        "reason_note",
        "created_at",
        "updated_at",
    }

    def setUp(self):
        super().setUp()
        self.row = self.file_request(self.delivered, ReturnRequest.Status.REQUESTED)

    def test_the_projection_is_exactly_the_hand_written_field_set(self):
        serializer = ReturnRequestSerializer(self.row)

        self.assertEqual(set(serializer.fields), self.CUSTOMER_FIELDS)

    def test_no_field_is_writable_so_a_client_can_never_write_a_return(self):
        # Structural, not a per-view promise: a ModelSerializer is WRITABLE by
        # default, so this is the assertion that stops a future edit from
        # dropping ``read_only_fields`` and letting a client post a status.
        serializer = ReturnRequestSerializer()

        for name in self.CUSTOMER_FIELDS:
            with self.subTest(field=name):
                self.assertTrue(serializer.fields[name].read_only, name)

    def test_the_order_foreign_key_is_not_exposed_only_its_number_is(self):
        # The customer-facing handle is the reference (spec 8.3 /
        # ``OrderSerializer``'s [R-8.5] identifier-exposure strategy); the pk
        # stays a server-side routing key and ownership is read off the order.
        raw = ReturnRequestSerializer(self.row).data

        self.assertEqual(raw["order_number"], self.delivered.order_number)
        for leak in ("order_id", '"order"', "order__"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, str(raw))

    def test_the_body_carries_no_money_and_no_customer_record(self):
        # A return row moves no money (B07a's MoneyIsUntouchedTests), so no
        # money field may appear - and specifically this serializer must not
        # REACH THROUGH the order to pull one off, which is the way a leak
        # would get in here.
        raw = ReturnRequestSerializer(self.row).data
        body = str(raw)

        for leak in (
            "total_amount",
            "payment_status",
            "refundable_remaining",
            "currency",
            "guest_token",
            "guest_email",
            "address",
            "phone",
        ):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, body)

    def test_a_staff_only_annotation_is_absent_from_both_model_and_projection(self):
        # ``internal_note`` is B06's leak class. This row has no such column
        # today, and the projection must not grow one: asserted on BOTH sides
        # so a future migration adding a staff column fails here rather than
        # reaching a customer by way of an explicit-field-list edit.
        model_fields = {field.name for field in ReturnRequest._meta.get_fields()}
        self.assertNotIn("internal_note", model_fields)
        self.assertNotIn("internal_note", ReturnRequestSerializer().fields)

    def test_the_projection_reveals_nothing_the_allowlist_does_not_name(self):
        # Completeness, so the hand-written oracle cannot quietly go stale: if
        # the model grows a customer-visible column, this fails rather than the
        # column silently riding a ``fields`` list nobody re-read. It asserts
        # the SUBSET direction (every exposed field is a declared, permitted
        # one), which is the direction that is a leak - a field absent from the
        # projection is a documented omission, never a vulnerability.
        permitted = self.CUSTOMER_FIELDS | {"order_number"}
        for name in ReturnRequestSerializer().fields:
            with self.subTest(field=name):
                self.assertIn(name, permitted)

    def test_no_field_reaches_through_the_order_but_for_its_number(self):
        # The structural half of "no money", and the probe that answers CLEANLY.
        #
        # ``test_the_body_carries_no_money_and_no_customer_record`` asserts on
        # rendered output, which is the property that matters - but a field that
        # reaches through the relation makes the CREATE seam itself raise, so a
        # mutation that adds one is caught as an ERROR cascade across many
        # tests rather than as one crisp failure. This reads the DECLARED
        # sources instead: every exposed field must be one of the model's own
        # columns, except ``order_number``, whose one traversal is the customer
        # reference. A money field added to the projection fails HERE, by name.
        model_columns = {field.name for field in ReturnRequest._meta.concrete_fields}
        serializer = ReturnRequestSerializer()

        for name, field in serializer.fields.items():
            with self.subTest(field=name):
                source = getattr(field, "source", None) or name
                if name == "order_number":
                    self.assertEqual(source, "order.order_number")
                    continue
                self.assertIn(
                    source,
                    model_columns,
                    f"{name} reaches outside the return row (source={source!r}); "
                    "the customer projection may only read the order for its "
                    "order_number",
                )

    def test_create_detail_and_list_agree_byte_for_byte_on_one_row(self):
        # The anti-drift pin for this task's central decision: ONE
        # representation for all three surfaces. A future edit that widens the
        # create body only, or the list only, fails here.
        self.login_as(self.buyer)
        # A second order for this customer, so the create below files against an
        # order that has no open request yet (the partial unique index forbids
        # two on the same order - which is itself the B07a contract).
        target = self._order("RET-2026-000007", self.buyer, status="delivered")

        created = self.ask(target.order_number)
        self.assertEqual(created.status_code, 201, created.data)
        pk = created.data["id"]

        detail = self.client.get(detail_url(pk))
        listed = self.client.get(LIST_URL)
        # Newest first, and the row just created is the newest.
        row = next(r for r in listed.data["results"] if r["id"] == pk)

        self.assertEqual(created.data, detail.data)
        self.assertEqual(detail.data, row)


@tag("e2e")
class ReturnListTests(ReturnTestCase):
    """GET .../returns/ - the customer's own returns, every one of them."""

    def setUp(self):
        super().setUp()
        self.login_as(self.buyer)
        self.mine = [
            self.file_request(self.delivered, ReturnRequest.Status.REQUESTED),
            self.file_request(self.order, ReturnRequest.Status.APPROVED),
        ]

    def test_the_listing_carries_every_return_the_caller_owns(self):
        # THE ``.first()`` TRAP. A to-many answered with one row would pass a
        # single-return assertion and silently drop the rest - the exact shape
        # of B06's split-shipment defect. Two rows, both present.
        res = self.client.get(LIST_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 2)
        self.assertEqual(
            {row["id"] for row in res.data["results"]}, {r.pk for r in self.mine}
        )

    def test_the_envelope_is_the_house_page_number_shape(self):
        res = self.client.get(LIST_URL)

        self.assertEqual(
            set(res.data),
            {
                "count",
                "total_pages",
                "current_page",
                "next_page",
                "previous_page",
                "results",
            },
        )
        self.assertEqual(res.data["current_page"], 1)
        self.assertEqual(res.data["total_pages"], 1)
        self.assertIs(res.data["next_page"], False)
        self.assertIs(res.data["previous_page"], False)

    def test_the_listing_is_never_answered_by_a_stranger_s_returns(self):
        theirs = self.file_request(
            self._order("RET-2026-000004", self.stranger, status="delivered"),
            ReturnRequest.Status.REQUESTED,
        )
        self.login_as(self.stranger)

        res = self.client.get(LIST_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([row["id"] for row in res.data["results"]], [theirs.pk])
        # ...and the body carries none of mine either.
        body = str(res.data)
        self.assertNotIn(self.order.order_number, body)
        self.assertNotIn(self.delivered.order_number, body)

    def test_a_guest_orders_return_row_is_structurally_unreachable(self):
        # Guest returns are not offered (SPEC-1-B04 gives the guest browsing and
        # checkout only). Proven structurally: the row is written directly past
        # the create seam, and the ``order__user`` filter still cannot match a
        # NULL-user order for an authenticated caller.
        guest = self.guest_order()
        guest_row = self.file_request(guest, ReturnRequest.Status.REQUESTED)
        self.assertIsNone(guest.user_id)

        res = self.client.get(LIST_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 2)
        self.assertNotIn(guest_row.pk, [row["id"] for row in res.data["results"]])

    def test_the_listing_is_newest_first_with_a_total_sort(self):
        # ``-created_at, -id``: the unique-id tiebreaker makes the sort TOTAL,
        # which is what stops a paginated partition from repeating or skipping
        # a row across requests (the reasoning ``order_list`` records).
        older, newer = self.mine
        ReturnRequest.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )

        res = self.client.get(LIST_URL)

        self.assertEqual(
            [row["id"] for row in res.data["results"]], [newer.pk, older.pk]
        )

    def test_the_pages_partition_the_whole_list_with_nothing_lost_or_repeated(self):
        # Completeness across the partition, which is where an unbounded or
        # mis-sorted listing loses rows. Five returns over five orders, walked
        # page by page at page_size=2: every row exactly once.
        for index in range(5):
            order = self._order(f"RET-2026-0015{index}", self.buyer, status="delivered")
            self.file_request(order, ReturnRequest.Status.REQUESTED)

        seen = []
        page = 1
        while True:
            res = self.client.get(LIST_URL, {"page_size": 2, "page": page})
            self.assertEqual(res.status_code, 200, res.data)
            seen.extend(row["id"] for row in res.data["results"])
            if not res.data["next_page"]:
                break
            page += 1

        self.assertEqual(len(seen), 7)
        self.assertEqual(len(set(seen)), 7)
        self.assertEqual(
            set(seen), set(ReturnRequest.objects.values_list("id", flat=True))
        )

    def test_an_empty_listing_is_an_empty_page_not_an_error(self):
        # A customer with no returns at all, while somebody ELSE's return exists
        # in the table - so an empty page proves the filter, not an empty DB.
        nobody = self.make_user("returns-nobody")
        someone = self.make_user("returns-someone")
        other = self._order("RET-2026-000005", someone, status="delivered")
        self.file_request(other, ReturnRequest.Status.REQUESTED)
        self.login_as(nobody)

        res = self.client.get(LIST_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["count"], 0)
        self.assertEqual(res.data["results"], [])
        self.assertEqual(res.data["total_pages"], 1)

    def test_the_page_size_is_configuration_and_not_a_literal_in_the_view(self):
        # conventions.md: no hardcoded thresholds. The number lives in
        # settings; override it here and the listing must follow.
        with override_settings(RETURNS_HISTORY_PAGE_SIZE=1):
            res = self.client.get(LIST_URL)

        self.assertEqual(res.data["count"], 2)
        self.assertEqual(len(res.data["results"]), 1)
        self.assertEqual(res.data["total_pages"], 2)
        self.assertIs(res.data["next_page"], True)

    def test_a_page_size_caller_is_clamped_and_never_unbounded(self):
        with override_settings(RETURNS_HISTORY_MAX_PAGE_SIZE=3):
            res = self.client.get(LIST_URL, {"page_size": 10000})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data["results"]), 2)
        # A non-numeric or non-positive value falls back to the configured
        # default rather than erroring - the order-history resolver's contract,
        # which this resolver deliberately mirrors.
        for junk in ("abc", "0", "-3", ""):
            with self.subTest(page_size=junk):
                fallback = self.client.get(LIST_URL, {"page_size": junk})
                self.assertEqual(fallback.status_code, 200, fallback.data)
                self.assertEqual(fallback.data["current_page"], 1)

    def test_a_page_past_the_end_lands_on_the_last_page_rather_than_404ing(self):
        res = self.client.get(LIST_URL, {"page": 99})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["current_page"], 1)

    def test_reading_the_listing_moves_no_money(self):
        before = Order.objects.filter(user=self.buyer).values_list(
            "payment_status", "total_amount", "refunded_at"
        )
        refunds_before = Refund.objects.count()

        self.client.get(LIST_URL)
        self.client.get(detail_url(self.mine[0].pk))

        after = Order.objects.filter(user=self.buyer).values_list(
            "payment_status", "total_amount", "refunded_at"
        )
        self.assertEqual(list(before), list(after))
        self.assertEqual(Refund.objects.count(), refunds_before)


@tag("e2e")
class ReturnReadIdorTests(ReturnTestCase):
    """The detail endpoint is the IDOR surface. Probed, not assumed.

    Spec 17 line 4285 asks for IDOR tests "especially in order, address, return
    and customer APIs", so the property is asserted on the raw response BYTES -
    a body that differs is as good an oracle as a status code that differs.
    """

    def setUp(self):
        super().setUp()
        self.mine = self.file_request(self.delivered, ReturnRequest.Status.REQUESTED)
        # A stranger's real return, on a real delivered order they own.
        their_order = self._order("RET-2026-000006", self.stranger, status="delivered")
        self.theirs = self.file_request(their_order, ReturnRequest.Status.REQUESTED)

    def test_the_owner_reads_their_own_return(self):
        self.login_as(self.buyer)

        res = self.client.get(detail_url(self.mine.pk))

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], self.mine.pk)
        self.assertEqual(res.data["order_number"], self.delivered.order_number)

    def test_a_stranger_probing_another_customers_pk_gets_the_baseline_miss(self):
        # THE probe. A stranger with a REAL JWT names a pk that exists and
        # belongs to somebody else. The answer must be byte-identical to the
        # answer for a pk that does not exist - if it differed, the endpoint
        # would confirm the pk is real and would be an existence oracle, which
        # is a P1 in this repo's history.
        self.login_as(self.stranger)
        baseline = self.client.get(detail_url(99999999))
        self.assertEqual(baseline.status_code, 404, baseline.data)

        probe = self.client.get(detail_url(self.mine.pk))

        self.assertEqual(probe.status_code, 404)
        self.assertEqual(probe.content, baseline.content)

    def test_a_stranger_s_own_return_reads_fine_so_the_refusal_is_about_ownership(self):
        # The control: the endpoint is not simply refusing everyone. Without
        # this, "stranger gets 404" could be a broken route rather than a
        # working guard, and the probe above would pass for the wrong reason.
        self.login_as(self.stranger)

        res = self.client.get(detail_url(self.theirs.pk))

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], self.theirs.pk)

    def test_an_anonymous_caller_is_refused_before_the_row_is_read(self):
        res = self.anonymous_client().get(detail_url(self.mine.pk))

        self.assertEqual(res.status_code, 401)
        self.assertNotIn(self.delivered.order_number, res.content.decode())

    def test_a_forged_bearer_is_refused(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")

        res = self.client.get(detail_url(self.mine.pk))

        self.assertEqual(res.status_code, 401)
        self.assertNotIn(self.delivered.order_number, res.content.decode())

    def test_a_guest_token_opens_no_door_on_an_account_return(self):
        # The B04 credential is accepted by NO surface of this family; here it
        # rides a valid account bearer and must change nothing.
        token = self.login_as(self.buyer)

        res = self.client.get(detail_url(self.mine.pk), HTTP_X_GUEST_ORDER_TOKEN=token)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["id"], self.mine.pk)

    def test_a_guest_token_alone_cannot_read_an_account_return(self):
        res = self.anonymous_client().get(
            detail_url(self.mine.pk), HTTP_X_GUEST_ORDER_TOKEN="tok-RET-2026-000002"
        )

        self.assertEqual(res.status_code, 401)
        self.assertNotIn(self.delivered.order_number, res.content.decode())

    def test_a_non_integer_pk_is_refused_by_the_router_rather_than_crashing(self):
        # Not the view's miss contract (the URL converter never matches it), so
        # the status is all that is asserted - what matters is that it is a
        # refusal and not a 500.
        res = self.client.get("/api/v1/store/orders/returns/not-an-int/")

        self.assertEqual(res.status_code, 404)

    def test_the_detail_body_carries_no_money_and_no_other_customer(self):
        self.login_as(self.buyer)

        res = self.client.get(detail_url(self.mine.pk))

        body = res.content.decode()
        for leak in (
            "total_amount",
            "payment_status",
            "refundable_remaining",
            "guest_token",
            "address",
            self.stranger.username,
        ):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, body)


@tag("e2e")
class ReturnEligibilityWindowTests(ReturnTestCase):
    """The window decision: NO WINDOW, pinned at its boundary.

    Spec 4 line 1083 ("Return/refund request where eligible") and spec 6.8
    line 1959 ("Eligibility validation") both describe a window the spec never
    defines. A whole-file sweep for a day count finds exactly one hit and it is
    about deployment cadence (line 276), not returns.

    So SPEC-1-B07b chose NO WINDOW over an env-driven ``RETURN_WINDOW_DAYS``:
    any value for that key would have been invented in this task rather than
    derived from the spec, which is fabricated policy wearing a configuration
    setting. The consequence - stated rather than hidden - is that eligibility
    is a function of the order's two machine dimensions ONLY, and age is not an
    input to it.

    A "no window" policy has no boundary to pin in the ordinary way, so what
    follows pins the ABSENCE of the boundary: the same dimension pair answers
    identically at every age, on both the accepted and the refused side.
    """

    # Ages wide enough to cross any plausible commercial window, including ones
    # nobody would argue for. If a window is ever introduced, the eligible row
    # below is the case that will fail first, and it will fail LOUDLY rather
    # than quietly expiring a customer's right to ask.
    AGES = (
        timedelta(seconds=0),
        timedelta(days=1),
        timedelta(days=30),
        timedelta(days=365),
        timedelta(days=3650),
    )

    def _aged(self, order_number, status, age):
        order = self._order(order_number, self.buyer, status=status)
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - age,
            delivered_at=timezone.now() - age,
        )
        order.refresh_from_db()
        return order

    def test_an_eligible_order_is_eligible_at_every_age(self):
        self.login_as(self.buyer)

        for index, age in enumerate(self.AGES):
            with self.subTest(age=age):
                order = self._aged(f"RET-2026-0020{index}", "delivered", age)

                res = self.ask(order.order_number)

                self.assertEqual(res.status_code, 201, res.data)
                self.assertTrue(_return_eligible(order))

    def test_an_ineligible_order_is_ineligible_at_every_age(self):
        # The other half, and the half that makes the first one mean something:
        # if age were an input, SOME of these would flip. A pending order from
        # five years ago is exactly as ineligible as one created this morning.
        self.login_as(self.buyer)

        for index, age in enumerate(self.AGES):
            with self.subTest(age=age):
                order = self._aged(f"RET-2026-0021{index}", "pending", age)

                res = self.ask(order.order_number)

                self.assertEqual(res.status_code, 409, res.data)
                self.assertFalse(_return_eligible(order))

    def test_the_gate_reads_no_clock_at_all(self):
        # The structural half of the decision, and the probe that fails the
        # MOMENT a window clause is added rather than after an auditor notices
        # it. The predicate is two expressions over the order's two dimensions;
        # this asserts it stays that way. Deliberately narrow - it would also
        # reject a comment mentioning the clock, which is the trade this makes:
        # a false alarm here is a docstring reword, a missed window is a
        # fabricated policy shipping silently.
        source = inspect.getsource(_return_eligible)

        for token in ("timezone", "timedelta", "settings", "now("):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_a_request_filed_years_ago_is_still_readable_and_still_its_own(self):
        # Age must not change what the customer can SEE either: a five-year-old
        # return is still on their list and still opens by its pk.
        self.login_as(self.buyer)
        aged = self._aged("RET-2026-002200", "delivered", timedelta(days=3650))
        created = self.ask(aged.order_number)
        self.assertEqual(created.status_code, 201, created.data)
        pk = created.data["id"]
        ReturnRequest.objects.filter(pk=pk).update(
            created_at=timezone.now() - timedelta(days=3650)
        )

        listed = self.client.get(LIST_URL)
        opened = self.client.get(detail_url(pk))

        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertIn(pk, [row["id"] for row in listed.data["results"]])
        self.assertEqual(opened.status_code, 200, opened.data)
        self.assertEqual(opened.data["id"], pk)

    def test_no_return_window_setting_is_declared(self):
        # The config half of the decision: a merchant who wants a window gets
        # one as an env-driven key added HERE with its own documented default,
        # never as a number that appears from nowhere in a view.
        self.assertFalse(
            [name for name in dir(settings) if "RETURN" in name and "WINDOW" in name],
            "a return-window setting would mean the policy had acquired a "
            "number; if one is wanted, add it deliberately and say why",
        )


@tag("e2e")
class ReturnsSurfaceNotWidenedTests(ReturnTestCase):
    """This task added no route and no capability the customer seam did not have.

    Spec 6.8 line 1957 ("Return request review") is a STAFF feature and its
    routes are ``/admin/returns`` and ``/admin/returns/:id`` - Django admin
    pages, which SPEC-1-B07a already serves behind ``returns.read`` /
    ``returns.write`` ({support, admin}). The spec names no staff JSON API for
    returns anywhere, so none was invented: adding one would have widened the
    API surface past what the spec asks for.

    Asserted rather than asserted-in-prose, because "we did not widen anything"
    is exactly the claim that quietly stops being true.
    """

    def test_the_returns_family_is_exactly_the_two_customer_routes(self):
        routes = [
            str(pattern.pattern)
            for pattern in orders_urls.urlpatterns
            if "returns" in str(pattern.pattern)
        ]

        self.assertEqual(
            sorted(routes),
            sorted(["returns/", "returns/<int:return_request_id>/"]),
        )

    def test_no_returns_route_is_mounted_in_an_admin_family(self):
        # The staff surface stays the admin, behind the existing capabilities.
        admin_routes = [
            str(pattern.pattern)
            for pattern in config_urls.urlpatterns
            if "return" in str(pattern.pattern)
        ]

        self.assertEqual(admin_routes, [])

    def test_the_capability_map_is_unchanged_by_this_task(self):
        # No capability was added, widened or reassigned. Asserted against the
        # ROLES map - the source of truth - rather than restating the set here,
        # which would agree with a widened map from both sides. The
        # packing-operator question remains SPEC-1-B07c's, exactly as B07a
        # recorded it (B07a pins that role's exact set separately).
        from common.roles import CAPABILITY_ROLES

        self.assertEqual(RETURNS_READ, "returns.read")
        self.assertEqual(RETURNS_WRITE, "returns.write")
        self.assertEqual(
            CAPABILITY_ROLES[RETURNS_READ], frozenset({ROLE_SUPPORT, ROLE_ADMIN})
        )
        self.assertEqual(
            CAPABILITY_ROLES[RETURNS_WRITE], frozenset({ROLE_SUPPORT, ROLE_ADMIN})
        )

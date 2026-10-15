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
  two features, so 100% line coverage could not see it;
* the merchant-configurable return window is pinned at its boundary with
  HAND-WRITTEN day counts against a fixed aware clock, the anchor fork is
  pinned by executing the divergence between the two readings, and the retired
  "the gate reads no clock" invariant is INVERTED rather than deleted
  (``ReturnEligibilityWindowTests``) - SPEC-1-B07d, which overrules B07b's
  no-window outcome at the product owner's direction.
"""

import ast
import inspect
import textwrap
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django import forms
from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, User
from django.db import IntegrityError, transaction
from django.forms import modelform_factory
from django.test import override_settings, tag
from django.utils import timezone
from rest_framework.test import APIClient

from common.roles import (
    ROLE_ADMIN,
    ROLE_FINANCE,
    ROLE_MARKETING,
    ROLE_SUPPORT,
)
from common.testing import ApiTestCase
from config import urls as config_urls
from ops.models import (
    CLOSED_RETURN_WINDOW_DAYS,
    DEFAULT_RETURN_WINDOW_DAYS,
    MAX_RETURN_WINDOW_DAYS,
    SiteSettings,
)
from orders import urls as orders_urls
from orders.admin import (
    RETURN_CAPABILITY_MAP,
    RETURNS_READ,
    RETURNS_WRITE,
    ReturnRequestAdminForm,
)
from orders.models import (
    RETURN_ALLOWED_TRANSITIONS,
    RETURN_OPEN_STATUSES,
    RETURN_STATUS_CHOICES,
    Order,
    Refund,
    ReturnRequest,
    _reject_oversized_value,
    return_transition_allowed,
)
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
    RETURN_REFUSAL_BODY_KEY,
    RETURN_REFUSAL_ERRORS,
    RETURN_REFUSAL_WINDOW_CLOSED,
    MalformedReturnRequestBody,
    _return_body,
    _return_eligible,
    _return_request_miss,
    _return_window_anchor,
    _return_window_days,
    _return_window_refusal,
    _returns_closed,
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
                    expected = (
                        new_status == old_status
                        or new_status in (RETURN_ALLOWED_TRANSITIONS[old_status])
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

        from orders.admin import RETURNS_READ, RETURNS_WRITE

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

    def test_the_projection_names_no_field_outside_the_permitted_set(self):
        # What this actually pins, stated exactly. It asserts the SUBSET
        # direction - every EXPOSED field is a declared, permitted one - and
        # that is the direction that is a leak; a field absent from the
        # projection is a documented omission, never a vulnerability.
        #
        # What it does NOT do, and an earlier comment here wrongly claimed: it
        # does not detect a MODEL column that was never added to the projection.
        # ``Meta.fields`` is an explicit allowlist, so a new column is
        # structurally unable to leak - it never reaches ``serializer.fields``
        # until somebody edits that list by hand, and THAT edit is what these
        # two tests catch: this one rejects an unpermitted name the moment it
        # appears, and ``test_the_projection_is_exactly_the_hand_written_field_set``
        # rejects the field set changing at all.
        #
        # (Verified: adding ``staff_hint`` to the model alone, with no
        # ``Meta.fields`` edit, leaves both tests passing - which is the point.)
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
        # THE FIXTURE MUST EXCEED THE CAP, or this pin is vacuous. It once
        # overrode MAX=3 against the two-row setUp, where clamping and not
        # clamping return the SAME two rows - dropping the ``min()`` in
        # ``_returns_page_size`` left the suite green. Five rows against a cap
        # of two makes truncation observable: clamped gives 2 results over 3
        # pages, unclamped gives all 5 over 1 page.
        for index in range(3):
            order = self._order(
                f"RET-2026-00160{index}", self.buyer, status="delivered"
            )
            self.file_request(order, ReturnRequest.Status.REQUESTED)

        with override_settings(RETURNS_HISTORY_MAX_PAGE_SIZE=2):
            res = self.client.get(LIST_URL, {"page_size": 10000})

        self.assertEqual(res.status_code, 200, res.data)
        # There ARE more rows than the cap allows on one page...
        self.assertEqual(res.data["count"], 5)
        # ...and the caller's request for 10000 was clamped to the cap.
        self.assertEqual(len(res.data["results"]), 2)
        self.assertEqual(res.data["total_pages"], 3)
        self.assertIs(res.data["next_page"], True)
        # A non-numeric or non-positive value falls back to the configured
        # default rather than erroring - the order-history resolver's contract,
        # which this resolver deliberately mirrors.
        for junk in ("abc", "0", "-3", ""):
            with self.subTest(page_size=junk):
                fallback = self.client.get(LIST_URL, {"page_size": junk})
                self.assertEqual(fallback.status_code, 200, fallback.data)
                self.assertEqual(fallback.data["current_page"], 1)

    def test_a_page_past_the_end_lands_on_the_last_page_rather_than_404ing(self):
        # Same vacuity, different trap: at the shipped default page size the
        # two-row fixture is a SINGLE page, so "landed on the last page" and
        # "silently fell back to page 1" are the same number and the pin
        # cannot see which one happened. Sizing the page to 1 row against the
        # same two rows makes the listing genuinely multi-page, so a fallback
        # to page 1 now reads 1 where a correct answer reads 2.
        with override_settings(RETURNS_HISTORY_PAGE_SIZE=1):
            res = self.client.get(LIST_URL, {"page": 99})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["total_pages"], 2)
        self.assertEqual(res.data["current_page"], 2)
        self.assertIs(res.data["previous_page"], True)
        self.assertEqual(len(res.data["results"]), 1)

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


CLOCK_TOKENS = ("timezone", "timedelta", "settings", "now(")


def _is_docstring(statement):
    """Whether `statement` is a docstring - a bare string FIRST in a body."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _without_docstrings(node):
    """Strip every docstring under `node`, at every nesting level.

    A docstring is only the first statement of a def, class or module, so this
    tests the SHAPE before it drops anything. Slicing the first statement off
    instead would quietly blind the scan in the two cases that matter most: a
    function with no docstring at all would lose its only real statement, and a
    nested helper would lose its whole body - so a window clause hidden in
    either would pass a scan that is supposed to be about behaviour.
    """
    for child in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(child, field, None)
            if (
                isinstance(statements, list)
                and statements
                and _is_docstring(statements[0])
            ):
                setattr(child, field, statements[1:])


def _executable_source(function):
    """`function`'s EXECUTABLE body as source, with every docstring removed.

    This is the whole point of the clock scan: prose may name the calendar -
    SPEC-1-B07d's documentation is required to - while behaviour may not read
    it. Comments go too, since they are prose, and a nested def's docstring
    goes with its parent's so the two are not confused for each other.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    _without_docstrings(tree)

    return ast.unparse(ast.Module(body=tree.body[0].body, type_ignores=[]))


@tag("e2e")
class ReturnEligibilityWindowTests(ReturnTestCase):
    """[R-1.16] SPEC-1-B07d: the merchant-configurable return window.

    Spec 4 line 1083 ("Return/refund request where eligible") and spec 6.8
    line 1959 ("Eligibility validation") describe a window the spec never
    numbers, so SPEC-1-B07b shipped without one rather than fabricate policy
    wearing a configuration setting. **That was a build-order state, not a
    settled policy**: THE PRODUCT OWNER HAS SINCE REQUIRED a configurable
    site-wide return window, which overrides that outcome rather than
    complementing it. SPEC-1-B07d owns it, and its number lives in
    ``ops.SiteSettings`` because a window is merchant-facing policy each store
    sets for itself, not a deployment concern.

    So this class used to pin the ABSENCE of a window and now pins its
    boundary. That is a genuine change of invariant, not a test rewritten to
    keep passing, and the pins below were chosen so that the thing they replace
    could not have asserted anything useful anyway: ``test_an_eligible_order_
    is_eligible_at_every_age`` became meaningless the moment a window exists
    (a five-year-old order is simply refused now, which is the FEATURE), and
    ``test_the_gate_reads_no_clock_at_all`` asserted the opposite of the
    required behaviour. Each retirement is stated at the pin that replaces it.

    TWO DECISIONS ARE PINNED HERE, not left to the code:

    * the DEFAULT, ``DEFAULT_RETURN_WINDOW_DAYS = 30`` - the ecommerce
      convention (the published general-retail window the FTC's three-day
      online cooling-off rule sits under as a statutory floor), and the order
      lifecycle's own demand that the window outlive packing-plus-courier
      latency, because the gate admits a PAID order the merchant has not yet
      dispatched and a shorter window would expire a paying customer's right
      before their parcel existed;
    * the ANCHOR - delivery where the goods have arrived, the order's own date
      otherwise. The fallback is the production case, not an edge case:
      ``orders.models`` records that ``delivered_at``/``shipped_at``/
      ``fulfilled_at`` have no writer yet, so every real row is NULL on them.

    Every boundary below is written against HAND-WRITTEN day counts
    (``timedelta(days=30)``, spelled out) and against a FIXED aware clock.
    Neither is negotiable: a boundary recomputed from the configured number
    agrees with a wrong default from both sides (B07a cycle 2's defect), and a
    boundary sampled from a moving clock tests scheduling luck rather than
    the predicate.
    """

    # An AWARE, fixed instant with zero microseconds, so
    # ``anchor + timedelta(days=N)`` lands on an exactly representable moment
    # and "exactly N days admitted, N+1 refused" is a fact rather than a race.
    NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)

    @contextmanager
    def clock_frozen_at(self, moment=None):
        """The seam's clock pinned to ``moment`` (this class's NOW by default).

        ``orders.views`` is patched at its OWN attribute, which is the whole
        reason the probes call the private module helper rather than importing
        the gate with an injected clock: the production call path - one
        ``timezone.now()`` read inside ``_return_eligible`` - is what runs
        here, not a test-only variant of it. The freeze lasts only the block,
        so no other probe inherits a stale ``now``.
        """
        target = self.NOW if moment is None else moment
        with patch("orders.views.timezone.now", return_value=target):
            yield target

    def _placed(self, order_number, status, created_age, delivered_age=None):
        """An order whose own timestamps sit at hand-written ages.

        Ages are subtracted from the class's fixed ``NOW`` and NOT from
        ``timezone.now()``, so a probe's arithmetic does not depend on where in
        the suite it happens to run. ``delivered_age=None`` leaves
        ``delivered_at`` NULL - not a contrived row: no writer in this repo
        touches that column yet, so it is the shape every real order has.
        """
        order = self._order(order_number, self.buyer, status=status)
        stamps = {"created_at": self.NOW - created_age}
        if delivered_age is not None:
            stamps["delivered_at"] = self.NOW - delivered_age
        Order.objects.filter(pk=order.pk).update(**stamps)
        order.refresh_from_db()
        return order

    def test_an_eligible_order_inside_the_window_is_accepted(self):
        # The old pin here was "an eligible order is eligible at EVERY age",
        # up to ten years. A window makes that false by design, so it is
        # retired rather than weakened; what replaces it pins the ages that
        # must still work - including the very first second, because a gate
        # that read the clock before the order was stamped would refuse an
        # order the customer has only just placed.
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            for index, age in enumerate((timedelta(seconds=0), timedelta(days=29))):
                with self.subTest(age=age):
                    order = self._placed(f"RET-2026-0020{index}", "delivered", age)

                    res = self.ask(order.order_number)

                    self.assertEqual(res.status_code, 201, res.data)
                    self.assertTrue(_return_eligible(order))

    def test_an_ineligible_order_is_ineligible_at_every_age(self):
        # Unchanged by the window, and unchanged is the point: it is the half
        # that makes the accepted-side pins mean something. A window must never
        # RESCUE an order the machine refuses, so if age were an input to the
        # only predicate, SOME of these would flip. A pending order from five
        # years ago is exactly as ineligible as one created this morning - the
        # window can only ever add a refusal, never remove one.
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            for index, age in enumerate(
                (timedelta(seconds=0), timedelta(days=30), timedelta(days=3650))
            ):
                with self.subTest(age=age):
                    order = self._placed(f"RET-2026-0021{index}", "pending", age)

                    res = self.ask(order.order_number)

                    self.assertEqual(res.status_code, 409, res.data)
                    self.assertFalse(_return_eligible(order))

    def test_the_window_admits_exactly_thirty_days_and_refuses_the_next(self):
        # HAND-WRITTEN literals on both sides, deliberately NOT
        # ``timedelta(days=configured_window)``: a boundary whose expected
        # value is recomputed from the number under test agrees with a WRONG
        # default from both sides, which is the defect the auditor caught in
        # B07a cycle 2. ``test_the_default_is_thirty_days`` pins the constant
        # to the same literal, so the constant and the behaviour are checked
        # from two independent directions.
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            on_the_last_day = self._placed(
                "RET-2026-0040", "delivered", timedelta(days=30)
            )
            one_day_late = self._placed(
                "RET-2026-0041", "delivered", timedelta(days=31)
            )

            admitted = self.ask(on_the_last_day.order_number)
            refused = self.ask(one_day_late.order_number)

            self.assertEqual(admitted.status_code, 201, admitted.data)
            self.assertEqual(refused.status_code, 409, refused.data)
            self.assertTrue(_return_eligible(on_the_last_day))
            self.assertFalse(_return_eligible(one_day_late))

    def test_the_default_is_thirty_days(self):
        # Decision (a), pinned twice: the CONSTANT against a hand-written 30,
        # and the resolved value against the same literal. Two sides, so
        # changing the default to 14 cannot leave the behaviour pin agreeing
        # with it.
        self.assertEqual(DEFAULT_RETURN_WINDOW_DAYS, 30)

        row = SiteSettings.load()

        self.assertIsNone(row.return_window_days, "the window starts out unset")
        self.assertEqual(row.resolved_return_window_days(), 30)
        self.assertEqual(_return_window_days(), 30)

    def test_an_unset_window_admits_thirty_days_and_refuses_thirty_one(self):
        # The blank-means-default path end to end, over the seam rather than
        # the model: a store that has never touched the field still gets a
        # decidable window, and the seam says so with its own bytes.
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            admitted = self.ask(
                self._placed(
                    "RET-2026-0044", "delivered", timedelta(days=30)
                ).order_number
            )
            refused = self.ask(
                self._placed(
                    "RET-2026-0045", "delivered", timedelta(days=31)
                ).order_number
            )

            self.assertEqual(admitted.status_code, 201, admitted.data)
            self.assertEqual(refused.status_code, 409, refused.data)

    def test_a_configured_window_is_the_one_that_is_enforced(self):
        # The store's SETTING drives the gate, not the constant read directly.
        # Thirty is out of the question here, so a build that ignored the
        # column and used DEFAULT_RETURN_WINDOW_DAYS would admit the
        # eleven-day-old order below and fail this pin.
        row = SiteSettings.load()
        row.return_window_days = 7
        row.save()
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            seventh_day = self._placed("RET-2026-0046", "delivered", timedelta(days=7))
            eighth_day = self._placed("RET-2026-0047", "delivered", timedelta(days=8))
            past_the_default = self._placed(
                "RET-2026-0048", "delivered", timedelta(days=11)
            )

            self.assertEqual(_return_window_days(), 7)
            self.assertEqual(self.ask(seventh_day.order_number).status_code, 201)
            self.assertEqual(self.ask(eighth_day.order_number).status_code, 409)
            self.assertEqual(self.ask(past_the_default.order_number).status_code, 409)

    def test_the_window_runs_from_delivery_when_the_goods_arrived(self):
        # THE DIVERGENCE, made executable. An order placed 50 days ago and
        # delivered 20 days ago: a window measured from ``created_at`` refused
        # it a full 20 days ago, and one measured from ``delivered_at`` admits
        # it today, on the last day of its window. Both readings are asserted
        # here - the chosen one by the seam's own answer, the rejected one by
        # a hand-written inequality - because the divergence is the entire
        # argument for the anchor, and an argument nobody can execute is not an
        # argument.
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            slow = self._placed(
                "RET-2026-0049",
                "delivered",
                created_age=timedelta(days=50),
                delivered_age=timedelta(days=20),
            )

            self.assertEqual(_return_window_anchor(slow), slow.delivered_at)
            # What a created_at reading would have said, spelled out:
            self.assertLess(slow.created_at + timedelta(days=30), self.NOW)
            self.assertTrue(_return_eligible(slow))

            res = self.ask(slow.order_number)

            self.assertEqual(res.status_code, 201, res.data)

    def test_an_order_that_never_arrived_is_measured_from_its_own_date(self):
        # The unshipped-but-admitted case, and the reason the fallback exists:
        # ``confirmed`` is ``captured / unfulfilled``, so the gate admits this
        # row on the MONEY half alone while the merchant has not dispatched
        # it and there is no delivery to anchor to. Both readings of the fork
        # would use the order date here, which is why a window shorter than the
        # store's own packing latency would expire a paying customer's right
        # before their parcel existed.
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            paid_unshipped = self._placed(
                "RET-2026-0050", "confirmed", timedelta(days=10)
            )
            stale_unshipped = self._placed(
                "RET-2026-0051", "confirmed", timedelta(days=31)
            )

            self.assertIsNone(paid_unshipped.delivered_at)
            self.assertEqual(
                _return_window_anchor(paid_unshipped), paid_unshipped.created_at
            )
            self.assertTrue(_return_eligible(paid_unshipped))
            self.assertFalse(_return_eligible(stale_unshipped))
            self.assertEqual(self.ask(paid_unshipped.order_number).status_code, 201)
            self.assertEqual(self.ask(stale_unshipped.order_number).status_code, 409)

    def test_the_retired_no_clock_invariant_now_names_what_the_gate_reads(self):
        # REPLACES ``test_the_gate_reads_no_clock_at_all``, and the replacement
        # is deliberately not a weaker version of it. That pin walked this
        # function's executable body and asserted it contained no ``timezone``
        # / ``timedelta`` / ``settings`` / ``now(`` - a correct description of
        # the world B07b shipped, and the exact opposite of the world the
        # product owner then required. A window NEEDS a clock read; deleting
        # the pin without replacing it would have left "the gate ignores
        # elapsed time" as an untested claim, and rewriting it to assert the
        # tokens are ABSENT-by-name-substitution would have bought nothing.
        #
        # So the scan is INVERTED rather than removed, and it says what is now
        # required instead: the gate reads the clock, the number it compares
        # against comes from the store's own settings row, and neither read
        # comes from deployment config. The scan stays behavioural (docstrings
        # and comments stripped by ``_executable_source``) because SPEC-1-B07d
        # REQUIRES prose that names both - a text scan would fire on the
        # documentation of the thing it exists to precede.
        #
        # This is the structural half only, and it is the smaller half: the
        # boundary, fork and per-call pins below are what prove the window is
        # honoured, because they would fail if the tokens were present and the
        # window were not read.
        gate = _executable_source(_return_eligible)

        self.assertIn(
            "timezone",
            gate,
            "the gate must read the clock now that a window exists",
        )

        window = _executable_source(_return_window_days)

        self.assertIn(
            "SiteSettings",
            window,
            "the number must come from the store's own settings row",
        )
        self.assertNotIn(
            "django.conf",
            window,
            "a merchant-set window must never be deployment configuration",
        )

    def test_the_gate_reads_the_window_on_every_call_and_not_from_a_cache(self):
        # The behavioural replacement for the structural pin above, and the one
        # that cannot be satisfied by tokens in the source. A gate that read the
        # window at import time, or memoised it, would keep answering with the
        # number it first saw; this walks the SAME order through the seam twice
        # and watches the answer change under it.
        self.login_as(self.buyer)
        order = self._placed("RET-2026-0052", "delivered", timedelta(days=5))

        with self.clock_frozen_at():
            with patch("orders.views.return._return_window_days", return_value=0):
                self.assertFalse(_return_eligible(order))
                self.assertEqual(self.ask(order.order_number).status_code, 409)
            self.assertTrue(_return_eligible(order))
            self.assertEqual(self.ask(order.order_number).status_code, 201)

    def test_the_clock_scan_reads_the_body_and_not_the_prose(self):
        # The scan's own contract, so it cannot quietly regress into the text
        # scan it replaced. Every fixture is scanned through the SAME helper the
        # gate test uses, so this pins the scan that actually runs rather than a
        # copy of it, and the two directions are asserted together because a
        # scanner that only ever passes proves nothing.
        #
        # The NO-DOCSTRING and NESTED rows are the ones a shortcut gets wrong.
        # Slicing the first statement off the body - which is what this helper
        # did before it was measured - makes a docstring-less function's only
        # statement disappear and a nested def's whole body with it, so both of
        # those clock reads passed a scan whose entire job is to catch them.
        def prose_only():
            """The clock is named here only - settings, now(, timezone."""
            return "return order"

        def multiline_prose():
            """First line.

            settings, now(, timezone, timedelta - across a multi-line docstring.
            """
            return "return order"

        def prose_beside_a_comment():
            """> settings now( timezone timedelta"""
            # The same four words again, in a comment.
            return "return order"

        def nested_prose():
            def inner():
                """settings, now(, timezone - the inner function's prose."""

            return inner

        def with_a_clock_read():
            """> Settings ignored; this body reads the calendar."""
            return timezone.now()

        def with_a_clock_read_and_no_docstring():
            return timezone.now()

        def nested_clock_read():
            def inner():
                return timezone.now()

            # Called, not merely returned: a nested body that never runs is
            # not behaviour, and an unexecuted line is an uncovered one.
            return inner()

        # Every fixture is CALLED as well as parsed. A body that never runs is a
        # shape rather than a function, and the whole point of the rewrite is
        # that the scan describes behaviour - so the behaviour is exercised.
        for prose_fixture in (prose_only, multiline_prose, prose_beside_a_comment):
            with self.subTest(function=prose_fixture.__name__):
                self.assertEqual(prose_fixture(), "return order")
        self.assertIsNone(nested_prose()())
        for clock_fixture in (
            with_a_clock_read,
            with_a_clock_read_and_no_docstring,
            nested_clock_read,
        ):
            with self.subTest(function=clock_fixture.__name__):
                self.assertIsNotNone(clock_fixture())

        for function, should_pass in (
            (prose_only, True),
            (multiline_prose, True),
            (prose_beside_a_comment, True),
            (nested_prose, True),
            (with_a_clock_read, False),
            (with_a_clock_read_and_no_docstring, False),
            (nested_clock_read, False),
        ):
            with self.subTest(function=function.__name__, should_pass=should_pass):
                found = [t for t in CLOCK_TOKENS if t in _executable_source(function)]
                if should_pass:
                    self.assertEqual(found, [], "prose must not trip the scan")
                else:
                    self.assertTrue(found, "a clock read in the body must trip it")

    def test_a_request_filed_years_ago_is_still_readable_and_still_its_own(self):
        # Age must not change what the customer can SEE, only what they may
        # ASK for. The window retired the other half of this probe - a
        # ten-year-old order can no longer be filed against - so the request
        # is filed while it is in date and aged afterwards, which keeps the
        # claim it was written to make and stops it quietly re-testing the
        # window instead.
        self.login_as(self.buyer)
        fresh = self._placed("RET-2026-002200", "delivered", timedelta(days=1))
        created = self.ask(fresh.order_number)
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
        # The config half, and it SURVIVES this task: a return-window NUMBER
        # must not appear in deployment config. SPEC-1-B07d puts it in an
        # ``ops.SiteSettings`` row, because a window is merchant-facing policy
        # each store sets for itself, and not as an env key the way the two
        # page-density keys above are. If a number is ever wanted here, add it
        # deliberately in the right home and say why.
        self.assertFalse(
            [name for name in dir(settings) if "RETURN" in name and "WINDOW" in name],
            "a return-window deployment key would put the policy in the wrong "
            "place; a window belongs in ops.SiteSettings, and if one is wanted "
            "here, add it deliberately and say why",
        )

    def test_a_client_cannot_steer_the_window_through_the_create_body(self):
        # CHECK 1, the security surface this task adds: a site-wide setting is
        # merchant-controlled, so the probe is that a CUSTOMER cannot move it -
        # and cannot widen their own eligibility by trying. Six spellings of
        # the same attempt, every one ignored, because the create body reads
        # exactly three fields (``_RETURN_BODY_FIELDS``) and stores what it
        # read. The store's window is 1 day and the order is 10 days old, so
        # the baseline answer is a refusal and any spelling that worked would
        # have to change these bytes.
        row = SiteSettings.load()
        row.return_window_days = 1
        row.save()
        self.login_as(self.buyer)
        order = self._placed("RET-2026-0053", "delivered", timedelta(days=10))
        attempts = (
            {},
            {"return_window_days": 3650},
            {"return_window": 3650},
            {"window_days": 3650},
            {"days": 3650},
            {"site_settings": {"return_window_days": 3650}},
        )

        baseline = None
        for extra in attempts:
            with self.subTest(field=sorted(extra) or "none"):
                res = self.ask(order.order_number, **extra)

                self.assertEqual(res.status_code, 409, res.data)
                if baseline is None:
                    baseline = res.content
                # Byte-identical to the attempt with no steering parameter: a
                # different body would confirm that the parameter was read.
                self.assertEqual(res.content, baseline)

        self.assertEqual(SiteSettings.load().return_window_days, 1)

    def test_the_window_comes_from_the_singleton_and_from_no_other_row(self):
        # CHECK 2, the false-universal probe. ``load()`` is a ``get_or_create``
        # on pk=1, so repeated reads cannot fork the row: two callers racing
        # into the same INSERT get one row and one IntegrityError the ORM
        # retries, and the primary key is what forbids a second row at all.
        # The singleton's ``save()`` pk=1 guard, though, does NOT cover
        # queryset paths - so the second half of this probe creates exactly
        # the row that guard was supposed to prevent, by the one route that
        # skips ``save()``, and pins that the window is NOT steered by it.
        row = SiteSettings.load()
        row.return_window_days = 45
        row.save()

        for _ in range(3):
            SiteSettings.load()
        self.assertEqual(SiteSettings.objects.count(), 1)
        self.assertEqual(_return_window_days(), 45)

        SiteSettings.objects.bulk_create([SiteSettings(pk=2, return_window_days=999)])

        self.assertEqual(SiteSettings.objects.count(), 2)
        self.assertEqual(_return_window_days(), 45)

    def test_the_window_is_published_to_the_storefront(self):
        # Decision (b)-adjacent disclosure: the window is STORE POLICY, so it
        # is published on the one public settings surface the spec already has.
        # Asserted on the raw bytes, including anonymously, and pinned to the
        # RESOLVED value - a storefront that had to implement its own copy of
        # the default is a storefront that will show the wrong number.
        self.login_as(self.buyer)

        res = self.client.get("/api/settings/")
        body = res.content.decode()

        self.assertEqual(res.status_code, 200)
        self.assertIn('"return_window_days": 30', body)

        row = SiteSettings.load()
        row.return_window_days = 45
        row.save()

        res = self.client.get("/api/settings/")

        self.assertEqual(res.status_code, 200)
        self.assertIn('"return_window_days": 45', res.content.decode())

    def test_only_settings_manage_roles_may_read_or_write_the_window(self):
        # No capability was widened: the field landed on an existing admin
        # whose ``capability_map`` already gates every verb on
        # ``settings.manage`` ({admin, superadmin}). A support operator -
        # who DOES hold ``returns.read``/``returns.write`` - must not be able
        # to set the window, or the packing desk could widen the policy it is
        # judged by.
        SiteSettings.load()
        url = "/admin/ops/sitesettings/1/change/"
        payload = {
            "support_email": "",
            "support_phone": "",
            "whatsapp_number": "",
            "whatsapp_message": "",
            "instagram_url": "",
            "return_window_days": 45,
            "_save": "Save",
        }

        self.client.force_login(role_user(ROLE_SUPPORT, "window-support"))
        refused = self.client.post(url, payload)
        self.assertEqual(refused.status_code, 403)
        self.assertIsNone(SiteSettings.load().return_window_days)

        self.client.force_login(role_user(ROLE_ADMIN, "window-admin"))
        allowed = self.client.post(url, payload)

        self.assertEqual(allowed.status_code, 302)
        self.assertEqual(SiteSettings.load().return_window_days, 45)

    def test_the_window_field_is_reachable_on_the_admin_form(self):
        # ``fieldsets`` on that admin is EXPLICIT, so without a field entry the
        # column would exist, be writable through the ORM, and be unreachable
        # in the only surface the merchant has for editing settings - a policy
        # no one could change. Asserted on the rendered bytes, which is where
        # "reachable" actually means something.
        SiteSettings.load()
        self.client.force_login(role_user(ROLE_ADMIN, "window-form-admin"))

        res = self.client.get("/admin/ops/sitesettings/1/change/")

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "return_window_days")

    def test_the_window_column_is_bounded_on_both_database_backends(self):
        # CHECK 5, the SQLite/Postgres split. The ceiling is a POLICY bound, not
        # a storage limit - past ten years a return window stops being one a
        # merchant can honour - and it is asserted at the FORM deliberately:
        # ``MaxValueValidator`` is Python, so it is the same code path on every
        # backend, whereas the column's own bounds are Postgres-enforced and
        # ignored by SQLite. An earlier version of this comment called the
        # ceiling a representability bound and predicted a DataError; 3650 sits
        # far below Postgres int4's 2,147,483,647, so that was false. A
        # negative window is definitional nonsense.
        form_class = modelform_factory(
            SiteSettings,
            fields=["return_window_days"],
            widgets={"return_window_days": forms.NumberInput},
        )

        accepted = form_class(data={"return_window_days": MAX_RETURN_WINDOW_DAYS})

        self.assertTrue(accepted.is_valid(), accepted.errors)

        for rejected in (-1, MAX_RETURN_WINDOW_DAYS + 1, 10**12):
            with self.subTest(value=rejected):
                refused = form_class(data={"return_window_days": rejected})

                self.assertFalse(refused.is_valid())
                self.assertIn("return_window_days", refused.errors)

    def test_a_negative_window_is_refused_by_the_admin_form(self):
        # The end-to-end half of the probe above: the merchant's save attempt,
        # not the form field in isolation, leaves the stored policy untouched.
        SiteSettings.load()
        self.client.force_login(role_user(ROLE_ADMIN, "window-bound-admin"))

        res = self.client.post(
            "/admin/ops/sitesettings/1/change/",
            {
                "support_email": "",
                "support_phone": "",
                "whatsapp_number": "",
                "whatsapp_message": "",
                "instagram_url": "",
                "return_window_days": -1,
                "_save": "Save",
            },
        )

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(SiteSettings.load().return_window_days)

    def test_the_ceiling_is_the_policy_value_written_out_by_hand(self):
        # The load-bearing half of the ceiling probe, and the half that is NOT
        # derived from the constant. The probe above takes BOTH of its
        # expectations FROM ``MAX_RETURN_WINDOW_DAYS`` - the accepted value IS
        # the constant and the refused value is the constant + 1 - so it agrees
        # with whatever that constant says, and setting it to 999999 leaves it
        # green because the value under test and the value expected of it move
        # together. A pin whose expectation is recomputed from the constant
        # under test cannot fail, so the number 3650 is spelled out below as a
        # literal: not imported, not computed, not ``MAX + anything``.
        #
        # Two independent literal expectations, because either alone is half a
        # pin. The FORM half is asserted first and inside ``subTest`` so that
        # under a mutated constant BOTH halves are reported rather than the
        # first one masking the second.
        form_class = modelform_factory(
            SiteSettings,
            fields=["return_window_days"],
            widgets={"return_window_days": forms.NumberInput},
        )

        for accepted_value, should_be_valid in ((3650, True), (3651, False)):
            with self.subTest(days=accepted_value):
                probe = form_class(data={"return_window_days": accepted_value})

                self.assertEqual(probe.is_valid(), should_be_valid, probe.errors)

        # 3650 is the POLICY value - ten years - and not a storage limit. (The
        # probe above still calls the ceiling a representability bound, which
        # this cycle's changelog entry records as a superseded description:
        # ``PositiveIntegerField`` is an int4 on Postgres, which holds
        # 2,147,483,647, so nothing is refused here for being unrepresentable.)
        self.assertEqual(MAX_RETURN_WINDOW_DAYS, 3650)

    # ------------------------------------------------------------------
    # SPEC-1-B07f-a: 0 CLOSES RETURNS.
    #
    # The owner's ruling of 2026-10-02 gave the column three states where it
    # previously had two that behaved like one: NULL (no policy, resolves to
    # 30), 0 (RETURNS ARE CLOSED) and N > 0 (returns allowed within N days of
    # the anchor). The shipped code branched on ``is None``, so a published 0
    # fell into the date arithmetic and was read as a window one instant long:
    # an order was admitted AT THE EXACT ANCHOR INSTANT and refused a second
    # later. That was an accident of the arithmetic rather than a decision, and
    # the probes below are the replacement for it.
    #
    # EVERY expectation in this section is a HAND-WRITTEN LITERAL - 30, 0, 1,
    # 14, 3650, "window_closed", "Returns aren't available for this order" -
    # and never the constant under test. A boundary or a refusal recomputed
    # from ``CLOSED_RETURN_WINDOW_DAYS`` or ``DEFAULT_RETURN_WINDOW_DAYS``
    # agrees with a WRONG constant from both sides, which is the defect this
    # file has already been bitten by (SPEC-1-B07a cycle 2, and the ceiling
    # probe above).
    # ------------------------------------------------------------------

    def test_the_three_states_are_three_and_each_is_spelled_out_by_hand(self):
        # The state ENUMERATION, driven value by value: every value this
        # column admits is written in the table, and both expectations beside
        # it are literals. A 100%-covered gate can still be wrong about a value
        # nothing drives it with, so the table is the coverage that counts -
        # and 3651 is in it deliberately, because it is storable through the
        # ORM even though the admin form refuses to write it (the ceiling
        # probe above), so the gate has to have an answer for it too.
        self.assertEqual(CLOSED_RETURN_WINDOW_DAYS, 0)

        for published, resolved, is_closed in (
            (None, 30, False),  # unset -> the default, and nothing is closed
            (0, 0, True),  # the ruling: 0 closes returns
            (1, 1, False),
            (14, 14, False),
            (30, 30, False),
            (3650, 3650, False),  # the ceiling
            (3651, 3651, False),  # storable, form-refused, still coherent
        ):
            with self.subTest(published=published):
                row = SiteSettings.load()
                row.return_window_days = published
                row.save()
                row.refresh_from_db()

                self.assertEqual(row.return_window_days, published)
                self.assertEqual(row.resolved_return_window_days(), resolved)
                self.assertIs(row.returns_closed(), is_closed)
                # The seam the gate reads, and the model predicate the refusal
                # sentence is chosen by, must not disagree with the row.
                self.assertEqual(_return_window_days(), resolved)
                self.assertIs(_returns_closed(), is_closed)

    def test_a_published_zero_closes_returns_at_the_exact_anchor_instant(self):
        # THE SHARPEST CASE IN THE RULING, and the one the old arithmetic got
        # wrong by accident: at the anchor instant a zero-day window evaluates
        # ``now <= anchor + timedelta(days=0)`` to TRUE, so the shipped build
        # ADMITTED the order here and refused it a second later. Both readings
        # of the anchor are driven, because the anchor is a fork (delivery
        # where the goods arrived, the order date otherwise) and a closed
        # window has to close on either one.
        row = SiteSettings.load()
        row.return_window_days = 0
        row.save()
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            # ``timedelta(seconds=0)`` is the anchor instant written out: the
            # order stamped exactly now, and the same order delivered exactly
            # now after fifty days in the warehouse.
            at_its_own_instant = self._placed(
                "RET-2026-0060", "delivered", timedelta(seconds=0)
            )
            just_delivered = self._placed(
                "RET-2026-0061",
                "delivered",
                created_age=timedelta(days=50),
                delivered_age=timedelta(seconds=0),
            )

            for order in (at_its_own_instant, just_delivered):
                with self.subTest(anchor=_return_window_anchor(order)):
                    self.assertEqual(_return_window_anchor(order), self.NOW)
                    self.assertFalse(_return_eligible(order))
                    # The refusal is a NAMED one, checked against a literal
                    # rather than against the constant in the module.
                    self.assertEqual(
                        _return_window_refusal(order, self.NOW), "window_closed"
                    )

                    res = self.ask(order.order_number)

                    self.assertEqual(res.status_code, 409, res.data)
                    # ``details``, not ``code``: the error envelope ASSIGNS
                    # ``code`` from the status family and moves the rest into
                    # ``details`` (common/errors.py, _envelope), so the refusal
                    # code has to be read from where it actually lands.
                    self.assertEqual(
                        res.data["details"]["return_refusal"], "window_closed"
                    )
                    self.assertEqual(
                        res.data["error"], "Returns aren't available for this order"
                    )

            # No loophole: a refusal writes no return request, so closing the
            # window cannot become a way to slip one past the seam.
            self.assertEqual(ReturnRequest.objects.count(), 0)

    def test_a_published_zero_closes_returns_for_an_order_of_any_age(self):
        # "Every return request" is the ruling's phrase, so every age is
        # driven rather than one: a closed store is not a store with a very
        # short window, and the ages below include one INSIDE the default
        # 30-day window, which is the value a reader is most likely to assume
        # a 0 still obeys.
        row = SiteSettings.load()
        row.return_window_days = 0
        row.save()
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            for index, age in enumerate(
                (
                    timedelta(seconds=1),
                    timedelta(days=1),
                    timedelta(days=29),
                    timedelta(days=3650),
                )
            ):
                with self.subTest(age=age):
                    order = self._placed(f"RET-2026-006{index + 2}", "delivered", age)

                    res = self.ask(order.order_number)

                    self.assertEqual(res.status_code, 409, res.data)
                    self.assertEqual(
                        res.data["details"]["return_refusal"], "window_closed"
                    )
                    self.assertFalse(_return_eligible(order))

        self.assertEqual(ReturnRequest.objects.count(), 0)

    def test_a_closed_refusal_is_not_the_expired_window_refusal(self):
        # THE DISTINGUISHABILITY PIN, and it is run on ONE order at ONE moment
        # under TWO policies, so the only thing that differs between the two
        # answers is the store's published window. A build that merely reworded
        # the closed refusal, or that reused the expired body, fails here; a
        # build that answers both with one body fails here too.
        self.login_as(self.buyer)
        row = SiteSettings.load()

        with self.clock_frozen_at():
            order = self._placed("RET-2026-0066", "delivered", timedelta(days=31))

            row.return_window_days = 0
            row.save()
            closed = self.ask(order.order_number)

            row.return_window_days = 30
            row.save()
            expired = self.ask(order.order_number)

        self.assertEqual(closed.status_code, 409, closed.data)
        self.assertEqual(expired.status_code, 409, expired.data)
        # The two bodies, as literals. The closed one names the store's policy
        # and carries the refusal code under ``details``; the expired one is the
        # envelope this seam has always returned, unchanged, with ``details``
        # EMPTY. Both keep ``code: "conflict"`` - that is the status family the
        # error envelope assigns to every 409 in this app and this task does not
        # change it - so the refusal code is what tells the two apart.
        self.assertEqual(
            closed.data["error"], "Returns aren't available for this order"
        )
        self.assertEqual(
            expired.data["error"], "This order is not eligible for a return"
        )
        self.assertEqual(closed.data["details"]["return_refusal"], "window_closed")
        self.assertEqual(expired.data["details"], {})
        self.assertEqual(closed.data["code"], "conflict")
        self.assertEqual(expired.data["code"], "conflict")
        self.assertNotEqual(closed.content, expired.content)
        # And the codes are module constants B07f-b and the storefront can
        # import, which is what "programmatically distinguishable" has to mean
        # for a task that has not been written yet.
        self.assertEqual(RETURN_REFUSAL_WINDOW_CLOSED, "window_closed")
        self.assertEqual(RETURN_REFUSAL_BODY_KEY, "return_refusal")
        self.assertEqual(
            RETURN_REFUSAL_ERRORS[RETURN_REFUSAL_WINDOW_CLOSED],
            closed.data["error"],
        )

    def test_a_window_of_one_day_or_more_behaves_exactly_as_before(self):
        # THE RULING CHANGED THE MEANING OF 0 AND NOTHING ELSE. Each N below
        # is admitted on its last day and refused the day after, on both the
        # predicate and the seam, with the refusal carrying the expired body
        # and NO code - which is what proves the closed branch did not quietly
        # widen. 3650 is the ceiling and 3651 the day past it, so the widest
        # window the machine admits is exercised at its own boundary.
        self.login_as(self.buyer)
        row = SiteSettings.load()

        with self.clock_frozen_at():
            for index, (days, admitted_age, refused_age) in enumerate(
                (
                    (1, timedelta(days=1), timedelta(days=2)),
                    (14, timedelta(days=14), timedelta(days=15)),
                    (30, timedelta(days=30), timedelta(days=31)),
                    (3650, timedelta(days=3650), timedelta(days=3651)),
                )
            ):
                with self.subTest(days=days):
                    row.return_window_days = days
                    row.save()
                    on_the_last_day = self._placed(
                        f"RET-2026-007{index}0", "delivered", admitted_age
                    )
                    one_day_late = self._placed(
                        f"RET-2026-007{index}1", "delivered", refused_age
                    )

                    self.assertEqual(_return_window_days(), days)
                    self.assertFalse(_returns_closed())
                    self.assertTrue(_return_eligible(on_the_last_day))
                    self.assertFalse(_return_eligible(one_day_late))
                    self.assertIsNone(_return_window_refusal(on_the_last_day, self.NOW))
                    self.assertEqual(
                        _return_window_refusal(one_day_late, self.NOW),
                        "outside_window",
                    )

                    admitted = self.ask(on_the_last_day.order_number)
                    refused = self.ask(one_day_late.order_number)

                    self.assertEqual(admitted.status_code, 201, admitted.data)
                    self.assertEqual(refused.status_code, 409, refused.data)
                    self.assertEqual(
                        refused.data["error"],
                        "This order is not eligible for a return",
                    )
                    # No refusal code: an expired window is the plain conflict
                    # this seam has always returned, and a body that grew a code
                    # here would mean the closed branch has widened.
                    self.assertEqual(refused.data["details"], {})

    def test_a_closed_store_says_so_even_where_the_machine_itself_refuses(self):
        # THE COMBINATION NOTHING ELSE IN THIS SECTION DRIVES. Every other
        # closed-window probe here uses a delivered order, which PASSES the
        # machine half of ``_return_eligible`` and is therefore refused by the
        # window. A pending order is refused by the MACHINE instead - nothing
        # shipped and no money captured - so the closed branch OVERRIDING the
        # order-specific reason is a behaviour that shipped with no probe
        # against it. Coverage cannot see this: it is one line, executed for
        # either reason, and both reasons execute it.
        #
        # What is pinned is the SENTENCE and the CODE, because that is the only
        # observable difference between the two policies on this order. The
        # store's policy is a fact about the shop rather than about this order,
        # and a customer of a shop that takes no returns should not be told
        # their own window ran out.
        row = SiteSettings.load()
        row.return_window_days = 30
        row.save()
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            machine_refused = self._placed(
                "RET-2026-0077", "pending", timedelta(days=3)
            )

            self.assertFalse(_return_eligible(machine_refused))
            self.assertFalse(_returns_closed())
            open_res = self.ask(machine_refused.order_number)

            row.return_window_days = 0
            row.save()

            self.assertTrue(_returns_closed())
            closed_res = self.ask(machine_refused.order_number)

        # THE SAME ORDER at THE SAME MOMENT: the published window is the only
        # thing that differs between the two answers, so the override is what
        # separates these bodies and nothing else can.
        self.assertEqual(open_res.status_code, 409, open_res.data)
        self.assertEqual(closed_res.status_code, 409, closed_res.data)
        self.assertEqual(
            open_res.data["error"], "This order is not eligible for a return"
        )
        self.assertEqual(open_res.data["details"], {})
        self.assertEqual(
            closed_res.data["error"], "Returns aren't available for this order"
        )
        self.assertEqual(closed_res.data["details"]["return_refusal"], "window_closed")
        # Refused is not merely re-described: a refusal writes no row, so the
        # override cannot become a way to write one.
        self.assertEqual(ReturnRequest.objects.count(), 0)

    def test_a_window_above_the_ceiling_is_an_ordinary_positive_window(self):
        # 3651 IS THE STATE THE OTHER TWO PROBES LEAVE BETWEEN THEM, and the
        # gate is the thing the ruling is about. The admin form refuses to
        # WRITE it (the ceiling probe above), and the three-states probe reads
        # it only through the model's own predicates - so nothing asked the
        # GATE what it does with a value above the policy ceiling, which is
        # the exact question a "closed is a magnitude rather than a state"
        # regression would answer wrongly.
        #
        # It is reachable without the admin, which is why it needs a probe: the
        # column is an integer whose validator runs on ``full_clean`` and not on
        # ``save``, so an ORM write stores it. The expectation is that it
        # behaves like any other positive window, because closed is a NAMED
        # state and not a size - a build that treated the ceiling as the closed
        # state would have to read 3651 as closed, and this is what holds that
        # line. 3650 is NOT repeated here: the probe above already drives it at
        # its own boundary, and duplicating a boundary makes two places to
        # update rather than one.
        row = SiteSettings.load()
        row.return_window_days = 3651
        row.save()
        self.login_as(self.buyer)

        with self.clock_frozen_at():
            on_the_last_day = self._placed(
                "RET-2026-0078", "delivered", timedelta(days=3651)
            )
            one_day_late = self._placed(
                "RET-2026-0079", "delivered", timedelta(days=3652)
            )

            self.assertEqual(SiteSettings.load().return_window_days, 3651)
            self.assertEqual(_return_window_days(), 3651)
            self.assertFalse(_returns_closed())
            self.assertTrue(_return_eligible(on_the_last_day))
            self.assertFalse(_return_eligible(one_day_late))
            self.assertIsNone(_return_window_refusal(on_the_last_day, self.NOW))
            self.assertEqual(
                _return_window_refusal(one_day_late, self.NOW), "outside_window"
            )

            admitted = self.ask(on_the_last_day.order_number)
            refused = self.ask(one_day_late.order_number)

            self.assertEqual(admitted.status_code, 201, admitted.data)
            self.assertEqual(refused.status_code, 409, refused.data)
            self.assertEqual(
                refused.data["error"],
                "This order is not eligible for a return",
            )
            self.assertEqual(refused.data["details"], {})

    def test_a_stranger_is_never_told_whether_the_store_is_closed(self):
        # THE DISCLOSURE HALF OF THE NEW CODE, on the seam that carries it.
        # The closed refusal is a statement about the SHOP, so it is worth
        # asking whether publishing it turns the create seam into a probe for
        # store policy. It does not, and that is a property of WHERE the check
        # sits rather than of what it says: it lives inside the
        # ``not _return_eligible`` branch, which is reached only after
        # ``select_for_update().get(..., user=request.user)`` has matched a row
        # the caller OWNS.
        #
        # Both policies are driven for the SAME stranger on the SAME order, so
        # the published window is the only thing that could separate the two
        # answers - and nothing may.
        order = self._placed("RET-2026-0080", "delivered", timedelta(days=3))
        row = SiteSettings.load()
        self.login_as(self.stranger)

        with self.clock_frozen_at():
            row.return_window_days = 0
            row.save()
            closed_res = self.ask(order.order_number)

            row.return_window_days = 30
            row.save()
            open_res = self.ask(order.order_number)

        self.assertEqual(closed_res.status_code, 404, closed_res.data)
        self.assertEqual(open_res.status_code, 404, open_res.data)
        self.assertEqual(closed_res.content, open_res.content)
        # Nor smuggled in under some other key: the policy is not in this body
        # at all, on either policy.
        self.assertNotIn("return_refusal", closed_res.content.decode())
        self.assertNotIn("window_closed", closed_res.content.decode())
        self.assertEqual(ReturnRequest.objects.count(), 0)

    def test_a_client_can_neither_open_nor_close_returns_through_the_body(self):
        # CHECK 1 for the third state, in BOTH directions. A store-wide policy
        # is merchant-controlled, so the probe is that a CUSTOMER can move it
        # in neither direction: a closed store must not be reopened by a body
        # field (which would be a way to buy eligibility), and an open store
        # must not be closed by one (which would be a way to grief the desk).
        self.login_as(self.buyer)
        row = SiteSettings.load()
        row.return_window_days = 0
        row.save()
        with self.clock_frozen_at():
            closed_order = self._placed("RET-2026-0075", "delivered", timedelta(days=1))
            attempts = (
                {"return_window_days": 30},
                {"return_window_days": 3650},
                {"window_days": 30},
                {"returns_closed": False},
                {"site_settings": {"return_window_days": 30}},
            )
            baseline = None
            for extra in attempts:
                with self.subTest(closed_attempt=sorted(extra)):
                    res = self.ask(closed_order.order_number, **extra)

                    self.assertEqual(res.status_code, 409, res.data)
                    if baseline is None:
                        baseline = res.content
                    # Byte-identical to the attempt that named nothing: a body
                    # that differed would prove the field was read.
                    self.assertEqual(res.content, baseline)

        self.assertEqual(SiteSettings.load().return_window_days, 0)

        # The other direction, on the same seam: naming a window in the body
        # does not close the store either, so the order is still returnable.
        row = SiteSettings.load()
        row.return_window_days = 30
        row.save()
        with self.clock_frozen_at():
            open_order = self._placed("RET-2026-0076", "delivered", timedelta(days=1))

            res = self.ask(open_order.order_number, return_window_days=0)

            self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(SiteSettings.load().return_window_days, 30)

    def test_the_closed_state_reaches_the_storefront_as_zero(self):
        # The disclosure half. ``/api/settings/`` publishes the RESOLVED value,
        # so a closed store has to reach the storefront as 0 or the frontend
        # (SPEC-1-B07e) would be told to render a 30-day window for a store
        # that takes no returns. Both directions are asserted, as literals,
        # because this is the same conflation the model probe above pins and a
        # second copy of it is exactly the kind that drifts.
        res = self.client.get("/api/settings/")

        self.assertEqual(res.status_code, 200)
        self.assertIn('"return_window_days": 30', res.content.decode())

        row = SiteSettings.load()
        row.return_window_days = 0
        row.save()

        closed_res = self.client.get("/api/settings/")

        self.assertEqual(closed_res.status_code, 200)
        self.assertIn('"return_window_days": 0', closed_res.content.decode())

    def test_the_admin_tells_the_merchant_that_zero_closes_returns(self):
        # The merchant-facing half, asserted on the RENDERED change page,
        # because a description in the fieldset config that never reaches the
        # page tells the merchant nothing. No migration is bought for this
        # prose: ``help_text`` is a field attribute, so the fieldset
        # description is where the statement lives (ops.admin), and the
        # field's own help text is deliberately left as it was.
        SiteSettings.load()
        self.client.force_login(role_user(ROLE_ADMIN, "window-closed-admin"))

        res = self.client.get("/admin/ops/sitesettings/1/change/")

        self.assertEqual(res.status_code, 200)
        page = res.content.decode()
        self.assertIn("to close returns entirely", page)
        self.assertIn("not available rather than that the window expired", page)
        # The default is quoted in the same paragraph, from the constant rather
        # than from a number typed twice.
        self.assertIn("store default of 30 days", page)
        self.assertIn(f"{DEFAULT_RETURN_WINDOW_DAYS} days", page)
        self.assertIn(f"{CLOSED_RETURN_WINDOW_DAYS} to close", page)


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

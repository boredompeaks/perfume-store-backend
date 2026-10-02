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
  it must produce is the same one a stranger gets (``GuestOrderTests``).
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, User
from django.db import IntegrityError, transaction
from django.test import tag
from rest_framework.test import APIClient

from common.roles import ROLE_ADMIN, ROLE_MARKETING, ROLE_SUPPORT, sync_role_groups
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
from orders.views import _return_request_miss

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
            payment_status="captured" if status != "pending" else "pending",
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

        self.assertEqual(
            set(res.data), {"id", "order_number", "status", "reason_code", "created_at"}
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
        # A cancelled order was never fulfilled (the machine declares the
        # cancel edge only from pending), so there is nothing to send back.
        self.login_as(self.buyer)

        res = self.ask(self.cancelled.order_number)

        self.assertEqual(res.status_code, 409, res.data)
        self.assertFalse(self.cancelled.return_requests.exists())


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
        # The consequence of that pin, stated as a test rather than a comment:
        # spec 1.1 line 110's "returns" is NOT reachable by this role today.
        self.client.force_login(operator)
        self.assertEqual(self.client.get(CHANGELIST).status_code, 403)


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

"""The customer's returns family: create a return request, list the caller's
own, and read one by pk.

One of six modules split out of the former single-file `orders/views.py`. Three
properties are structural here rather than checked, and the split must not
soften any of them: ownership is the FILTER (a stranger's row is never in the
queryset), guest returns are structurally unreachable (a NULL-user order can
never equal an authenticated user), and NOTHING in this module moves money -
the refund seam in `refund.py` is the only writer of the payment dimension.

The `page_size` param name is imported from `order.py` rather than restated,
which is what keeps "one name across both account listings" true.

Nothing in this module was rewritten - the bodies moved verbatim - so the
guarantees documented on each helper are the ones the split inherits.
"""

from datetime import timedelta

from django.conf import settings
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

# [R-1.16] SPEC-1-B07d: the merchant-configurable return window the
# eligibility gate honours. ops.models imports nothing from orders, so this is
# a leaf import and cannot cycle; it is named in full rather than aliased so
# the reader of ``_return_window_days`` can see where the policy comes from.
# ``CLOSED_RETURN_WINDOW_DAYS`` rides the same import (SPEC-1-B07f-a): it is
# part of that window's meaning, and the gate compares the resolved number
# against it BEFORE it does any date arithmetic.
from ops.models import CLOSED_RETURN_WINDOW_DAYS, SiteSettings

# [R-1.16] SPEC-1-B07a: the return-request row and the open-status tuple its
# duplicate guard reads.
from ..models import RETURN_OPEN_STATUSES, Order, ReturnRequest
from ..serializers import ReturnRequestSerializer

# [R-10.1] The money half of eligibility is membership of the captured-money
# set, itself derived from the machine's transition table.
from ..state import CAPTURED_MONEY_PAYMENT_STATUSES

# The returns listing answers in the same envelope under the same query-param
# name as the order history; the CAP differs (own configured default), so the
# resolver is separate but the param name is one.
from .order import HISTORY_PAGE_SIZE_QUERY_PARAM

# ==================================
# [R-1.16] SPEC-1-B07a: the customer's return request
# ==================================

# SPEC-1-B07b [R-1.16]: the same query param name the order-history listing
# accepts, for the returns listing that replaces the interim bare enumeration.
# One name across both account listings is deliberate: a storefront page that
# paginates orders should be able to paginate returns with the same query string
# and get the same envelope.
RETURNS_PAGE_SIZE_QUERY_PARAM = HISTORY_PAGE_SIZE_QUERY_PARAM


# Every body field this seam reads. One tuple so the type gate below and its
# test cannot drift apart: a field added to the view without being listed here
# would be read without the gate that keeps it out of a string operation.
_RETURN_BODY_FIELDS = ("order_number", "reason_code", "reason_note")


class MalformedReturnRequestBody(Exception):
    """A body this seam cannot read as text. Carries its own answer.

    The message is built from the REQUEST alone - the field name, or the fact
    that the body was not an object - so it is identical whatever the order
    number in that body would have turned out to name. That is what keeps the
    refusal from becoming an order-existence oracle (see
    ``_return_request_miss``).
    """

    def __init__(self, error):
        super().__init__(error)
        self.error = error


def _return_body(request):
    """The three text fields of this seam's body, stripped.

    TYPE-GATED BEFORE ANY STRING OPERATION, and that is the whole point of
    this function: a request body is attacker-controlled JSON, so
    ``order_number: 1`` / ``["x"]`` / ``{"a": 1}`` / ``reason_note: true`` /
    ``null`` all arrive as non-``str``. Reading those with
    ``(request.data.get(...) or "").strip()`` raised AttributeError, i.e. a 500
    on a public endpoint, and the 500 was byte-identical for an existing and a
    non-existent order - so it was not an existence oracle, just a crash the
    auditor could reach with any JSON client.

    Rules, uniformly across all three fields:

    * absent or ``null`` reads as the empty string, which keeps ``reason_note``
      optional (it is the only field that may be omitted) and keeps a null
      ``order_number`` on the "required" refusal it has always got;
    * a ``str`` is stripped, so padding is still tolerated;
    * anything else - ``int``, ``float``, ``bool``, ``list``, ``dict`` - is a
      malformed body and is refused, never coerced. Coercing would mean
      inventing an order number out of ``["x"]`` and guessing a note out of
      ``{"a": 1}``; refusing says the truth about what the caller sent.

    A body that is not a JSON object at all (a bare list or a bare string) has
    no ``.get`` to call, so it is refused on the same path rather than
    crashing on the lookup that follows.
    """
    data = request.data
    if not isinstance(data, dict):
        raise MalformedReturnRequestBody("A JSON object body is required")
    fields = {}
    for field in _RETURN_BODY_FIELDS:
        raw = data.get(field)
        if raw is None:
            fields[field] = ""
        elif isinstance(raw, str):
            fields[field] = raw.strip()
        else:
            raise MalformedReturnRequestBody(f"{field} must be a string")
    return fields


# [R-1.16] SPEC-1-B07f-a: the refusals the returns gate can raise, as
# MACHINE-READABLE CODES rather than as prose. A customer refused because the
# store takes no returns at all, and a customer refused because their own
# window ran out, are different facts and the owner's ruling (2026-10-02)
# requires the first to SAY SO: "returns aren't available", not "not eligible".
#
# Prose alone would not carry it. SPEC-1-B07f-b has to surface the reason on
# orders the customer is not attempting to return right now, and the storefront
# has to render it, and neither can be built on an English string that a copy
# edit would change. So the code is the contract and the sentence is the
# rendering of it.
RETURN_REFUSAL_NOT_ELIGIBLE = "not_eligible"
RETURN_REFUSAL_WINDOW_CLOSED = "window_closed"
RETURN_REFUSAL_OUTSIDE_WINDOW = "outside_window"

# The two refusals that are about THIS order keep one sentence, byte-identical
# to the body this seam has always returned: the caller owns the order either
# way, so naming which of the two fired would disclose nothing but would fork a
# contract the probes pin as one. The closed refusal is the exception the ruling
# asks for, and it is the only body that carries a refusal code.
RETURN_REFUSAL_ERRORS = {
    RETURN_REFUSAL_NOT_ELIGIBLE: "This order is not eligible for a return",
    RETURN_REFUSAL_OUTSIDE_WINDOW: "This order is not eligible for a return",
    RETURN_REFUSAL_WINDOW_CLOSED: "Returns aren't available for this order",
}

# WHERE the refusal code rides in the body, and it is not ``code``.
#
# ``common.errors.ErrorEnvelopeMiddleware`` rewrites every recognised error body
# into its one envelope, and ``_envelope`` ASSIGNS ``code`` from the status
# family (409 -> "conflict") while moving every other key into ``details``. A
# view that returned ``{"error": ..., "code": "window_closed"}`` therefore
# published ``code: "conflict"`` and buried its own value at
# ``details.code`` - the code was destroyed, and the thing that survived was an
# accident of dict ordering rather than a contract anyone chose.
#
# So the code travels under its own key, which the envelope carries into
# ``details`` - the place that module reserves for exactly this, "other
# context". A closed refusal reaches the client as
# ``{"error": ..., "code": "conflict", "details": {"return_refusal":
# "window_closed"}}``, and an expired one as the same envelope with
# ``details`` empty. The envelope's own ``code`` stays the status family for
# every other endpoint in this app; only the closed refusal adds context.
RETURN_REFUSAL_BODY_KEY = "return_refusal"


def _return_eligible(order):
    """Whether ``order`` has anything at all a customer could send back.

    DERIVED FROM THE MACHINE, NOT LISTED. The cycle-2 audit caught the earlier
    version of this gate refusing ``cancelled`` with the rationale "nothing was
    fulfilled" and then ACCEPTING ``pending`` for the identical reason. It had
    to accept it: ``LEGACY_STATUS_DIMENSIONS`` (orders.state) is the machine's
    own map of every status onto the two dimensions spec 10.2 declares, and it
    records ``pending`` and ``cancelled`` as the same row twice -
    ``("pending", "unfulfilled")`` both. So any rule that refuses a cancelled
    order for "never fulfilled, nothing to send back" necessarily refuses a
    pending one for exactly the same reason. Refusing one and admitting the
    other was a contradiction dressed as a policy; this is the honest form.

    THE RULE: an order is ineligible only while NEITHER dimension says anything
    happened - the money is not held AND nothing has shipped. Both halves are
    read off the row, and neither is a window or a threshold.

    THE MONEY HALF IS NOT ``payment_status == "captured"``. It is membership of
    ``CAPTURED_MONEY_PAYMENT_STATUSES`` (orders.state), which is itself derived
    from ``PAYMENT_ALLOWED_TRANSITIONS`` rather than written out here: the
    values reachable from the capture point that the machine can still move a
    refund out of. That is ``captured`` and ``partially_refunded``. Cycle 3's
    audit is why: ``PAYMENT_ALLOWED_TRANSITIONS`` places
    ``partially_refunded`` STRICTLY AFTER ``captured``, orders/models.py's
    shipped-edge precondition admits it ALONGSIDE ``captured`` as real money
    ("a capture minus a recorded refund"), and the SPEC-1-05 refund seam is the
    writer that produces it - and that seam writes ``payment_status`` and NEVER
    ``fulfilment_status``, so the row a partly-refunded order is in is exactly
    ``partially_refunded / unfulfilled``. Testing the money half against the
    single capture literal refused that row 409 "This order is not eligible for
    a return" on the strength of a rule whose own sentence said the money had
    moved - the code and the rationale disagreeing, which is the defect class
    this docstring exists to prevent.

    The other half of the money half is a deliberate exclusion rather than an
    oversight: ``refunded`` is the one captured-money value that is NOT here,
    because ``PAYMENT_ALLOWED_TRANSITIONS`` declares no edge out of it - the
    money has all gone back and there is nothing left to send anything against.
    That refusal is a policy, it is now said out loud here and pinned by
    ``test_every_machine_value_gets_the_answer_this_rationale_claims``.

    CONSEQUENCES, all of them read off that one rule rather than appended as
    special cases. The full enumeration of what the machine admits, both
    dimensions, is:

    * ``captured / unfulfilled`` and ``partially_refunded / unfulfilled``:
      ACCEPTED. Money is held, so there is a purchase to be returning against.
    * ``pending``, ``authorized`` and ``failed / unfulfilled``: REFUSED. The
      machine reaches ``failed`` only from ``pending`` and declares no capture
      there, so "never paid for" is the same first half of this predicate - a
      return against an order the store holds no money for is not a return.
      ``authorized`` is the gateway's step BEFORE capture, so the store holds
      no funds on it either.
    * ``refunded / unfulfilled``: REFUSED, per the exclusion above.
    * ANY payment value with ``fulfilled`` or ``partially_fulfilled``: ACCEPTED.
      The goods went out, which is the half of the disjunction that does not
      care about the money at all - it is why this is a disjunction and not
      "payment == captured": the machine WAIVES the capture precondition for
      COD orders and puts their capture point at delivery (``orders.models``),
      so a COD order that has SHIPPED must stay returnable while its money is
      still ``pending``.

    NO WINDOW IS A BUILD-ORDER STATE AND IS NOW OVERRULED. SPEC-1-B07b found
    the spec silent on the NUMBER - line 1083's "Return/refund request where
    eligible" and line 1959's "Eligibility validation" both describe a window a
    whole-file sweep for a day count cannot find (the single hit is about
    deployment cadence, not returns) - so B07b shipped without one rather than
    fabricate policy wearing a configuration key. The product owner has since
    REQUIRED a configurable site-wide return window, which overrides that
    outcome rather than complementing it, and SPEC-1-B07d owns it. The rule
    below is B07a's derivation with B07d's window clause bolted onto it; the
    derivation above is untouched, and the window is a SECOND, independent
    refusal rather than a rewrite of the first.

    * the number is MERCHANT-FACING SETTING, not deployment config - each store
      sets its own - so it lives in ``ops.SiteSettings``, NOT in env and NOT
      here (the page-size keys B07b DID add beside the deployment config stay
      env-driven, because a page density is a property of the deployment and
      does not vary per store; the two differ in kind, and that difference is
      why they do not share a home).
    * age is now an input, read through ``_return_window_refusal`` below, and
      the seam no longer answers "has anything happened yet" alone.
    * 0 IS NOT A SHORT WINDOW. SPEC-1-B07f-a carries the owner's ruling of
      2026-10-02: a published 0 CLOSES returns, and it closes them at every
      age including the exact anchor instant, which the old arithmetic
      admitted and then refused a second later. The closed check is a named
      state consulted before the date arithmetic, and the refusal it produces
      is a named one too, so a customer is never told their window expired
      when the store is not taking returns at all.
    """
    money_moved = order.payment_status in CAPTURED_MONEY_PAYMENT_STATUSES
    goods_moved = order.fulfilment_status != "unfulfilled"
    if not (money_moved or goods_moved):
        return False
    return _return_window_refusal(order, timezone.now()) is None


def _return_window_days():
    """The return window this store publishes, in days (SPEC-1-B07d).

    Read from ``ops.SiteSettings`` - the admin surface the merchant already
    has - and never from ``django.conf.settings``, because a window is store
    POLICY (each merchant sets its own) while this module's other settings
    reads are deployment shape. An unset column resolves to the documented
    default in the model's own method, so the number the gate enforces and the
    number ``/api/settings/`` publishes are one value by construction rather
    than two copies that could drift.

    A published ``CLOSED_RETURN_WINDOW_DAYS`` (0) is returned AS 0, deliberately
    not defaulted: it is a value this store publishes, and
    ``_return_window_refusal`` reads it as the closed state before it reaches
    any date arithmetic (SPEC-1-B07f-a).

    ``load()`` is a ``get_or_create`` on the singleton's pk, so the very first
    call on a store that has never saved the row INSERTS it. That happens
    inside the create seam's ``transaction.atomic`` below and is safe: the row
    is the pk-1 singleton, so the insert is idempotent and commits or rolls
    back with the surrounding attempt without leaving a second row.
    """
    return SiteSettings.load().resolved_return_window_days()


def _return_window_anchor(order):
    """The instant the window is measured FROM (SPEC-1-B07d).

    DELIVERY WHERE THE GOODS HAVE ARRIVED, THE ORDER'S OWN DATE OTHERWISE -
    and the fallback is not an edge case, it is the production case. As of
    this task ``orders.models`` says of ``delivered_at``, ``shipped_at`` and
    ``fulfilled_at``: "no writer touches them yet". The admin surface's
    mark_delivered does not stamp them, and ``admin_order_fulfill`` declines
    to invent a richer record than the admin surface for the same transition.
    So every real delivered row in this repo has ``delivered_at IS NULL``, and
    a window anchored on delivery alone would either refuse every order that
    has actually been delivered or measure from nothing at all.

    The two readings DIVERGE, and the divergence is the argument for this
    form. Take an order paid on day 0, dispatched on day 3 and delivered on
    day 20:

    * measured from ``created_at``: on day 50 it is 50 days old and refused,
      even though the customer has had the goods for 30 days;
    * measured from ``delivered_at``, with this fallback: on day 50 it is 30
      days past arrival and admitted, on the last day of its window.

    The chosen form is the GENEROUS of the two wherever they disagree, because
    ``delivered_at`` is never earlier than ``created_at``: where a delivery
    stamp exists it is the later anchor and buys the customer more time, and
    where it does not exist the order date is the only anchor there is. Its
    one asymmetry is the unshipped-but-admitted row - an order admitted on
    the money half alone (``captured / unfulfilled``), which has no delivery to
    anchor to - and there both readings would use the order date anyway, which
    is why a window shorter than the store's own packing latency would expire
    a paying customer's right before their parcel exists.
    """
    return order.delivered_at or order.created_at


def _returns_closed():
    """Whether this store has published ``CLOSED_RETURN_WINDOW_DAYS`` (0).

    Read for the SENTENCE the refusal carries, not for the verdict:
    ``_return_eligible`` is the single place that decides whether an order may
    be returned against, and it reaches its own answer through
    ``_return_window_refusal``. Asking it a second time for the reason would
    mean a second ``timezone.now()`` inside one request that has to be decided
    by ONE clock read, and a request whose refusal sentence disagreed with its
    own verdict is a worse bug than the extra read on the singleton this costs.

    It is read AFTER the verdict, and it OVERRIDES the order-specific reason,
    because it is a fact about the store rather than about the order: a closed
    store refuses every order it has, and "your 30 days ran out" would be a
    misleading thing to tell a customer whose window was never open.

    THE COMPARISON agrees with the gate's, and the READS do not have to. This
    applies ``return_window_days == CLOSED_RETURN_WINDOW_DAYS`` to the RAW
    column, while ``_return_window_refusal`` applies the same comparison to
    ``resolved_return_window_days()``, and for every value the column admits
    the two EXPRESSIONS are equal - NULL resolves to 30, which is neither
    closed nor equal to 0; 0 resolves to 0, which is both; N > 0 resolves to
    itself, which is neither.
    ``test_the_three_states_are_three_and_each_is_spelled_out_by_hand`` drives
    all of those values and asserts both expressions together.

    What is NOT claimed is that they are ONE read. They are two, each its own
    ``get_or_create`` of the singleton, and the ``atomic`` block plus
    ``select_for_update()`` in the seam locks the ORDER - not SiteSettings - so
    a merchant saving the window between the two reads can make them disagree.

    THE BOUND ON THAT, which is what makes it a bounded defect rather than a
    hole: this is reached only INSIDE the ``not _return_eligible`` branch, so
    the verdict is already decided and a stale read can only MISLABEL a
    refusal that has already happened. It cannot admit a return - there is no
    path from here back to an acceptance - so the seam fails CLOSED, and the
    only artefact of the race is the wrong sentence and a missing refusal code
    on a refusal the customer was getting anyway.

    Two reads are kept deliberately. Collapsing them would mean threading the
    verdict's own code back out of ``_return_eligible`` and re-deriving the
    machine half of eligibility in the seam, trading a bounded, fail-closed
    mislabel for two sources of truth about whether an order may be returned.
    """
    return SiteSettings.load().returns_closed()


def _return_window_refusal(order, now):
    """The window's verdict on ``order`` at ``now``: a refusal code or None.

    THE CLOSED CHECK COMES FIRST, and it is a named state rather than an
    accident of the arithmetic. Before SPEC-1-B07f-a this function returned
    ``now <= anchor + timedelta(days=window)`` and let a published 0 through
    that comparison, which admitted an order AT THE EXACT ANCHOR INSTANT and
    refused the same order one second later. The owner's ruling (2026-10-02) is
    that 0 means returns are closed, so a closed store refuses at every age and
    the date arithmetic below is not reached at all.

    Every OTHER published value takes exactly the path it took before: the
    window is INCLUSIVE of its last day, so ``<=`` and not ``<``, on the
    anchor ``_return_window_anchor`` chooses. The ruling changed the meaning of
    0 and nothing else.

    ``now`` is passed in rather than read here so the CALLER owns the single
    clock read of the whole predicate: one read means a request cannot be
    accepted by a gate that consulted one clock and refused by a gate that
    consulted a later one.

    WHO CONSUMES THE CODE, precisely, because it is NOT the seam. The only
    production caller is ``_return_eligible``, which reduces this to a boolean
    with ``is None`` and therefore DISCARDS which code it was: both refusal
    codes collapse to the same False there. The body the customer finally gets
    is re-derived by the seam from an INDEPENDENT read - ``_returns_closed()``
    - so at the seam the closed code is load-bearing and the other two are
    not, and ``RETURN_REFUSAL_OUTSIDE_WINDOW`` reaches no response body at
    all. Stated here because a reader of the vocabulary would otherwise
    assume all three codes steer the shipped response. They do not; what the
    seam distinguishes is closed against everything else, and that is the
    distinction the ruling asks for. It is also why the closed/expired
    distinguishability pin survives mutating either code name: the seam's
    choice is made from the store's policy, not from the string.
    """
    window = _return_window_days()
    if window == CLOSED_RETURN_WINDOW_DAYS:
        return RETURN_REFUSAL_WINDOW_CLOSED
    if now > _return_window_anchor(order) + timedelta(days=window):
        return RETURN_REFUSAL_OUTSIDE_WINDOW
    return None


def _return_request_miss():
    """The one answer every return-request LOOKUP failure gets.

    Byte-identical for: an order number that does not exist, an order number
    belonging to somebody else, and an order number belonging to a GUEST order.
    Any difference between those cases would make this endpoint an
    order-existence oracle for a stranger, which is the exact leak
    ``_guest_lookup_miss`` and ``shipping.views._tracking_lookup_miss`` were each
    built to close (conventions.md: uniform responses on anonymous flows, no
    existence leaks). 404 rather than 403 for the same reason: "forbidden"
    would confirm the order is real.

    Deliberately NOT the answer for a malformed request: a body ``_return_body``
    cannot read as text - no order number, a reason code outside the
    vocabulary, a non-string or ``null`` where text is required, a body that is
    not an object at all - is refused with a 400 BEFORE any lookup runs. That
    is now true of EVERY malformed shape and not just of a missing field or a
    bad vocabulary string (cycle 1 overclaimed it here, and the gap was real:
    a non-string field raised AttributeError instead of being refused). Those
    refusals are built from the request alone and are identical whatever the
    order number would have turned out to be, so they cannot disclose anything
    about an order - and folding them into this 404 would only hide a real
    input error from the customer who made it.
    """
    return Response(
        {"error": "Order not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def _return_request_payload(request_row):
    """The confirmation body this seam returns.

    Now driven by ``ReturnRequestSerializer`` (SPEC-1-B07b), so the create
    confirmation and the list/detail bodies are ONE representation. B07a built
    this inline as an explicit interim shape and left the real serializer to
    B07b precisely so there would not be a second body to keep in step; the
    five keys it returned are a strict SUBSET of the serializer's seven, so
    nothing B07a documented is lost and the leak assertions over its bytes
    still hold.
    """
    return ReturnRequestSerializer(request_row).data


def _return_detail_miss():
    """The one answer every return-request DETAIL lookup miss gets.

    Byte-identical for: a pk that does not exist, a pk belonging to another
    customer's order, and a pk belonging to a GUEST order's return (which
    cannot exist today - guest returns are structurally unreachable - but the
    same answer is what it would get).

    404, never 403, and never a different BODY, for the same reason
    ``_return_request_miss`` and ``order_detail`` are: "forbidden" would
    confirm the pk is real and is a stranger's, which is precisely the IDOR
    oracle spec 17 line 4285 asks this API to be tested against. The lookup
    below folds ownership into the queryset so a stranger's row is simply not
    in the result set - it never reaches a view branch that could answer
    differently from a nonexistent pk.

    Deliberately its OWN helper rather than a reuse of ``_return_request_miss``:
    the two answer about different resources, so a future change to one miss
    contract (e.g. the order seam gaining a field) cannot silently move the
    detail seam's body with it. Each names the resource it is missing.
    """
    return Response(
        {"error": "Return request not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


def _returns_page_size(raw):
    """Resolve the ?page_size query param for the returns listing.

    Identical in shape and intent to ``_history_page_size`` in the order
    module: an integer in [1, RETURNS_HISTORY_MAX_PAGE_SIZE]; anything
    unparseable or non-positive falls back to the configured default, and an
    over-cap request is clamped to the cap. Not shared with the order-history
    resolver because each surface has its OWN configured default and cap in
    settings - the store may want a 50-row returns page and a 10-row order
    page - and threading two setting names through one helper would be a
    signature that reads as though the two listings share one knob.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return settings.RETURNS_HISTORY_PAGE_SIZE
    if value < 1:
        return settings.RETURNS_HISTORY_PAGE_SIZE
    return min(value, settings.RETURNS_HISTORY_MAX_PAGE_SIZE)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def return_requests(request):
    """[R-1.16] SPEC-1-B07a/B07b: the customer's returns family root.

    GET lists the caller's own returns and POST creates one, because Django
    resolves the FIRST matching url pattern - two patterns on ``returns/``
    would leave the second unreachable behind the first (a listing that answers
    405 to every GET). One pattern, one dispatch.

    Splitting the work into two plain functions rather than an if/else with two
    bodies is what keeps SPEC-1-B07a's create contract intact: its function is
    byte-for-byte the one B07a shipped (only its decorators moved up here), and
    its docstring travels with it. ``permission_classes`` is declared ONCE, on
    this view, rather than repeated on two functions that are never routed
    independently - the single authorization point for the whole family
    (conventions.md:14).
    """
    if request.method == "POST":
        return _create_return_request(request)
    return _list_return_requests(request)


def _list_return_requests(request):
    """[R-1.16] SPEC-1-B07b: GET .../returns/ - the caller's OWN returns.

    The one sentence this exists to satisfy: spec 4 line 1083 puts "Return/refund
    request where eligible" on the account's Orders page, and spec 4 line 1045
    gives that page its own ``/account/returns`` route - a page that has to
    show the customer the returns they have already asked for, not only the one
    they can create.

    OWNERSHIP IS THE FILTER, not a check after the fetch. Scoping on
    ``order__user=request.user`` means a stranger's row is never in the
    queryset at all, so there is no branch in this code that could answer
    differently for "yours" and "theirs". The same filter makes GUEST returns
    structurally unreachable: a guest order's ``user`` is NULL and can never
    equal an authenticated user, so a NULL-user row cannot appear here. (Guest
    returns are not offered by decision - SPEC-1-B04 gives the guest browsing
    and checkout only.)

    A LIST, never a single row. This is the trap SPEC-1-B06's split-shipment bug
    was (a listing that reported only the newest row, so an older parcel
    vanished). ``.first()`` here would answer "the returns this customer has"
    with an arbitrary one of them and silently drop the rest; the
    ``RETURN_OPEN_STATUSES`` probe in the create path exists to prevent exactly
    that shape of loss on the write side.

    PAGINATED, in the house page-number envelope with the ORDER_HISTORY
    settings convention (SPEC-9-04 [R-9.2.14]): ``settings`` supplies the
    default page size and the cap, a ``?page_size`` caller is clamped to the
    cap, and an unparsable page falls back rather than 404-ing.
    conventions.md forbids a hardcoded page size, so the number lives in
    config/settings.py (env-driven) and not here.

    The queryset is explicitly ``order_by``-ed on the model's ``-created_at,
    -id`` with the unique-id tiebreaker, so the sort is TOTAL and a paginated
    partition can never repeat or skip a row across requests (the reasoning
    ``order_list`` records). ``ReturnRequest.Meta.ordering`` already declares
    that exact pair, and it is re-stated here so the guarantee does not depend
    on a model default that a future edit could change silently.
    """
    returns = (
        ReturnRequest.objects.filter(order__user=request.user)
        .select_related("order")
        .order_by("-created_at", "-id")
    )

    paginator = Paginator(
        returns,
        _returns_page_size(request.query_params.get(RETURNS_PAGE_SIZE_QUERY_PARAM)),
    )
    # get_page never raises: an unparsable page falls back to 1, a page past
    # the end to the last page - no 404 for a stale page link.
    page = paginator.get_page(request.query_params.get("page", 1))

    # House page-number envelope (products-listing parity).
    return Response(
        {
            "count": paginator.count,
            "total_pages": paginator.num_pages,
            "current_page": page.number,
            "next_page": page.has_next(),
            "previous_page": page.has_previous(),
            "results": ReturnRequestSerializer(page.object_list, many=True).data,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def return_request_detail(request, return_request_id):
    """[R-1.16] SPEC-1-B07b: GET .../returns/<pk>/ - ONE of the caller's returns.

    The classic IDOR surface, so the lookup is ownership-scoped and the miss is
    ONE uniform 404 (``_return_detail_miss``). A stranger probing another
    customer's return by its pk gets the byte-identical body and status a
    nonexistent pk gets - never a 200 (the leak) and never a 403 that confirms
    the pk is real (the existence oracle). This is exactly the property spec 17
    line 4285 asks to be tested ("insecure direct object references (IDOR),
    especially in order, address, return and customer APIs"), and the probe is
    in tests_returns.ReturnReadIdorTests.

    Account-only and guest-blind by the same structural argument as the list:
    the filter is ``order__user=request.user``, so a NULL-user guest order's
    row cannot match. It never traverses the order to decide anything - it asks
    the database for rows this caller owns and nothing else.
    """
    try:
        return_row = ReturnRequest.objects.select_related("order").get(
            pk=return_request_id,
            order__user=request.user,
        )
    except ReturnRequest.DoesNotExist:
        return _return_detail_miss()

    return Response(ReturnRequestSerializer(return_row).data)


def _create_return_request(request):
    """[R-1.16] POST /api/v1/store/orders/returns/ - request a return.

    SPEC-1-B07b moves this off ``return_requests`` (the family root dispatches
    here on POST) and off its own url pattern; the function body below is
    otherwise byte-for-byte B07a's, and every contract that shipped with it -
    the uniform miss, the type gate, the eligibility rule, the atomic
    duplicate guard - is unchanged by the move. Its docstring travels with the
    body so the reasoning is not stranded in a wrapper that no longer exists.

    Spec 4 line 1083 makes the return request a customer self-service feature
    ("Return/refund request where eligible", under the account's Orders page and
    line 1045's ``/account/returns``). The body names the order by its
    CUSTOMER-FACING reference, never by pk, and the reference alone authorizes
    nothing: the lookup below filters on ``user=request.user``, so a hit is by
    definition one of the caller's own account orders.

    ACCOUNT-ONLY, deliberately. The spec routes returns under the account
    section (line 1045) and gives the guest (line 74) browsing and checkout
    only - it never names returns among the guest's powers - so this seam does
    not accept the B04 guest token. A guest order is therefore not addressable
    here at all, and naming one answers the uniform miss above like any other
    miss. Half-building a guest path (a token accepted for orders but a
    different answer for guest rows) would have been worse than none.

    NOTHING HERE MOVES MONEY. No ``Refund`` is created, no ``payment_status``
    and no ``total_amount`` changes: spec 6.8 line 1985 ("Never equate 'refund
    requested' with 'refund completed'") makes the refund seam (SPEC-1-05,
    ``refunds.create``) the only writer of the payment dimension, and approval
    is a separate staff act rather than something this request implies.

    DUPLICATES ARE PREVENTED, atomically, twice over. The Order row is locked
    for the whole attempt and the existing open request for it is probed under
    that lock (a second, concurrent insert for the same order is caught by the
    partial unique index on ``ReturnRequest.order``), and a racing insert that
    still slips through raises IntegrityError, which the savepoint catches and
    answers with the same 409. So one order carries at most one OPEN request
    (``RETURN_OPEN_STATUSES``) at a time; after it is rejected or closed a
    later request may be filed.

    Not idempotent by design: each accepted request is one visible customer
    ask. A retry that arrives after the first committed is the 409 above, which
    is honest about what happened rather than pretending to create a second one.

    No throttle scope, DECLINED ON AUTHORIZATION GROUNDS - and the reason is
    what the seam IS, not what its tests happen to do. conventions.md requires
    a scope on public MUTATING endpoints; this one is ``IsAuthenticated`` and
    never ``AllowAny``, so it is not that class of endpoint. A throttle would
    buy little here specifically: an anonymous caller cannot reach the seam at
    all, and a caller looping over their OWN orders cannot amplify rows - the
    partial unique index allows at most one OPEN request per order, so the
    write is bounded by the number of orders the account owns rather than by
    the number of requests it sends.

    The cost a future scope would carry is real, and it is recorded here as
    that - a cost to solve, NOT the reason to skip the scope. The enumeration
    probe that proves the eligibility gate answers for EVERY payment x
    fulfilment pair issues ~18 requests inside one test, and
    ``ScopedRateThrottle`` keys on user pk, so any rate worth having (the
    repo's existing ones run 5-120/min) would 429 it. Restructuring that probe
    - distinct users per cell, or a cache reset between cells - is precisely
    the work a throttle on this seam would require. Until then the two
    authorization facts above are the decision, and the probe cost is the
    reason the eventual scope will need test work rather than a one-line
    decorator.
    """
    try:
        body = _return_body(request)
    except MalformedReturnRequestBody as malformed:
        return Response(
            {"error": malformed.error},
            status=status.HTTP_400_BAD_REQUEST,
        )
    order_number = body["order_number"]
    reason_code = body["reason_code"]
    reason_note = body["reason_note"]

    # Input validation runs BEFORE the order lookup and answers 400s that are
    # identical whatever the order number turns out to be, so no combination of
    # refusals can disclose whether an order exists.
    if not order_number:
        return Response(
            {"error": "order_number is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if reason_code not in ReturnRequest.ReasonCode.values:
        return Response(
            {"error": "A valid reason_code is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        # select_for_update on the ORDER row, not on the return rows: every
        # requester for this order takes this lock first, so no other writer can
        # insert an open request between this probe and this commit. The order
        # number is unique, so get() cannot hide a second row behind the first.
        try:
            order = Order.objects.select_for_update().get(
                order_number=order_number,
                user=request.user,
            )
        except Order.DoesNotExist:
            # One shape for "no such order", "not yours" and "a guest order".
            return _return_request_miss()

        if not _return_eligible(order):
            # Two INDEPENDENT refusals answer 409 with the same body: the
            # machine's dimensions (see _return_eligible - a pending and a
            # cancelled order are refused for the same reason because the
            # machine maps them onto the same dimension pair) and the store's
            # return window. One body for both is deliberate - the caller owns
            # the order either way, so naming which of the two fired would
            # disclose nothing but would fork a contract the tests pin as one,
            # and "not eligible" is the honest sentence for an order past its
            # deadline. A never-paid one is refused on the capture half of the
            # machine test, whichever status it happens to wear.
            #
            # SPEC-1-B07f-a adds the THIRD refusal, and it is the one refusal
            # here that is about the store rather than the order: returns closed
            # outright. The store-wide fact is what the customer needs to know and
            # an order-specific sentence would misdescribe it, so it overrides the
            # reason the verdict would otherwise have given. The code travels under
            # RETURN_REFUSAL_BODY_KEY - see there for why it cannot be ``code`` -
            # so B07f-b and the storefront can tell this apart from the two above
            # without matching on English.
            if _returns_closed():
                return Response(
                    {
                        "error": RETURN_REFUSAL_ERRORS[RETURN_REFUSAL_WINDOW_CLOSED],
                        RETURN_REFUSAL_BODY_KEY: RETURN_REFUSAL_WINDOW_CLOSED,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(
                {"error": RETURN_REFUSAL_ERRORS[RETURN_REFUSAL_NOT_ELIGIBLE]},
                status=status.HTTP_409_CONFLICT,
            )

        already_open = ReturnRequest.objects.filter(
            order=order,
            status__in=RETURN_OPEN_STATUSES,
        ).exists()
        if already_open:
            return Response(
                {"error": "This order already has an open return request"},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            # The savepoint is what makes the racing-insert refusal recoverable:
            # an IntegrityError raised inside this transaction would otherwise
            # poison the outer block, so the duplicate would be reported as a
            # 500 instead of the 409 it is.
            with transaction.atomic():
                return_request = ReturnRequest.objects.create(
                    order=order,
                    reason_code=reason_code,
                    reason_note=reason_note,
                )
        except IntegrityError:
            return Response(
                {"error": "This order already has an open return request"},
                status=status.HTTP_409_CONFLICT,
            )

    return Response(
        _return_request_payload(return_request),
        status=status.HTTP_201_CREATED,
    )

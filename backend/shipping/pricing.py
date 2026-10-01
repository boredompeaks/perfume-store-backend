"""Server-side shipping pricing (SPEC-1-B05 [R-1.07]).

ONE resolver, two callers: the storefront estimate (spec 9.1 line 2703,
`GET /store/shipping/estimate`) and checkout. They share this module so a
destination cannot price one way on the estimate and another way at
checkout, and so the guest and account checkout paths - which are the same
view - price identically by construction rather than by two copies agreeing.

Three rules, in this order, and they are deliberately NOT collapsed into one:

1. A named method that does not resolve is a REFUSAL. Never a zero: a caller
   who names something odd must not talk the server into a free shipment.
2. A destination no active rate serves is a REFUSAL, for the same reason.
   "No matching rate" is a store configuration or serviceability fact, and
   answering it with a free shipment is how a store loses money quietly.
3. Only "the store has configured no shipping at all" answers with no
   charge. That is a different fact from (2) - shipping was never switched
   on - and it is the only silent zero in this module. It stays a zero
   because refusing the order is a different decision than pricing one, but
   it is no longer SILENT: the checkout caller logs a WARNING when it
   records one, `ops.services.get_health` reports `shipping_configured`
   for a merchant to read, and `manage.py seed_shipping_methods` is the
   documented way to end the state. None of those three change what this
   function returns.

Two orders of precedence, for two different questions: INSIDE one method the
narrowest matching rate wins over the cheapest (a regional rate is written to
override the national one), while ACROSS methods the cheapest wins (a
customer who expressed no preference is choosing by price, and must not be
handed the expensive option because it happens to be the more narrowly
targeted one). Both are pinned by tests.

Money is Decimal end to end (conventions.md:15): the rate, the threshold and
the quoted amount are all Decimals and every amount is quantized with the
shared helper before it is returned, so no path compares or serializes a
carry-over of extra decimal places.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db.models import Q

from common.money import quantize_money

from .models import ShippingMethod, ShippingRate

# One message per refusal, so the client learns WHICH question it got wrong
# (a bad method code vs an unserviceable destination) without this endpoint
# becoming an oracle over anything private: both are public catalogue facts.
UNKNOWN_METHOD_MESSAGE = "The selected shipping method is not available"
UNSERVICEABLE_MESSAGE = (
    "Shipping is not available for this destination. "
    "Please check your delivery address."
)


class ShippingUnavailable(Exception):
    """No active rate can serve this submission.

    Raised, never returned as a zero, and mapped to a 400 by both callers.
    It carries no store data beyond the two public messages above.
    """


@dataclass(frozen=True)
class ShippingQuote:
    """One priced delivery option for one destination.

    ``amount`` is what the customer is charged for shipping; ``free_shipping``
    says whether the configured free-shipping threshold produced it, so a
    caller can render "free" without re-deriving the rule. ``method_id`` is
    the server-side key checkout stores on the order - the client-facing
    identity is ``method_code``, and the estimate response never carries the
    row id.
    """

    method_code: str
    method_name: str
    method_id: int
    amount: Decimal
    free_shipping: bool


def shipping_configured():
    """Whether the store has switched shipping on at all: one active method.

    The distinction this exists for. "No shipping configured" is not
    "destination unserviceable", and answering the second with the first is
    exactly the silent free shipment this module refuses to ship.
    """
    return ShippingMethod.objects.filter(is_active=True).exists()


def _geography_conditions(region, postal_code):
    """The two SQL conditions a rate's geography rules translate to.

    A wildcard is the empty string on the rate, so the prefix condition is
    "the rate's prefix is empty OR it is one of the destination's own
    prefixes". Enumerating the destination's prefixes (at most 11 for a
    10-character postal code) is what turns a prefix MATCH into something
    the database can answer, instead of fetching every rate and comparing
    strings in Python.
    """
    prefixes = {postal_code[:length] for length in range(len(postal_code) + 1)}
    return (
        Q(region__iexact="") | Q(region__iexact=region),
        Q(postal_code_prefix="") | Q(postal_code_prefix__in=prefixes),
    )


def _candidate_rates(*, region, postal_code, method_code=None, lock=False):
    """Every active rate that can serve this destination.

    The destination is normalized HERE rather than at each call site, so every
    caller gets it: checkout passes the raw ``state``/``pincode`` the customer
    typed, and a stray trailing space would otherwise match no rate at all -
    a legitimate order refused as an unserviceable destination.

    ``lock`` adds the row lock checkout needs: a staff edit to a rate that
    commits while a checkout is pricing must not interleave with it, so the
    amount an order is charged is one committed value rather than a torn
    read. It is a no-op on SQLite, exactly like every other
    ``select_for_update`` in this codebase (there it cannot be tested at the
    database level, only at the query level).
    """
    region = (region or "").strip()
    postal_code = (postal_code or "").strip()
    region_condition, prefix_condition = _geography_conditions(region, postal_code)
    rates = ShippingRate.objects.select_related("method").filter(
        Q(is_active=True, method__is_active=True) & region_condition & prefix_condition
    )
    if method_code is not None:
        rates = rates.filter(method__code=method_code)
    if lock:
        rates = rates.select_for_update()
    # Deterministic input order so the winner never depends on the database's
    # freedom to return rows in any order it likes.
    return list(rates.order_by("pk"))


def _specificity(rate):
    """How narrowly a rate pins itself to the destination: naming both the
    region and a postal-code prefix is the narrowest, a wildcard the widest.

    Specificity beats price on purpose, and only WITHIN one method. A store
    that prices one region below the rest must see that region's rate apply
    there; picking the cheapest matching rate inside the method instead
    would let a national rate undercut every regional one it was written to
    override. Across methods it is the reverse - see ``_method_rank``.
    """
    return int(bool(rate.region)) + int(bool(rate.postal_code_prefix))


def _rate_rank(rate):
    """The total order over one method's matching rates: narrowest first,
    then cheapest, then a stable tiebreak on the method code and the row id.

    The last two keys exist so two rates that are equally specific and
    equally priced still resolve the same way on every call - an order
    priced by a coin toss is an order nobody can reconcile against the rate
    table afterwards.
    """
    return (-_specificity(rate), rate.amount, rate.method.code, rate.pk)


def _method_rank(rate):
    """The total order over METHODS for a destination the customer expressed
    no preference about: cheapest first, then the method code, then the row
    id.

    Deliberately NOT specificity-first. Specificity ranks the rates INSIDE a
    method; ranking methods by it would hand an unsolicited customer the
    most narrowly-targeted option whenever a store happens to price its
    regional express rate above its national standard one - the customer
    would be upgraded to the expensive option they did not ask for. Across
    methods the customer is choosing by price, so price decides.
    """
    return (rate.amount, rate.method.code, rate.pk)


def _best_rate_per_method(rates):
    """One rate per method: each method's own ``_rate_rank`` winner.

    Partial serviceability needs no special case: a method with no rate for
    this destination simply contributes no candidate, so it is absent from
    the result rather than refused on its own.
    """
    winners = {}
    for rate in rates:
        current = winners.get(rate.method_id)
        if current is None or _rate_rank(rate) < _rate_rank(current):
            winners[rate.method_id] = rate
    return winners


def _charge(rate_amount, merchandise_total):
    """``(amount, free_shipping)``: the rate, or zero when the configured
    free-shipping threshold is met.

    Spec 6.9 line 2005 ("Free-shipping rules"). The threshold compares
    against the MERCHANDISE total - what the customer pays for goods, after
    any discount - because that is the number the rule is written about, and
    it is the number checkout already computed before it asks this module
    for a price. Both operands are Decimals and the result is quantized.
    """
    threshold = settings.SHIPPING_FREE_THRESHOLD
    if threshold is not None and merchandise_total >= threshold:
        return quantize_money(Decimal("0.00")), True
    return quantize_money(rate_amount), False


def _quote(rate, merchandise_total):
    amount, free_shipping = _charge(rate.amount, merchandise_total)
    return ShippingQuote(
        method_code=rate.method.code,
        method_name=rate.method.name,
        method_id=rate.method_id,
        amount=amount,
        free_shipping=free_shipping,
    )


def _method_resolves(method_code):
    """Whether the named method exists and is offerable.

    Checked separately from the rate resolution so an unserviceable
    destination and an unrecognised method get their own answers: "that
    method is not available" and "we do not ship there" are different
    corrections for the customer to make.
    """
    return ShippingMethod.objects.filter(
        code=method_code,
        is_active=True,
    ).exists()


def quote_shipping(
    *,
    region,
    postal_code,
    merchandise_total,
    method_code=None,
    lock=False,
):
    """The one shipping amount for this destination, or None when the store
    has configured no shipping at all.

    ``merchandise_total`` is the discounted merchandise total the caller
    already computed; it feeds the free-shipping rule only. ``method_code``
    is the client's delivery OPTION (an identifier), never an amount: the
    price below is the server's answer to it, which is what keeps the
    checkout money path server-side (the SPEC-1-B04 P1 class of bug).

    Raises :class:`ShippingUnavailable` for a method that does not resolve
    and for a destination no active rate serves. Returns None ONLY for the
    unconfigured store, which the caller records as an explicit
    no-shipping-charge order rather than as a rate of zero - and which the
    caller is expected to make visible, because a store that never notices
    is a store that never charges for delivery.
    """
    if method_code is not None and not _method_resolves(method_code):
        raise ShippingUnavailable(UNKNOWN_METHOD_MESSAGE)

    rates = _candidate_rates(
        region=region,
        postal_code=postal_code,
        method_code=method_code,
        lock=lock,
    )
    if not rates:
        if not shipping_configured():
            return None
        raise ShippingUnavailable(UNSERVICEABLE_MESSAGE)

    if method_code is not None:
        # A method the customer named is priced on its own terms: its own most
        # specific matching rate, then its own cheapest. Nobody else's rates
        # compete for the answer.
        return _quote(min(rates, key=_rate_rank), merchandise_total)

    # No preference expressed, so the cheapest method that can reach the
    # destination wins, each judged on its own best rate.
    return _quote(
        min(_best_rate_per_method(rates).values(), key=_method_rank),
        merchandise_total,
    )


def shipping_options(*, region, postal_code):
    """Every method that can serve this destination, cheapest first.

    What the storefront's delivery-option step renders (spec 9.1 line 3142,
    "Select delivery option"). One quote per method - each method's own best
    matching rate, chosen by the same rank checkout uses - so a method that
    cannot serve the destination is simply absent while a destination no
    method serves raises, exactly as checkout would.

    No merchandise total is accepted here, and that is the point: the
    estimate has no cart to read, so it reports the PLAIN rate and the
    free-shipping rule is applied only at checkout, where the discounted
    total is known. The client is therefore never asked to compute money and
    can never be quoted a number the server would not charge.
    """
    if not shipping_configured():
        return []

    rates = _candidate_rates(region=region, postal_code=postal_code)
    if not rates:
        raise ShippingUnavailable(UNSERVICEABLE_MESSAGE)

    quotes = [
        _quote(rate, Decimal("0.00")) for rate in _best_rate_per_method(rates).values()
    ]
    return sorted(quotes, key=lambda quote: (quote.amount, quote.method_code))

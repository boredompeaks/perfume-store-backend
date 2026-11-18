"""Server-side shipping pricing, shared by the storefront estimate and checkout.

An unresolvable method or destination is a refusal, never a zero; only "no
shipping configured at all" quotes zero. Inside a method the most specific
rate wins, across methods the cheapest does.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db.models import Q

from common.money import quantize_money

from .models import ShippingMethod, ShippingRate

# Distinct public messages so a refusal names which question was wrong.
UNKNOWN_METHOD_MESSAGE = "The selected shipping method is not available"
UNSERVICEABLE_MESSAGE = (
    "Shipping is not available for this destination. "
    "Please check your delivery address."
)


class ShippingUnavailable(Exception):
    """No active rate can serve this submission.

    Raised, never returned as a zero, and mapped to a 400 by both callers; it
    carries no store data beyond the public messages.
    """


@dataclass(frozen=True)
class ShippingQuote:
    """One priced delivery option for one destination.

    ``free_shipping`` says the threshold produced the amount; ``method_id`` is
    the server-side key checkout stores, while ``method_code`` is the client
    identity.
    """

    method_code: str
    method_name: str
    method_id: int
    amount: Decimal
    free_shipping: bool


def shipping_configured():
    """Whether the store has switched shipping on at all.

    "No shipping configured" is not "destination unserviceable" — conflating
    them is the silent free shipment this module refuses.
    """
    return ShippingMethod.objects.filter(is_active=True).exists()


def _geography_conditions(region, postal_code):
    """The two SQL conditions a rate's geography rules translate to.

    A wildcard is the empty string on the rate, and enumerating the
    destination's prefixes turns a prefix match into a query the database can
    answer.
    """
    prefixes = {postal_code[:length] for length in range(len(postal_code) + 1)}
    return (
        Q(region__iexact="") | Q(region__iexact=region),
        Q(postal_code_prefix="") | Q(postal_code_prefix__in=prefixes),
    )


def _candidate_rates(*, region, postal_code, method_code=None, lock=False):
    """Every active rate that can serve this destination.

    The destination is normalized here so a stray space cannot refuse a valid
    order; ``lock`` gives checkout a committed rate while staff may be editing
    it.
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
    """How narrowly a rate pins itself to the destination.

    Specificity beats price only WITHIN one method, so a regional rate is not
    undercut by the national rate it was written to override.
    """
    return int(bool(rate.region)) + int(bool(rate.postal_code_prefix))


def _rate_rank(rate):
    """The total order over one method's matching rates: narrowest, then
    cheapest, then a stable tiebreak.

    The tiebreak keys exist so two equal rates always resolve the same way — an
    order priced by a coin toss cannot be reconciled.
    """
    return (-_specificity(rate), rate.amount, rate.method.code, rate.pk)


def _method_rank(rate):
    """The total order over methods when the customer expressed no preference.

    Cheapest first, deliberately not specificity: ranking by it would hand an
    unsolicited customer the expensive regional option.
    """
    return (rate.amount, rate.method.code, rate.pk)


def _best_rate_per_method(rates):
    """One rate per method: each method's own ``_rate_rank`` winner."""
    winners = {}
    for rate in rates:
        current = winners.get(rate.method_id)
        if current is None or _rate_rank(rate) < _rate_rank(current):
            winners[rate.method_id] = rate
    return winners


def _charge(rate_amount, merchandise_total):
    """``(amount, free_shipping)``: the rate, or zero when the threshold is met.

    The threshold compares against the discounted merchandise total the caller
    already computed, and the result is quantized.
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

    Checked separately so an unknown method and an unserviceable destination
    get their own answers.
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
    """The one shipping amount for this destination, or None when the store has
    configured no shipping at all.

    ``method_code`` is the client's delivery option, never an amount; raises
    ``ShippingUnavailable`` for an unresolved method or unserviceable
    destination.
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
        # A named method is priced on its own terms: no other method competes.
        return _quote(min(rates, key=_rate_rank), merchandise_total)

    return _quote(
        min(_best_rate_per_method(rates).values(), key=_method_rank),
        merchandise_total,
    )


def shipping_options(*, region, postal_code):
    """Every method that can serve this destination, cheapest first.

    Each method contributes its own best rate; no merchandise total is accepted
    because the estimate has no cart, so the free-shipping rule applies only at
    checkout.
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

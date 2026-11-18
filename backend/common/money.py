"""Shared money helpers.

Every money column is a 2-dp ``DecimalField``, so values are quantized before they
are compared or serialized.
"""

from decimal import ROUND_HALF_EVEN, Decimal

TWO_PLACES = Decimal("0.01")


def quantize_money(value):
    """Quantize to the 2-dp money precision with HALF_EVEN (the database
    adapter's own rounding), so in-memory and stored values agree."""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_EVEN)

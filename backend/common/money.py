"""Shared money helpers.

Every money column in this repo is ``DecimalField(max_digits=10,
decimal_places=2)`` and conventions.md requires Decimals to be quantized
to that precision before they are compared or serialized. The helper lives
in ``common`` (the shared-kernel app, like audit/permissions/roles)
because the rule is cross-app: orders computes discounts today, and any
future pricing or reporting consumer must quantize identically.
"""
from decimal import ROUND_HALF_EVEN, Decimal

TWO_PLACES = Decimal("0.01")


def quantize_money(value):
    """Quantize a money Decimal to the 2-dp money precision.

    Rounds HALF_EVEN, the rounding the database adapter applies when a
    DecimalField value is written, so the in-memory value and the stored
    value it becomes are always the same amount and no path can compare or
    serialize a carry-over of extra decimal places (F-11).
    """
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_EVEN)

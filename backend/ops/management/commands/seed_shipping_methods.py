"""SPEC-1-B05 [R-1.07]: give a fresh store a shipping configuration to charge.

The silent zero this command exists to end. `0001_initial` creates no
ShippingMethod, and until a staff member adds one, `shipping.pricing`
answers "the store has configured no shipping at all" for every destination
and checkout records a real `shipping_amount` of 0.00 with a null method.
That is correct behaviour for an unconfigured store - it is pinned by
`NoMatchingRateTests` and `CheckoutShippingCostTests` and this command does
not change any of it - but a store that never notices loses the delivery fee
on every single order without a single error. Two signals say so (a WARNING
on the checkout path, `shipping_configured` on `/health/`); this command is
the third: the fix itself.

A DEPLOYMENT step, run deliberately by an operator, never at import and
never from a migration. A data migration that invented prices would put
made-up money in every fresh database, and an import-time side effect would
write rows as a side effect of an unrelated import; a merchant's first
shipping price is a business decision an operator makes explicitly.

What it writes, and what it will never do:

* Two national methods, `standard` and `express`, each with ONE
  geography-wildcard rate (empty region and empty postal prefix match
  everywhere, which is how a national rate is expressed in this schema).
  Regional rates are the merchant's to add in the admin, where the
  geography rules are visible next to the price they override.
* **Idempotent, and never overwriting a price.** An existing method or an
  existing rate for the same geography is left exactly as it is and reported
  as `kept`, so re-running this in a deployment pipeline can never reset a
  live price to the default below. The defaults are only ever used for rows
  that do not exist yet.
* **Amounts are explicit options, not silent constants.** The defaults
  below are a starting point a merchant is expected to change (in the
  admin, or by passing these options); the help text says so, and every
  amount is validated as a non-negative money Decimal before anything is
  written, so a typo fails the command instead of storing a bad rate.

Usage: `python manage.py seed_shipping_methods [--standard-amount 49.00]
[--express-amount 149.00] [--dry-run]`

Exit status: 0 on success (including a no-op re-run and a dry run);
non-zero (`CommandError`) if an amount is not a usable non-negative money
value, so a bad flag is loud rather than a silently wrong price.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from common.money import quantize_money
from shipping.models import ShippingMethod, ShippingRate

# One national rate per method: (code, name, description, default amount).
# Kept as a table so adding a method is one row, and so the dry run and the
# real run read the identical definition.
DEFAULT_METHODS = (
    (
        "standard",
        "Standard",
        "Standard ground delivery.",
        Decimal("49.00"),
    ),
    (
        "express",
        "Express",
        "Express delivery.",
        Decimal("149.00"),
    ),
)


def _money(value, option):
    """A non-negative 2-dp Decimal for a CLI amount, or refuse by name.

    Mirrors `config.settings._env_money`'s fail-safe: anything that is not a
    usable non-negative money amount is refused outright here, because unlike
    an env default this command is writing a PRICE - a bad amount must stop
    the operator, not be quietly stored.
    """
    try:
        amount = quantize_money(Decimal(value))
    except (ArithmeticError, ValueError, TypeError):
        raise CommandError(f"{option} must be a money amount, got {value!r}")
    if not amount.is_finite() or amount < Decimal("0.00"):
        raise CommandError(
            f"{option} must be a finite amount of zero or more, got {value!r}"
        )
    return amount


class Command(BaseCommand):
    help = (
        "Create the default national shipping methods and rates for a store "
        "that has none. Idempotent: existing methods and rates are kept and "
        "never repriced."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--standard-amount",
            default=str(DEFAULT_METHODS[0][3]),
            help=(
                "Amount for the standard method, used only if it does not "
                f"already exist (default: {DEFAULT_METHODS[0][3]})."
            ),
        )
        parser.add_argument(
            "--express-amount",
            default=str(DEFAULT_METHODS[1][3]),
            help=(
                "Amount for the express method, used only if it does not "
                f"already exist (default: {DEFAULT_METHODS[1][3]})."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing anything.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        # Validated BEFORE anything else, so a bad amount cannot leave a
        # half-seeded store behind even if the atomic block were removed.
        amounts = {
            "standard": _money(options["standard_amount"], "--standard-amount"),
            "express": _money(options["express_amount"], "--express-amount"),
        }

        # Pass one is pure reads: the plan, and therefore what --dry-run
        # reports. Nothing may be written in order to describe what would be
        # written - get_or_create writes, so a dry run built from it would be
        # a lie about a command whose whole job is writing.
        plan = []
        for code, name, description, _ in DEFAULT_METHODS:
            existing = ShippingMethod.objects.filter(code=code).first()
            has_national_rate = (
                existing is not None
                and ShippingRate.objects.filter(
                    method=existing, region="", postal_code_prefix=""
                ).exists()
            )
            plan.append(
                {
                    "code": code,
                    "create_method": existing is None,
                    "name": name,
                    "description": description,
                    "create_rate": not has_national_rate,
                    "amount": amounts[code],
                }
            )

        planned_methods = sum(1 for row in plan if row["create_method"])
        planned_rates = sum(1 for row in plan if row["create_rate"])
        # Two things per method definition - the method row and its national
        # rate - so this is the count of rows the plan examined, which is what
        # "already existed" counts against.
        already_there = 2 * len(plan) - planned_methods - planned_rates

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    "Dry run: no rows written. Would create "
                    f"{planned_methods} method(s) and {planned_rates} rate(s); "
                    f"{already_there} already existed and would be left "
                    "unchanged."
                )
            )
            return

        # Pass two executes the plan. get_or_create rather than create(), so a
        # row that appeared between the two passes is kept instead of crashing
        # the command on the unique index. An existing method is fetched, not
        # updated: a merchant's own name, description and is_active flag are
        # theirs, and re-running this must not reactivate a retired method or
        # overwrite a price.
        written_methods = 0
        written_rates = 0
        for row in plan:
            if row["create_method"]:
                shipping_method, created = ShippingMethod.objects.get_or_create(
                    code=row["code"],
                    defaults={
                        "name": row["name"],
                        "description": row["description"],
                    },
                )
                written_methods += int(created)
            else:
                shipping_method = ShippingMethod.objects.get(code=row["code"])
            if row["create_rate"]:
                _, created = ShippingRate.objects.get_or_create(
                    method=shipping_method,
                    region="",
                    postal_code_prefix="",
                    defaults={"amount": row["amount"]},
                )
                written_rates += int(created)

        self.stdout.write(
            self.style.SUCCESS(
                f"Shipping seeded: {written_methods} method(s) and "
                f"{written_rates} rate(s) created, "
                f"{2 * len(plan) - written_methods - written_rates} already "
                "existed and were left unchanged."
            )
        )

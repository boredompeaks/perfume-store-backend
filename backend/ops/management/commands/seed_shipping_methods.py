"""Create the default national shipping methods and rates for a store that has none.

A deliberate deployment step, idempotent and never repricing: existing methods and rates are kept, and a bad amount fails the command.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from common.money import quantize_money
from shipping.models import ShippingMethod, ShippingRate

# One national rate per method: (code, name, description, default amount).
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
    """A non-negative 2-dp Decimal for a CLI amount, refusing anything else by name."""
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
        # Validated first, so a bad amount cannot leave a half-seeded store.
        amounts = {
            "standard": _money(options["standard_amount"], "--standard-amount"),
            "express": _money(options["express_amount"], "--express-amount"),
        }

        # Pass one is pure reads, so --dry-run reports a plan: get_or_create
        # writes, and a dry run built from it would be a lie.
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
        # Two rows per definition (the method and its national rate).
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
        # row that appeared between passes is kept instead of crashing on the
        # unique index; an existing method is fetched, not updated, so a
        # merchant's price and is_active flag survive a re-run.
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

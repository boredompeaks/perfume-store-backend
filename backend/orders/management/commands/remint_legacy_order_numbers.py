"""``manage.py remint_legacy_order_numbers [--apply --yes] [--mapping FILE]``.

Rewrites the rows whose ``order_number`` is not in the checkout format onto
it. The new value comes from the SAME ``secrets``-based generator checkout
uses, imported rather than re-implemented, so a reminted row cannot drift
from a freshly checked-out one.

This command changes a reference a customer may already be holding -- an
order-confirmation email, a support ticket, a printed invoice -- so it is
OPT-IN: the default mode is a dry run, ``--apply`` additionally requires
``--yes``, and no migration calls it. Nothing in the migration graph ever
rewrites a customer-facing identifier, and a deploy that only migrates stays
a no-op here.
"""

import csv
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from orders.models import Order
from orders.views.checkout import ORDER_NUMBER_ATTEMPTS, _generate_order_number

# The checkout format (spec 8.3), and the only shape that counts as already
# reminted. A NULL reference does not match, so a row that predates the column
# is legacy by definition and is picked up here.
ORDER_NUMBER_FORMAT = re.compile(r"^ORD-\d{4}-[A-F0-9]{8}$")


def is_legacy_number(number):
    """True when a stored reference is not in the checkout format."""
    return not number or ORDER_NUMBER_FORMAT.fullmatch(number) is None


def legacy_rows():
    """``(pk, current reference)`` for every row that needs a remint.

    Read as a stream of two columns rather than model instances: the scan is
    over every order, and only the reference matters for the decision.
    """
    return [
        (pk, number)
        for pk, number in Order.objects.order_by("pk").values_list("pk", "order_number")
        if is_legacy_number(number)
    ]


def reject_duplicate_old_numbers(olds):
    """Refuse a plan in which two rows claim the same existing reference.

    A repeated non-blank OLD number means two rows claim the reference a
    customer may be holding, so no old,new pair can be unambiguous. Blank old
    values are exempt on purpose: several rows can legitimately predate the
    column, and they carry no reference to be ambiguous about.

    New references are not checked here. The unique constraint on
    ``Order.order_number`` is what refuses a repeated new value, and
    ``_remint`` retries with a fresh candidate when it does, so a check after
    the write could only restate a guarantee the database already made.
    """
    seen = set()
    for value in olds:
        if not value:
            continue
        if value in seen:
            raise CommandError(
                f"refusing to remint: duplicate old order number {value}"
            )
        seen.add(value)


class Command(BaseCommand):
    help = (
        "Remint order numbers that are not in the ORD-YYYY-XXXXXXXX checkout "
        "format, using the checkout generator. Dry run by default; --apply "
        "rewrites customer-held references and requires --yes."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be reminted without writing (the default).",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Rewrite the legacy references (requires --yes).",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm that changing customer-held references is intended.",
        )
        parser.add_argument(
            "--mapping",
            default="",
            help=(
                "CSV path to write each old,new pair to, so support can "
                "translate a reference a customer still quotes."
            ),
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        if apply and not options["yes"]:
            raise CommandError(
                "--apply rewrites references customers may already hold; "
                "re-run with --apply --yes to confirm"
            )
        if options["mapping"] and not apply:
            raise CommandError(
                "--mapping records the numbers that changed, so it needs --apply"
            )

        rows = legacy_rows()
        if not rows:
            self.stdout.write(
                "[remint_legacy_order_numbers] no legacy order numbers found"
            )
            return

        reject_duplicate_old_numbers([number for _, number in rows])
        self.stdout.write(
            f"[remint_legacy_order_numbers] {len(rows)} order(s) not in the "
            "checkout format:"
        )
        for pk, number in rows:
            self.stdout.write(f"  #{pk} {number or '(none)'}")

        if not apply:
            self.stdout.write(
                "  dry run: no rows were written (pass --apply --yes to remint)"
            )
            return

        # One transaction for the whole repair: a bounded retry that cannot
        # find a free number must leave every row as it was, never a table
        # half-reminted into two reference formats.
        with transaction.atomic():
            changes = []
            for pk, number in rows:
                new_number = self._remint(pk)
                if new_number is None:
                    self.stdout.write(f"  #{pk} was reminted concurrently; skipped")
                    continue
                changes.append((number or "", new_number))
                self.stdout.write(f"  #{pk} {number or '(none)'} -> {new_number}")

        if options["mapping"]:
            self._write_mapping(options["mapping"], changes)
        self.stdout.write(
            f"[remint_legacy_order_numbers] reminted {len(changes)} order number(s)"
        )

    def _remint(self, pk):
        """Mint one row a fresh reference; None when it no longer needs one.

        The unique constraint on ``Order.order_number`` is the concurrency
        authority (conventions.md:17): this loop only mints candidates and
        retries a lost race. The row lock makes a concurrent run's remint
        visible, so the second run skips a row instead of overwriting it.
        """
        for _ in range(ORDER_NUMBER_ATTEMPTS):
            candidate = _generate_order_number()
            try:
                # Savepoint per attempt: a rejected candidate rolls back only
                # this update and leaves the repair transaction usable.
                with transaction.atomic():
                    order = Order.objects.select_for_update().get(pk=pk)
                    if not is_legacy_number(order.order_number):
                        return None
                    order.order_number = candidate
                    order.save(update_fields=["order_number"])
                return candidate
            except IntegrityError:
                continue
        raise CommandError(
            f"could not mint a unique order_number for order #{pk} after "
            f"{ORDER_NUMBER_ATTEMPTS} attempts; nothing was written"
        )

    def _write_mapping(self, path, changes):
        """Record ``old,new`` for every reminted row, header included."""
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["old", "new"])
            writer.writerows(changes)

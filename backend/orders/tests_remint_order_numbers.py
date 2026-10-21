"""D-05: ``manage.py remint_legacy_order_numbers``.

The repair rewrites references a customer may already hold, so the pins below
cover the gate (dry run by default, ``--apply`` needs ``--yes``), the scope
(only rows outside the checkout format, conforming rows untouched), the
generator (the checkout one, imported rather than copied), the collision
retry, the mapping CSV and the idempotent second run.
"""

import csv
import os
import shutil
import tempfile
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase

from orders.management.commands import remint_legacy_order_numbers as remint
from orders.models import Order
from orders.views import checkout as checkout_views

# Hand-written literals: a pin that recomputes its expectation from the code
# under test agrees with a wrong value from both sides.
CONFORMING = "ORD-2026-ABCDEF12"
LEGACY_SEQUENTIAL = "ORD-2026-000001"
LEGACY_SEQUENTIAL_B = "ORD-2026-000002"
FRESH = "ORD-2026-11112222"
FRESH_B = "ORD-2026-33334444"


def run_command(*argv):
    out = StringIO()
    call_command("remint_legacy_order_numbers", *argv, stdout=out)
    return out.getvalue()


class RemintTestCase(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user(username="buyer", password="x")

    def make_order(self, number):
        return Order.objects.create(
            user=self.buyer,
            full_name="R",
            phone="1",
            address="a",
            city="c",
            state="s",
            pincode="1",
            total_amount=Decimal("10.00"),
            order_number=number,
        )

    def number_of(self, order):
        order.refresh_from_db()
        return order.order_number


class RemintDryRunTests(RemintTestCase):
    def test_default_mode_is_a_dry_run_and_changes_nothing(self):
        legacy = self.make_order(LEGACY_SEQUENTIAL)
        conforming = self.make_order(CONFORMING)

        with mock.patch.object(
            remint,
            "_generate_order_number",
            side_effect=AssertionError("a dry run must not mint"),
        ):
            out = run_command()

        self.assertEqual(self.number_of(legacy), LEGACY_SEQUENTIAL)
        self.assertEqual(self.number_of(conforming), CONFORMING)
        self.assertIn(LEGACY_SEQUENTIAL, out)
        self.assertNotIn(CONFORMING, out)
        self.assertIn("dry run", out)

    def test_apply_without_yes_is_refused(self):
        legacy = self.make_order(LEGACY_SEQUENTIAL)

        with self.assertRaisesMessage(CommandError, "--apply --yes"):
            run_command("--apply")

        self.assertEqual(self.number_of(legacy), LEGACY_SEQUENTIAL)

    def test_mapping_without_apply_is_refused(self):
        self.make_order(LEGACY_SEQUENTIAL)
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = f"{directory}/mapping.csv"

        with self.assertRaisesMessage(CommandError, "--mapping"):
            run_command("--mapping", path)

        self.assertFalse(os.path.exists(path))


class RemintApplyTests(RemintTestCase):
    def test_apply_remints_only_non_conforming_rows(self):
        sequential = self.make_order(LEGACY_SEQUENTIAL)
        blank = self.make_order(None)
        conforming = self.make_order(CONFORMING)

        with mock.patch.object(
            remint,
            "_generate_order_number",
            side_effect=[FRESH, FRESH_B],
        ) as generate:
            out = run_command("--apply", "--yes")

        self.assertEqual(self.number_of(sequential), FRESH)
        self.assertEqual(self.number_of(blank), FRESH_B)
        self.assertEqual(self.number_of(conforming), CONFORMING)
        self.assertEqual(generate.call_count, 2)
        self.assertIn(f"{LEGACY_SEQUENTIAL} -> {FRESH}", out)
        self.assertIn(f"(none) -> {FRESH_B}", out)

    def test_mapping_records_every_old_new_pair(self):
        sequential = self.make_order(LEGACY_SEQUENTIAL)
        blank = self.make_order(None)
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = f"{directory}/remint.csv"

        with mock.patch.object(
            remint, "_generate_order_number", side_effect=[FRESH, FRESH_B]
        ):
            run_command("--apply", "--yes", "--mapping", path)

        with open(path, encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["old", "new"])
        self.assertEqual(
            sorted(rows[1:]),
            sorted([[LEGACY_SEQUENTIAL, FRESH], ["", FRESH_B]]),
        )
        self.assertEqual(self.number_of(sequential), FRESH)
        self.assertEqual(self.number_of(blank), FRESH_B)

    def test_second_run_is_idempotent(self):
        legacy = self.make_order(LEGACY_SEQUENTIAL)
        with mock.patch.object(remint, "_generate_order_number", side_effect=[FRESH]):
            run_command("--apply", "--yes")

        with mock.patch.object(
            remint,
            "_generate_order_number",
            side_effect=AssertionError("a conforming table must not be reminted"),
        ):
            out = run_command("--apply", "--yes")

        self.assertEqual(self.number_of(legacy), FRESH)
        self.assertIn("no legacy order numbers found", out)

    def test_collision_retries_with_a_fresh_candidate(self):
        self.make_order(CONFORMING)
        legacy = self.make_order(LEGACY_SEQUENTIAL)

        with mock.patch.object(
            remint,
            "_generate_order_number",
            side_effect=[CONFORMING, FRESH],
        ) as generate:
            run_command("--apply", "--yes")

        self.assertEqual(self.number_of(legacy), FRESH)
        self.assertEqual(generate.call_count, 2)

    def test_exhausted_retries_leave_every_row_untouched(self):
        # CONFORMING is already held by a conforming row, so every retry for
        # the second legacy row loses the unique-constraint race.
        self.make_order(CONFORMING)
        first = self.make_order(LEGACY_SEQUENTIAL)
        second = self.make_order(LEGACY_SEQUENTIAL_B)

        with mock.patch.object(
            remint,
            "_generate_order_number",
            side_effect=[FRESH] + [CONFORMING] * checkout_views.ORDER_NUMBER_ATTEMPTS,
        ):
            with self.assertRaisesMessage(CommandError, "nothing was written"):
                run_command("--apply", "--yes")

        self.assertEqual(self.number_of(first), LEGACY_SEQUENTIAL)
        self.assertEqual(self.number_of(second), LEGACY_SEQUENTIAL_B)

    def test_a_row_reminted_concurrently_is_skipped(self):
        legacy = self.make_order(LEGACY_SEQUENTIAL)

        def concurrent_remint():
            # Another run commits its own reference after this run built its
            # plan; the row lock is what lets this run see that and skip it.
            Order.objects.filter(pk=legacy.pk).update(order_number=CONFORMING)
            return FRESH

        with mock.patch.object(
            remint, "_generate_order_number", side_effect=concurrent_remint
        ):
            out = run_command("--apply", "--yes")

        self.assertEqual(self.number_of(legacy), CONFORMING)
        self.assertIn("reminted concurrently", out)

    def test_the_checkout_generator_and_attempt_bound_are_imported(self):
        self.assertIs(
            remint._generate_order_number, checkout_views._generate_order_number
        )
        self.assertEqual(
            remint.ORDER_NUMBER_ATTEMPTS, checkout_views.ORDER_NUMBER_ATTEMPTS
        )


class RemintDuplicateGuardTests(TestCase):
    def test_a_duplicate_old_number_is_refused(self):
        with self.assertRaisesMessage(CommandError, "duplicate old order number"):
            remint.reject_duplicate_numbers([LEGACY_SEQUENTIAL, LEGACY_SEQUENTIAL], [])

    def test_a_duplicate_new_number_is_refused(self):
        with self.assertRaisesMessage(CommandError, "duplicate new order number"):
            remint.reject_duplicate_numbers([LEGACY_SEQUENTIAL], [FRESH, FRESH])

    def test_blank_old_numbers_are_not_duplicates(self):
        remint.reject_duplicate_numbers(["", None, ""], [FRESH, FRESH_B])

"""Unit tests for ``scripts/pg_gate.py``'s output parsing.

Pins the three header shapes unittest emits (plain, subTest with a trailing
parameter group, docstring-bare) so a HEADER_RE change cannot silently
under-read a run; the fixtures are copies of real rendered output
(PostgreSQL 17.11, Python 3.14).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pg_gate  # noqa: E402


class HeaderParsingTests(unittest.TestCase):
    """The three shapes unittest renders for a failing test."""

    PLAIN = (
        "FAIL: test_order_number_stays_satisfied_by_its_unique_constraint "
        "(orders.tests.OrderIndexSchemaTests."
        "test_order_number_stays_satisfied_by_its_unique_constraint)"
    )
    # subTest: the parameter group follows the id on the SAME line.
    SUBTEST = (
        "FAIL: test_payment_provider_references_stay_constraint_covered "
        "(orders.tests.OrderIndexSchemaTests."
        "test_payment_provider_references_stay_constraint_covered) "
        "(column='razorpay_order_id')"
    )
    BARE = "FAIL: test_unhandled_exception_500_still_returns_the_request_id "

    def test_plain_header_yields_qualified_id(self):
        headers = pg_gate.collect_headers(self.PLAIN + "\n")
        self.assertEqual(
            headers,
            [
                (
                    "test_order_number_stays_satisfied_by_its_unique_constraint",
                    "orders.tests.OrderIndexSchemaTests."
                    "test_order_number_stays_satisfied_by_its_unique_constraint",
                )
            ],
        )

    def test_subtest_header_is_matched_and_keeps_the_parent_id(self):
        """The trailing `(column=...)` group must not hide the header.

        The id asserted here is the PARENT test: discovery collects it once."""
        headers = pg_gate.collect_headers(self.SUBTEST + "\n")
        self.assertEqual(len(headers), 1, "subTest header was not matched at all")
        self.assertEqual(
            headers[0][1],
            "orders.tests.OrderIndexSchemaTests."
            "test_payment_provider_references_stay_constraint_covered",
        )

    def test_two_subtest_headers_stay_two_headers(self):
        """Two sub-cases are two problems; the count check depends on that."""
        text = "\n".join(
            [
                self.SUBTEST,
                self.SUBTEST.replace("razorpay_order_id", "razorpay_payment_id"),
            ]
        )
        self.assertEqual(len(pg_gate.collect_headers(text + "\n")), 2)

    def test_bare_header_yields_no_qualified_id(self):
        """No id means the caller must resolve the short name, not guess."""
        headers = pg_gate.collect_headers(self.BARE + "\n")
        self.assertEqual(
            headers,
            [("test_unhandled_exception_500_still_returns_the_request_id", None)],
        )

    def test_parameter_group_is_never_promoted_to_a_test_id(self):
        """`(alpha)` is a parameter, not an id, and must not be read as one.

        Without the dotted-path restriction the params group would satisfy the
        id group and certify a test id that does not exist."""
        headers = pg_gate.collect_headers(
            "FAIL: test_x (module.Class.test_x) (alpha)\n"
        )
        self.assertEqual(headers, [("test_x", "module.Class.test_x")])
        self.assertNotIn("alpha", [qualified for _, qualified in headers])

    def test_params_only_group_leaves_the_id_unresolved(self):
        """A bare parameter group with no dotted id must yield no id at all.

        Otherwise `(column='x')` would be read as a qualified id; instead the
        short name is left for the resolver."""
        headers = pg_gate.collect_headers("FAIL: test_x (column='razorpay_order_id')\n")
        self.assertEqual(headers, [("test_x", None)])

    def test_django_log_error_lines_are_not_failure_headers(self):
        """A case-insensitive scan sweeps these in as failures.

        Django's audit logging interleaves `ERROR 2026-... django.request`
        records into the same stream unittest writes to."""
        text = "ERROR 2026-10-04 17:54:33,165 django.request Internal Server Error: /\n"
        self.assertEqual(pg_gate.collect_headers(text), [])


class SummaryParsingTests(unittest.TestCase):
    """The runner's own count, which the header count is checked against."""

    def test_expected_failures_is_not_counted_as_failures(self):
        """`expected failures=4` also matches the failures key.

        A bare key regex kept the LAST match and printed problems=5 for a
        7-problem run."""
        verdict, failures, errors = pg_gate.parse_summary(
            "FAILED (failures=6, errors=1, expected failures=4)"
        )
        self.assertEqual((verdict, failures, errors), ("FAILED", 6, 1))

    def test_green_run_reports_zero_problems(self):
        self.assertEqual(
            pg_gate.parse_summary("OK (expected failures=4)"), ("OK", 0, 0)
        )

    def test_absent_summary_is_not_zero(self):
        """Nothing is not zero: an unjudgeable log must not read as clean."""
        self.assertEqual(pg_gate.parse_summary("some unrelated output"), (None, 0, 0))


class DecodeTests(unittest.TestCase):
    """A log the gate cannot read must never read as an empty, clean set."""

    def test_utf16_bom_is_sniffed(self):
        raw = "FAILED (failures=6, errors=1)\n".encode("utf-16")
        self.assertIn("FAILED", pg_gate.decode(raw))

    def test_utf8_is_read_directly(self):
        self.assertIn("FAILED", pg_gate.decode(b"FAILED (failures=1)\n"))

    def test_undecodable_byte_does_not_lose_the_summary(self):
        """The suite's log output carries cp1252 bytes on Windows.

        A raw 0x97 appears in real captured output; decoded strictly it would
        raise and take the whole run down."""
        raw = b"junk \x97 more\nFAILED (failures=2, errors=0)\n"
        self.assertIn("FAILED (failures=2, errors=0)", pg_gate.decode(raw))


if __name__ == "__main__":
    unittest.main()

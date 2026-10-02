"""Unit tests for scripts/doc_claims.py.

Pure core only: no git, no filesystem, no network, no Django. The shell in
doc_claims.py is exercised by running it, not by mocking it.

Run from the repo root::

    python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doc_claims import (  # noqa: E402
    _path_exists,
    check_byte_integrity,
    extract_claims,
    verify_claims,
)

DOC = "backend/docs/changes.md"


def kinds(claims, kind):
    return [c for c in claims if c.kind == kind]


class ExtractTests(unittest.TestCase):
    def test_finds_backticked_test_name(self):
        claims = extract_claims("fixed `test_the_thing` today", DOC)
        names = kinds(claims, "test_name")
        self.assertEqual([c.value for c in names], ["test_the_thing"])

    def test_does_not_match_a_test_name_inside_a_longer_identifier(self):
        claims = extract_claims("see test_alpha_beta_extra_long_name", DOC)
        self.assertEqual(
            [c.value for c in kinds(claims, "test_name")],
            ["test_alpha_beta_extra_long_name"],
        )

    def test_finds_module_path(self):
        claims = extract_claims("changed `backend/orders/views.py`", DOC)
        self.assertEqual(
            [c.value for c in kinds(claims, "module_path")],
            ["backend/orders/views.py"],
        )

    def test_path_with_line_is_one_claim_not_two(self):
        claims = extract_claims("see `backend/orders/views.py:412`", DOC)
        self.assertEqual(len(kinds(claims, "path_line")), 1)
        self.assertEqual(kinds(claims, "module_path"), [])

    def test_extracts_counts(self):
        claims = extract_claims("M4 kills 7 tests and 3 fixtures", DOC)
        values = [c.value for c in kinds(claims, "count")]
        self.assertIn("7 tests", values)
        self.assertIn("3 fixtures", values)

    def test_extracts_measured_and_exactly_places(self):
        claims = extract_claims("measured 11, and it occurs at exactly 2 places", DOC)
        values = [c.value for c in kinds(claims, "count")]
        self.assertIn("measured 11", values)
        # The value is the whole matched phrase, so the plural is part of it.
        self.assertIn("exactly 2 places", values)

    def test_a_figure_claimed_by_two_patterns_is_reported_once(self):
        """One figure, one claim. Otherwise a reviewer is told to check two."""
        claims = extract_claims("it occurs at exactly 2 places", DOC)
        self.assertEqual(
            [c.value for c in kinds(claims, "count")], ["exactly 2 places"]
        )

    def test_a_spelled_out_numeral_still_counts(self):
        # The BUG-7 sentence in docs/changes.md reads "exactly ONE place", so a
        # digits-only pattern misses the very sentence this rule was written
        # for.
        claims = extract_claims("it occurs at exactly one place", DOC)
        self.assertEqual(
            [c.value for c in kinds(claims, "count")], ["exactly one place"]
        )

    def test_bare_number_is_not_a_claim(self):
        claims = extract_claims("cycle 3 of 3", DOC)
        self.assertEqual(kinds(claims, "count"), [])

    def test_extracts_universals(self):
        for phrase in (
            "no code path can rewrite it",
            "this is the only writer",
            "it can never happen",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(kinds(extract_claims(phrase, DOC), "universal"))

    def test_bare_all_is_not_treated_as_a_universal(self):
        # "all" fires on almost every honest sentence; including it would
        # bury the real findings in noise.
        self.assertEqual(kinds(extract_claims("all tests pass", DOC), "universal"), [])

    def test_self_referential_count_is_flagged(self):
        """The SPEC-1-B07d BUG-7 shape: a file counting its own occurrences."""
        claims = extract_claims(
            "the misspelling occurs at exactly ONE place in this file", DOC
        )
        self.assertEqual(len(kinds(claims, "self_count")), 1)

    def test_count_about_something_else_is_not_self_referential(self):
        claims = extract_claims("the mutation kills 6 tests", DOC)
        self.assertEqual(kinds(claims, "self_count"), [])

    def test_line_numbers_are_offset_by_first_line(self):
        claims = extract_claims("`test_x`", DOC, first_line=99)
        self.assertEqual(claims[0].line, 99)

    def test_blank_lines_are_skipped(self):
        self.assertEqual(extract_claims("\n\n   \n", DOC), [])


class VerifyTests(unittest.TestCase):
    KNOWN = {"test_real_one"}
    PATHS = {"backend/orders/views.py": 900, "backend/ops/models.py": 100}

    def verify(self, line, first_line=1):
        return verify_claims(
            extract_claims(line, DOC, first_line), self.KNOWN, self.PATHS
        )

    def test_missing_test_name_is_an_error(self):
        errors, _ = self.verify("fixed `test_does_not_exist`")
        self.assertEqual([c.kind for c in errors], ["test_name"])
        self.assertIn("no `def`", errors[0].detail)

    def test_existing_test_name_is_not_an_error(self):
        errors, warnings = self.verify("fixed `test_real_one`")
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_missing_module_path_is_an_error(self):
        errors, _ = self.verify("changed `backend/nope/gone.py`")
        self.assertEqual([c.kind for c in errors], ["module_path"])

    def test_line_past_end_of_file_is_an_error(self):
        errors, _ = self.verify("see `backend/ops/models.py:4000`")
        self.assertEqual([c.kind for c in errors], ["path_line"])
        self.assertIn("past the end", errors[0].detail)

    def test_line_inside_file_is_not_an_error(self):
        errors, _ = self.verify("see `backend/ops/models.py:40`")
        self.assertEqual(errors, [])

    def test_undecidable_claims_are_warnings_not_errors(self):
        errors, warnings = self.verify("it kills 7 tests and is always right")
        self.assertEqual(errors, [])
        self.assertEqual(sorted({c.kind for c in warnings}), ["count", "universal"])

    def test_severity_property_agrees_with_the_split(self):
        errors, _ = self.verify("fixed `test_does_not_exist`")
        self.assertEqual(errors[0].severity, "error")


class PathResolutionTests(unittest.TestCase):
    KNOWN = {
        "backend/orders/views.py": 10,
        "backend/orders/models.py": 10,
        "backend/ops/views.py": 10,
        "frontend/src/x.py": 10,
    }

    def test_exact_path_resolves(self):
        self.assertTrue(_path_exists("backend/orders/views.py", self.KNOWN))

    def test_bare_filename_resolves_when_unambiguous(self):
        self.assertTrue(_path_exists("models.py", self.KNOWN))

    def test_bare_filename_ambiguous_is_refused(self):
        # Two tracked views.py files exist, so the citation is reported rather
        # than guessed at -- and the error names the file it could not resolve.
        self.assertFalse(_path_exists("views.py", self.KNOWN))
        errors, _ = verify_claims(
            extract_claims("see `views.py:40`", DOC), set(), self.KNOWN
        )
        self.assertEqual([c.kind for c in errors], ["path_line"])
        self.assertIn("no such file: views.py", errors[0].detail)

    def test_path_line_resolves_a_bare_filename(self):
        # A path cited with a line is resolved exactly as one cited without,
        # or `views.py:40` would be an error in prose that is merely terse.
        errors, warnings = verify_claims(
            extract_claims("see `models.py:4`", DOC), set(), self.KNOWN
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_unknown_path_does_not_resolve(self):
        self.assertFalse(_path_exists("backend/orders/nope.py", self.KNOWN))


class ByteIntegrityTests(unittest.TestCase):
    BASELINE = {"control_bytes": 2, "cr": 0, "crlf_pairs": 0, "trailing_lf": True}

    def test_clean_blob_matches_baseline(self):
        blob = b"hello\nworld\n"
        result = check_byte_integrity(
            blob, {"control_bytes": 0, "cr": 0, "crlf_pairs": 0, "trailing_lf": True}
        )
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["lines"], 2)

    def test_extra_control_byte_is_caught(self):
        blob = b"hello\n\x07world\n"
        result = check_byte_integrity(blob, self.BASELINE)
        self.assertFalse(result["ok"])
        self.assertIn("control bytes", " ".join(result["problems"]))

    def test_tabs_are_not_control_bytes(self):
        # The baseline of 6 counts two real tabs; treating them as corruption
        # would make every honest commit look dirty.
        blob = b"a\tb\n"
        result = check_byte_integrity(blob, {"control_bytes": 0})
        self.assertTrue(result["ok"])

    def test_control_byte_reports_its_line(self):
        result = check_byte_integrity(b"a\nb\n\x07c\n", {})
        self.assertEqual(result["control_detail"][0]["line"], 3)
        self.assertEqual(result["control_detail"][0]["byte"], "0x07")

    def test_every_control_byte_on_a_line_is_counted(self):
        # The real backend/docs/changes.md carries THREE control bytes on line
        # 675. Recording only the first baselines that line at one byte, and a
        # second corruption there is then invisible -- which is the whole
        # defect this check exists to catch, reproduced inside the check.
        result = check_byte_integrity(b"a\x07b\x0cc\x07d\n", {})
        self.assertEqual(result["control_bytes"], 3)
        self.assertEqual(
            [entry["line"] for entry in result["control_detail"]], [1, 1, 1]
        )
        self.assertEqual(
            [entry["byte"] for entry in result["control_detail"]],
            ["0x07", "0x0c", "0x07"],
        )

    def test_crlf_conversion_is_caught(self):
        result = check_byte_integrity(b"a\r\nb\r\n", self.BASELINE)
        self.assertFalse(result["ok"])
        self.assertEqual(result["cr"], 2)

    def test_missing_trailing_newline_is_caught(self):
        result = check_byte_integrity(b"a\nb", self.BASELINE)
        self.assertFalse(result["ok"])
        self.assertFalse(result["trailing_lf"])

    def test_eaten_character_shortens_the_blob(self):
        result = check_byte_integrity(b"a\nb\n", {"lines": 3})
        self.assertFalse(result["ok"])
        self.assertIn("content was lost", " ".join(result["problems"]))

    def test_growth_is_allowed(self):
        # docs/changes.md grows by a row on every commit. Equality on the line
        # count would make the byte check unusable for the only file it covers,
        # which is why the invariant is "must not shrink", not "must match".
        result = check_byte_integrity(b"a\nb\nc\n", {"lines": 3})
        self.assertTrue(result["ok"], result["problems"])


if __name__ == "__main__":
    unittest.main()

"""Unit tests for scripts/doc_claims.py.

Pure core only: no git, no filesystem, no network, no Django. The shell in
doc_claims.py is exercised by running it, not by mocking it.

Run from the repo root::

    python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doc_claims import (  # noqa: E402
    _path_exists,
    _resolve_path,
    check_byte_integrity,
    empty_scan_notice,
    extract_claims,
    parse_counter_examples,
    resolve_base,
    verify_claims,
)
from doc_claims import FALLBACK_BASE, MIN_REASON_CHARS  # noqa: E402

DOC = "backend/docs/changes.md"

# A well-formed counter-example directive. The reason clears
# MIN_REASON_CHARS on purpose: a bare token is not a statement.
DIRECTIVE = (
    "<!-- doc-claims:absent test_retired_name (renamed-to test_real_one)"
    " ; reason: renamed in B07d, cited to report the old name -->"
)


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


class PartialPathResolutionTests(unittest.TestCase):
    """BUG-3 (TOOL-01): an app-relative citation must not be an error.

    Four apps each own a ``views.py``, so the basename fallback is ambiguous
    for all of them and prose that cites ``orders/views.py`` -- which is what
    this changelog writes several hundred times -- was reported as a missing
    file. 94 of the 121 errors on the wide base were exactly this.
    """

    KNOWN = {
        "backend/orders/views.py": 900,
        "backend/ops/views.py": 400,
        "backend/cart/views.py": 300,
        "backend/common/views.py": 120,
        "backend/orders/models.py": 200,
        "backend/orders/tests_returns.py": 2600,
        "backend/config/urls.py": 60,
    }

    def test_app_relative_path_resolves_despite_four_views_py(self):
        self.assertTrue(_path_exists("orders/views.py", self.KNOWN))
        self.assertEqual(
            _resolve_path("orders/views.py", self.KNOWN), "backend/orders/views.py"
        )

    def test_bare_basename_is_still_refused_when_ambiguous(self):
        # The regression guard on the fix: the middle pass must not make a
        # genuinely ambiguous citation resolvable.
        self.assertFalse(_path_exists("views.py", self.KNOWN))

    def test_suffix_match_needs_a_path_boundary(self):
        # "ders/views.py" is not a path suffix of anything; matching on the
        # bare string would resolve a typo to a real file, which is a gate that
        # passes on prose nobody checked.
        self.assertFalse(_path_exists("ders/views.py", self.KNOWN))
        self.assertFalse(_path_exists("s/views.py", self.KNOWN))

    def test_exact_path_still_wins(self):
        self.assertEqual(
            _resolve_path("backend/orders/views.py", self.KNOWN),
            "backend/orders/views.py",
        )

    def test_leading_dot_slash_resolves(self):
        self.assertEqual(
            _resolve_path("./orders/models.py", self.KNOWN),
            "backend/orders/models.py",
        )

    def test_ambiguous_suffix_is_still_refused(self):
        # Two apps can nest the same directory name; a suffix that matches two
        # tracked paths resolves to neither rather than to the first one found.
        index = {
            "backend/a/orders/views.py": 10,
            "backend/b/orders/views.py": 10,
        }
        self.assertFalse(_path_exists("orders/views.py", index))

    def test_resolving_a_path_does_not_disable_the_line_check(self):
        # The fix must move resolution, not skip the check: the line is still
        # verified against the file it resolved to.
        errors, warnings = verify_claims(
            extract_claims("see `orders/views.py:4000`", DOC), set(), self.KNOWN
        )
        self.assertEqual([c.kind for c in errors], ["path_line"])
        self.assertIn("past the end", errors[0].detail)
        self.assertIn("backend/orders/views.py", errors[0].detail)

        errors, warnings = verify_claims(
            extract_claims("see `orders/views.py:400`", DOC), set(), self.KNOWN
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_a_genuinely_missing_app_relative_path_is_still_an_error(self):
        errors, _ = verify_claims(
            extract_claims("changed `orders/nope.py`", DOC), set(), self.KNOWN
        )
        self.assertEqual([c.kind for c in errors], ["module_path"])


class CounterExampleDirectiveTests(unittest.TestCase):
    """BUG-4 (TOOL-01): a quoted absent name is not a claim that it exists.

    Both directions are pinned, because a directive that only ever suppresses
    is as broken as one that never does: a real missing-name claim must still
    fail, and a genuine counter-example must stop failing.
    """

    KNOWN = {"test_real_one"}
    PATHS = {"backend/orders/tests_returns.py": 2600}

    def verify(self, line, first_line=1):
        return verify_claims(
            extract_claims(line, DOC, first_line), self.KNOWN, self.PATHS
        )

    def test_a_genuine_counter_example_stops_failing(self):
        errors, warnings = self.verify(
            f"the row quoted `test_retired_name` {DIRECTIVE}"
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("counter-example", warnings[0].detail)

    def test_a_real_missing_name_still_fails(self):
        errors, _ = self.verify("fixed `test_does_not_exist`")
        self.assertEqual([c.kind for c in errors], ["test_name"])
        self.assertIn("no `def`", errors[0].detail)

    def test_a_directive_excuses_only_the_name_it_names(self):
        # THE narrowness test. Two absent names on one line, one directive.
        errors, _ = self.verify(
            f"`test_retired_name` {DIRECTIVE} and `test_also_missing`"
        )
        self.assertEqual([c.value for c in errors], ["test_also_missing"])

    def test_adding_the_word_not_does_not_stop_the_error(self):
        # The directive cannot be faked by editing the sentence, which is the
        # difference between a statement and a wording.
        errors, _ = self.verify("there is not a test named `test_does_not_exist`")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_a_directive_does_not_excuse_a_bad_path_on_the_same_line(self):
        errors, _ = self.verify(f"`test_retired_name` {DIRECTIVE} in gone.py")
        self.assertEqual(sorted(c.kind for c in errors), ["module_path"])

    def test_a_witness_that_does_not_exist_is_refused(self):
        # Otherwise a directive is a general escape hatch: swap one missing
        # name for another missing name and the claim is suppressed.
        liar = DIRECTIVE.replace("test_real_one", "test_also_missing")
        errors, _ = self.verify(f"`test_retired_name` {liar}")
        self.assertEqual([c.value for c in errors], ["test_retired_name"])
        self.assertIn("does not exist either", errors[0].detail)

    def test_retired_needs_no_witness(self):
        directive = (
            "<!-- doc-claims:absent test_retired_name (retired)"
            " ; reason: the pin was retired in B07d and is quoted as absent -->"
        )
        errors, _ = self.verify(f"`test_retired_name` {directive}")
        self.assertEqual(errors, [])

    def test_a_reason_under_the_floor_is_not_a_directive(self):
        terse = (
            "<!-- doc-claims:absent test_retired_name (renamed-to test_real_one)"
            " ; reason: renamed -->"
        )
        self.assertEqual(parse_counter_examples(terse), {})
        errors, _ = self.verify(f"`test_retired_name` {terse}")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_a_renamed_to_without_a_witness_is_not_a_directive(self):
        malformed = (
            "<!-- doc-claims:absent test_retired_name (renamed-to)"
            " ; reason: renamed in B07d and cited here -->"
        )
        self.assertEqual(parse_counter_examples(malformed), {})
        errors, _ = self.verify(f"`test_retired_name` {malformed}")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_an_unknown_kind_is_not_a_directive(self):
        malformed = DIRECTIVE.replace("(renamed-to", "(was-renamed-to")
        self.assertEqual(parse_counter_examples(malformed), {})
        errors, _ = self.verify(f"`test_retired_name` {malformed}")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_a_plain_mention_of_the_keyword_is_not_a_directive(self):
        self.assertEqual(parse_counter_examples("doc-claims:absent test_x"), {})
        self.assertEqual(parse_counter_examples("<!-- doc-claims:absent -->"), {})

    def test_a_directive_is_bound_to_its_own_line(self):
        text = (
            f"quoted `test_retired_name` {DIRECTIVE}\n"
            "and `test_retired_name` again, uncited-as-absent"
        )
        errors, warnings = verify_claims(
            extract_claims(text, DOC), self.KNOWN, self.PATHS
        )
        self.assertEqual([c.line for c in errors], [2])

    def test_two_directives_for_one_name_that_disagree_honour_nothing(self):
        other = (
            "<!-- doc-claims:absent test_retired_name (misspelling-of"
            " test_also_missing) ; reason: a different story entirely -->"
        )
        errors, _ = self.verify(f"`test_retired_name` {DIRECTIVE} {other}")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_the_claim_carries_the_reason_so_the_log_can_show_it(self):
        claims = kinds(
            extract_claims(f"`test_retired_name` {DIRECTIVE}", DOC), "test_name"
        )
        self.assertIn("renamed-to", claims[0].detail)
        self.assertIn("B07d", claims[0].detail)

    def test_an_honoured_claim_is_not_an_error(self):
        _, warnings = self.verify(f"`test_retired_name` {DIRECTIVE}")
        self.assertEqual(warnings[0].severity, "warn")

    def test_parse_returns_the_witness(self):
        parsed = parse_counter_examples(DIRECTIVE)
        self.assertEqual(parsed["test_retired_name"].witness, "test_real_one")
        self.assertEqual(parsed["test_retired_name"].kind, "renamed-to")
        self.assertGreaterEqual(
            len(parsed["test_retired_name"].reason), MIN_REASON_CHARS
        )


class BaseResolutionTests(unittest.TestCase):
    """BUG-5 (TOOL-01): the base has to move with the promotion."""

    def test_flag_wins_over_everything(self):
        base, source = resolve_base("v1", {"DOC_CLAIMS_BASE": "v2", "CI": "true"})
        self.assertEqual(base, "v1")
        self.assertEqual(source, "--base")

    def test_env_var_is_used_when_no_flag(self):
        base, source = resolve_base(None, {"DOC_CLAIMS_BASE": "abc123"})
        self.assertEqual(base, "abc123")
        self.assertEqual(source, "$DOC_CLAIMS_BASE")

    def test_ci_falls_back_to_the_pr_base_ref(self):
        base, source = resolve_base(None, {"CI": "true", "GITHUB_BASE_REF": "master"})
        self.assertEqual(base, "origin/master")
        self.assertIn("GITHUB_BASE_REF", source)

    def test_pr_base_ref_is_ignored_outside_ci(self):
        base, _ = resolve_base(None, {"GITHUB_BASE_REF": "master"})
        self.assertEqual(base, FALLBACK_BASE)

    def test_ci_without_a_pr_base_ref_falls_back(self):
        base, source = resolve_base(None, {"CI": "true"})
        self.assertEqual(base, FALLBACK_BASE)
        self.assertEqual(source, "default")

    def test_a_blank_env_var_is_ignored_not_used(self):
        base, source = resolve_base(None, {"DOC_CLAIMS_BASE": "   "})
        self.assertEqual(base, FALLBACK_BASE)
        self.assertEqual(source, "default")

    def test_local_default_is_unchanged(self):
        base, source = resolve_base(None, {})
        self.assertEqual(base, FALLBACK_BASE)
        self.assertEqual(source, "default")

    def test_no_source_is_ever_empty(self):
        # An unlabelled base is how "which diff did that even scan?" goes
        # unanswerable in a CI log three weeks later.
        for env in ({}, {"CI": "true"}, {"CI": "true", "GITHUB_BASE_REF": "main"}):
            with self.subTest(env=env):
                self.assertTrue(resolve_base(None, env)[1])

    def test_empty_scan_is_silent_when_files_were_scanned(self):
        self.assertEqual(empty_scan_notice(["a.md"], "b", "HEAD"), "")

    def test_empty_scan_says_so_and_names_the_fix(self):
        notice = empty_scan_notice([], "origin/spec-comp", "HEAD")
        self.assertIn("0 file(s)", notice)
        self.assertIn("origin/spec-comp", notice)
        self.assertIn("DOC_CLAIMS_BASE", notice)


class BaselineShapeTests(unittest.TestCase):
    """The baseline holds byte FACTS. A claims entry in it is the defect."""

    BYTE_FACTS = {"control_bytes", "cr", "crlf_pairs", "lines", "trailing_lf"}

    def test_baseline_records_byte_facts_and_nothing_else(self):
        path = Path(__file__).resolve().parent.parent / "doc_claims_baseline.json"
        baseline = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(baseline, "baseline is empty; byte integrity is unchecked")
        for doc, recorded in baseline.items():
            with self.subTest(doc=doc):
                self.assertIsInstance(recorded, dict)
                self.assertLessEqual(set(recorded), self.BYTE_FACTS)
                for key, value in recorded.items():
                    self.assertIsInstance(value, (int, bool), f"{doc}.{key}")

    def test_baseline_grew_no_new_top_level_sections(self):
        # A "claims" or "known_errors" key here would be an allowlist wearing
        # a baseline's name, and it is what this whole script exists to stop.
        path = Path(__file__).resolve().parent.parent / "doc_claims_baseline.json"
        baseline = json.loads(path.read_text(encoding="utf-8"))
        for doc in baseline:
            self.assertIn(".", doc, f"{doc} is not a documentation path")


if __name__ == "__main__":
    unittest.main()

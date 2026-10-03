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
    _inside_any_span,
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
from doc_claims import RE_MODULE_PATH, RE_PATH_LINE, RE_REASON_REFERENCE  # noqa: E402

DOC = "backend/docs/changes.md"

# A well-formed counter-example directive. The reason clears MIN_REASON_CHARS
# and names a task id, because both are required: the floor keeps it a fragment
# and the id keeps it traceable (BUG-B).
DIRECTIVE = (
    "<!-- doc-claims:absent test_retired_name (renamed-to test_real_one)"
    " ; reason: renamed in SPEC-1-B07d, cited to report the old name -->"
)
# A second well-formed directive for a DIFFERENT absent name. Two of these on
# one line is over quota (BUG-A).
OTHER_DIRECTIVE = (
    "<!-- doc-claims:absent test_second_absent (misspelling-of test_real_one)"
    " ; reason: SPEC-1-B07d misspelled it, quoted here to report the wrong"
    " spelling -->"
)
# The shape the ledger actually uses for byte-eaten path fragments (BUG-C).
PATH_DIRECTIVE = (
    "<!-- doc-claims:absent ests_returns.py (corrupted-fragment-of"
    " tests_returns.py) ; reason: c873b27 ate the leading byte, reported here"
    " as the corruption it is -->"
)


def retired_directive(name="test_retired_name", task="SPEC-1-B07d"):
    return (
        f"<!-- doc-claims:absent {name} (retired) ; reason: the pin was retired"
        f" in {task} and is quoted here to report that -->"
    )


def parse(line):
    """The accepted half of a parse, for the many malformed cases."""
    return parse_counter_examples(line)[0]


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


class TestStemmedModuleVersusMethodTests(unittest.TestCase):
    """BUG-1 (TOOL-01): a `test_`-stemmed MODULE is not a `test_*` METHOD.

    ``RE_TEST_NAME`` matches the ``test_`` inside ``test_e2e_concurrency.py``,
    so a prose citation of the file was reported as a missing ``def`` for the
    name ``test_e2e_concurrency``. The gate was red on correct prose, which is
    worse than no gate: the two available responses are to delete the check or
    to stop citing real files.

    The rule is one rule, in both directions. A token that is part of a cited
    PATH is a path claim and is verified as a path -- file resolves, line in
    range. A token that is not part of a path is still verified as a method.
    Every test below that says "still an error" is the loophole guard: the fix
    must not become a way to dress a real error up as a path.
    """

    KNOWN = {
        "backend/orders/views.py": 900,
        "tests/test_e2e_concurrency.py": 300,
    }

    def test_a_test_stemmed_module_path_is_not_also_a_method_claim(self):
        # The real failing case, for the RIGHT reason: the path_line claim is
        # still extracted and still verified, and no method claim is invented
        # out of the middle of the filename.
        claims = extract_claims("see `tests/test_e2e_concurrency.py:114`", DOC)
        self.assertEqual([c.kind for c in claims], ["path_line"])
        errors, warnings = verify_claims(claims, set(), self.KNOWN)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_a_test_stemmed_module_without_a_line_is_a_path_claim(self):
        claims = extract_claims("see `test_e2e_concurrency.py`", DOC)
        self.assertEqual([c.kind for c in claims], ["module_path"])
        errors, _ = verify_claims(claims, set(), self.KNOWN)
        self.assertEqual(errors, [])

    def test_a_method_name_next_to_a_path_is_still_a_method_claim(self):
        # Resolution is scoped to the PATH TOKEN. A real method cited in the
        # same sentence is untouched by the path beside it.
        claims = extract_claims(
            "`test_oversell_race` pins `tests/test_e2e_concurrency.py:114`", DOC
        )
        self.assertEqual(
            [c.value for c in claims if c.kind == "test_name"],
            ["test_oversell_race"],
        )
        errors, _ = verify_claims(claims, {"test_oversell_race"}, self.KNOWN)
        self.assertEqual(errors, [])

    def test_a_missing_method_name_is_still_an_error(self):
        errors, _ = verify_claims(
            extract_claims("killed `test_never_existed` today", DOC),
            set(),
            self.KNOWN,
        )
        self.assertEqual([c.kind for c in errors], ["test_name"])
        self.assertIn("no `def` for this name", errors[0].detail)

    def test_a_method_name_missing_while_a_real_path_sits_beside_it(self):
        # The loophole, in its plainest form: a TRUE citation on the line must
        # not launder a FALSE one next to it.
        errors, _ = verify_claims(
            extract_claims(
                "`tests/test_e2e_concurrency.py:114` does NOT pin `test_never_existed`",
                DOC,
            ),
            set(),
            self.KNOWN,
        )
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_a_non_existent_test_stemmed_path_is_still_an_error(self):
        # Dropping the `.py` is not an escape: the method check is still there
        # and the path check is now the only one, so it must fire.
        errors, _ = verify_claims(
            extract_claims("see `test_no_such_file.py`", DOC), set(), self.KNOWN
        )
        self.assertEqual([c.kind for c in errors], ["module_path"])
        self.assertIn("no such file in the tree", errors[0].detail)

    def test_a_non_existent_test_stemmed_path_with_a_line_is_still_an_error(self):
        errors, _ = verify_claims(
            extract_claims("see `test_no_such_file.py:114`", DOC), set(), self.KNOWN
        )
        self.assertEqual([c.kind for c in errors], ["path_line"])
        self.assertIn("no such file: test_no_such_file.py", errors[0].detail)

    def test_an_out_of_range_line_in_a_resolved_module_is_still_an_error(self):
        errors, _ = verify_claims(
            extract_claims("see `tests/test_e2e_concurrency.py:99999`", DOC),
            set(),
            self.KNOWN,
        )
        self.assertEqual([c.kind for c in errors], ["path_line"])
        self.assertIn("past the end", errors[0].detail)

    def test_a_module_that_holds_no_test_defs_at_all_is_not_demanded_one(self):
        # The mechanism, stated independently of the `test_` stem: the demand
        # for a `def` comes from the METHOD reading, and a cited path never
        # gets that reading.
        claims = extract_claims("see `backend/orders/views.py:12`", DOC)
        self.assertEqual([c.kind for c in claims], ["path_line"])

    def test_a_path_prefix_that_merely_starts_with_test_keeps_its_own_span(self):
        # `tests/` before `test_` is part of the path token, so nothing inside
        # the filename leaks out as a name; but a token AFTER the citation is
        # back to normal scanning.
        claims = extract_claims(
            "at `tests/test_e2e_concurrency.py:114`, `test_after` still needs checking",
            DOC,
        )
        self.assertEqual(
            [c.value for c in claims if c.kind == "test_name"], ["test_after"]
        )

    def test_a_py_suffix_on_a_non_test_name_is_unchanged(self):
        # Nothing about the rule is specific to `test_`: any cited path token
        # consumes itself, which is what `module_path` already meant.
        claims = extract_claims("see `orders/views.py:12`", DOC)
        self.assertEqual([c.kind for c in claims], ["path_line"])

    def test_a_trailing_dot_ends_no_path_and_re_opens_the_method_check(self):
        # The loophole this fix opened, found by attacking it: `x.py.bak`
        # matched the old module pattern under `\b`, resolved to the real
        # `x.py`, and with the name no longer demanded as a method the whole
        # citation passed -- a file nobody has, cited as if it existed. A dot
        # after `.py` is the front of a longer name, so the longer name is
        # what gets claimed, and it does not resolve.
        errors, _ = verify_claims(
            extract_claims("see `test_e2e_concurrency.py.bak`", DOC), set(), self.KNOWN
        )
        self.assertEqual([c.kind for c in errors], ["module_path"])
        self.assertEqual(errors[0].value, "test_e2e_concurrency.py.bak")

    def test_a_trailing_dot_before_a_line_is_not_a_path_either(self):
        errors, _ = verify_claims(
            extract_claims("see `test_e2e_concurrency.py.bak:12`", DOC),
            set(),
            self.KNOWN,
        )
        self.assertEqual([c.kind for c in errors], ["path_line"])
        self.assertIn("no such file: test_e2e_concurrency.py.bak", errors[0].detail)

    def test_a_sentence_closing_period_is_not_absorbed_into_the_path(self):
        # The tightening cuts the other way too: the tail must end in a word
        # character, or `see views.py.` would cite a file called `views.py.`
        # and fail on a correct sentence. Unbackticked, so the period really
        # does abut the token -- inside backticks it cannot, which is how a
        # weaker version of this test passed a mutant.
        for prose in ("see orders/views.py. Done.", "see `orders/views.py`."):
            claims = kinds(extract_claims(prose, DOC), "module_path")
            self.assertEqual([c.value for c in claims], ["orders/views.py"], prose)

    def test_every_path_line_citation_is_also_a_module_citation(self):
        # The invariant BUG-1's fix leans on, pinned so a future edit to either
        # pattern cannot quietly break it: the span of a path token is recorded
        # in the module pass alone, and that is only sound while the two
        # patterns agree on where the token ends.
        for cited in (
            "orders/views.py:12",
            "tests/test_e2e_concurrency.py:114",
            "test_e2e_concurrency.py.bak:12",
            "orders/views.py:L12",
        ):
            path_group = RE_PATH_LINE.search(cited).group(1)
            self.assertEqual(RE_MODULE_PATH.search(cited).group(1), path_group, cited)
            self.assertEqual(
                RE_MODULE_PATH.search(cited).span(1),
                RE_PATH_LINE.search(cited).span(1),
                cited,
            )

    def test_a_bak_path_for_a_non_test_stem_is_still_an_error(self):
        # The same tightening on the module half, on its own: `views.py.bak`
        # resolves to nothing, exactly as `views.py.nope` did.
        errors, _ = verify_claims(
            extract_claims("see `orders/views.py.bak`", DOC), set(), self.KNOWN
        )
        self.assertEqual([c.kind for c in errors], ["module_path"])

    def test_a_real_path_line_still_resolves_with_the_tightened_pattern(self):
        errors, warnings = verify_claims(
            extract_claims("see `tests/test_e2e_concurrency.py:114`", DOC),
            set(),
            self.KNOWN,
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_a_slash_separated_path_list_is_unchanged(self):
        # Prose enumerates `models.py/admin.py/views.py` in one breath. The
        # tightened pattern must keep reading that the way it always did --
        # one token, which does not resolve -- and not start splitting it.
        claims = extract_claims("from `models.py/admin.py/views.py` today", DOC)
        self.assertEqual([c.kind for c in claims], ["module_path"])
        self.assertEqual(claims[0].value, "models.py/admin.py/views.py")


class PathSpanContainmentTests(unittest.TestCase):
    """The decision BUG-1 rests on, pinned on its own: exact containment.

    Pinned here rather than only through ``extract_claims`` because the
    difference between containment and overlap is not reachable from prose --
    the boundary patterns make a partially-overlapping token impossible -- and
    a rule that is only correct by accident of two other regexes is a rule that
    one edit away from being a loophole.
    """

    def test_a_span_inside_a_path_span_is_consumed(self):
        self.assertTrue(_inside_any_span((5, 10), [(0, 20)]))

    def test_a_span_touching_a_path_span_but_outside_it_is_not(self):
        # Reaches across the path without sitting inside it. Not reachable from
        # prose -- the boundary patterns make it impossible -- which is exactly
        # why it is pinned here.
        self.assertFalse(_inside_any_span((0, 10), [(0, 9)]))
        self.assertFalse(_inside_any_span((0, 3), [(5, 9)]))

    def test_a_span_containing_a_path_span_is_not_consumed(self):
        self.assertFalse(_inside_any_span((0, 30), [(5, 10)]))

    def test_no_spans_consumes_nothing(self):
        self.assertFalse(_inside_any_span((0, 3), []))


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
        self.assertIn("counter-example", warnings[0].note)

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
        errors, _ = self.verify(f"`test_retired_name` {retired_directive()}")
        self.assertEqual(errors, [])

    def test_a_reason_under_the_floor_is_not_a_directive(self):
        terse = (
            "<!-- doc-claims:absent test_retired_name (renamed-to test_real_one)"
            " ; reason: SPEC-1-B07d -->"
        )
        self.assertEqual(parse(terse), {})
        errors, _ = self.verify(f"`test_retired_name` {terse}")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_a_renamed_to_without_a_witness_is_not_a_directive(self):
        malformed = (
            "<!-- doc-claims:absent test_retired_name (renamed-to)"
            " ; reason: renamed in SPEC-1-B07d and cited here -->"
        )
        self.assertEqual(parse(malformed), {})
        errors, _ = self.verify(f"`test_retired_name` {malformed}")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_an_unknown_kind_is_not_a_directive(self):
        malformed = DIRECTIVE.replace("(renamed-to", "(was-renamed-to")
        self.assertEqual(parse(malformed), {})
        errors, _ = self.verify(f"`test_retired_name` {malformed}")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_a_plain_mention_of_the_keyword_is_not_a_directive(self):
        self.assertEqual(parse("doc-claims:absent test_x"), {})
        self.assertEqual(parse("<!-- doc-claims:absent -->"), {})

    def test_a_directive_is_bound_to_its_own_line(self):
        text = (
            f"quoted `test_retired_name` {DIRECTIVE}\n"
            "and `test_retired_name` again, uncited-as-absent"
        )
        errors, warnings = verify_claims(
            extract_claims(text, DOC), self.KNOWN, self.PATHS
        )
        self.assertEqual([c.line for c in errors], [2])

    def test_two_directives_for_one_name_honour_nothing(self):
        # Refused by the per-line cap, not by comparing the two: the script
        # does not pick one of two statements the author put on one line.
        other = OTHER_DIRECTIVE.replace("test_second_absent", "test_retired_name")
        errors, _ = self.verify(f"`test_retired_name` {DIRECTIVE} {other}")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_the_claim_carries_the_reason_so_the_log_can_show_it(self):
        claims = kinds(
            extract_claims(f"`test_retired_name` {DIRECTIVE}", DOC), "test_name"
        )
        self.assertIn("renamed-to", claims[0].note)
        self.assertIn("B07d", claims[0].note)

    def test_an_honoured_claim_is_not_an_error(self):
        _, warnings = self.verify(f"`test_retired_name` {DIRECTIVE}")
        self.assertEqual(warnings[0].severity, "warn")

    def test_parse_returns_the_witness(self):
        parsed = parse(DIRECTIVE)["test_retired_name"]
        self.assertEqual(parsed.witness, "test_real_one")
        self.assertEqual(parsed.kind, "renamed-to")
        self.assertEqual(parsed.target_kind, "test")
        self.assertGreaterEqual(len(parsed.reason), MIN_REASON_CHARS)


class DirectiveQuotaTests(unittest.TestCase):
    """BUG-A [P2]: the documented invariant was false and is now enforced.

    Cycle 1 claimed a directive "names ONE test, and only that test, and only
    on its own line, so a sibling claim on the same sentence is still checked".
    Two well-formed directives on one line silenced both. The cap is one
    ACCEPTED directive per line: over quota, the line is read as carrying none.
    """

    KNOWN = {"test_real_one"}
    PATHS = {"backend/orders/tests_returns.py": 2600}

    def verify(self, line):
        return verify_claims(extract_claims(line, DOC), self.KNOWN, self.PATHS)

    def test_two_directives_on_one_line_silence_nothing(self):
        errors, _ = self.verify(
            f"`test_retired_name` {DIRECTIVE} and `test_second_absent`"
            f" {OTHER_DIRECTIVE}"
        )
        self.assertEqual(
            sorted(c.value for c in errors),
            ["test_retired_name", "test_second_absent"],
        )

    def test_neither_of_two_is_arbitrarily_chosen(self):
        # The first one is not privileged, because "first match wins" is a
        # guess, and a guess is what this script refuses everywhere else.
        errors, _ = self.verify(
            f"`test_retired_name` {DIRECTIVE} `test_second_absent`"
            f" {OTHER_DIRECTIVE}"
        )
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(c.kind == "test_name" for c in errors))

    def test_the_refusal_is_visible_in_the_error(self):
        # A gate that goes red without saying why is a gate the author cannot
        # act on, and the quota is the least guessable of the refusals.
        errors, _ = self.verify(
            f"`test_retired_name` {DIRECTIVE} `test_second_absent`"
            f" {OTHER_DIRECTIVE}"
        )
        self.assertTrue(all("refused" in c.detail for c in errors))
        self.assertTrue(all("more than one directive" in c.detail for c in errors))

    def test_one_directive_still_works(self):
        # The cap must not cost the mechanism its one legitimate use.
        errors, warnings = self.verify(f"`test_retired_name` {DIRECTIVE}")
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_one_directive_and_one_uncited_absent_name_still_separates(self):
        # The cycle-1 narrowness case, re-pinned under the cap: the single
        # directive is honoured and its neighbour is still an error.
        errors, _ = self.verify(
            f"`test_retired_name` {DIRECTIVE} and `test_also_missing`"
        )
        self.assertEqual([c.value for c in errors], ["test_also_missing"])


class DirectiveReasonTests(unittest.TestCase):
    """BUG-B [P3]: a length floor is a length floor, and now says so.

    `reason: the the the ...` cleared MIN_REASON_CHARS and silenced an absent
    name with zero machine verification. The floor stays and is restated for
    what it buys; the reason must ALSO name a commit sha or a task id, which is
    what makes it traceable to a ledger row.
    """

    KNOWN = {"test_real_one"}
    PATHS = {}

    def verify(self, line):
        return verify_claims(extract_claims(line, DOC), self.KNOWN, self.PATHS)

    def filler(self, reason):
        return (
            "<!-- doc-claims:absent test_absent_x (renamed-to test_real_one)"
            f" ; reason: {reason} -->"
        )

    def test_long_filler_with_no_reference_is_refused(self):
        # 39 characters, comfortably over the floor, and says nothing.
        reason = "the the the the the the the the the the"
        self.assertGreater(len(reason), MIN_REASON_CHARS)
        self.assertEqual(parse(self.filler(reason)), {})
        errors, _ = self.verify(f"`test_absent_x` {self.filler(reason)}")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_a_task_id_is_enough(self):
        errors, _ = self.verify(
            f"`test_absent_x` {self.filler('renamed under SPEC-1-B07d')}"
        )
        self.assertEqual(errors, [])

    def test_every_id_form_this_repository_uses_is_enough(self):
        # The fix must not cost a real reference its force, so the accepted set
        # is pinned explicitly rather than by one lucky example.
        for reference in (
            "renamed under SPEC-1-B07d and reported as absent",
            "reported as absent, see BUG-3 for the shape",
            "reported as absent, see TOOL-01 for the shape",
            "reported absent, see R-9.2.14 and S22-04 both",
            "the leading byte was eaten at c873b27 in that write",
            "eaten at 46a5eee1cc in the byte-eaten write",
        ):
            with self.subTest(reference=reference):
                errors, _ = self.verify(f"`test_absent_x` {self.filler(reference)}")
                self.assertEqual(errors, [])

    def test_hyphenated_english_filler_is_refused(self):
        # BUG-B: the old second branch was any hyphenated word, and this prose
        # is full of them. Each of these was ACCEPTED before the digit rule.
        for filler in (
            "the the the the the the the the the the well-known",
            "the the the the the the the the the the counter-example",
            "the the the the the the the the the the read-only",
            "the the the the the the the the the the up-to-date",
            "the the the the the the the the the the e-mail",
            "the the the the the the the the the the so-so",
        ):
            with self.subTest(filler=filler):
                self.assertEqual(parse(self.filler(filler)), {})
                errors, _ = self.verify(f"`test_absent_x` {self.filler(filler)}")
                self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_an_a_f_word_is_not_a_sha(self):
        # The old sha branch was [0-9a-f]{7,40}, so these all matched.
        for word in ("defaced", "effaced", "aaaaaaaaaaaaaaaaaaaa"):
            with self.subTest(word=word):
                reason = "the the the the the the the the the the " + word
                self.assertEqual(parse(self.filler(reason)), {})
                errors, _ = self.verify(f"`test_absent_x` {self.filler(reason)}")
                self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_a_reason_with_no_token_at_all_is_refused(self):
        # The purest filler there is: 36 a's matched the old sha branch whole.
        reason = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        self.assertGreaterEqual(len(reason), MIN_REASON_CHARS)
        self.assertEqual(parse(self.filler(reason)), {})
        errors, _ = self.verify(f"`test_absent_x` {self.filler(reason)}")
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_the_shipped_rule_is_a_shape_test_not_proof(self):
        # Recorded deliberately, so the residual cannot be forgotten: a
        # hyphenated token containing a digit still satisfies the rule. It is
        # pinned as the KNOWN boundary of what ships, not as an endorsement --
        # an uppercase-led token would close it, and that is a decision for a
        # later cycle rather than a quiet change here.
        for filler in (
            "aaaaaaaaaaaaaaaaaaaa a-1",
            "aaaaaaaaaaaaaaaaaaaa utf-8",
            "aaaaaaaaaaaaaaaaaaaa sha-1",
        ):
            with self.subTest(filler=filler):
                self.assertTrue(RE_REASON_REFERENCE.search(filler))

    def test_a_commit_sha_is_enough(self):
        errors, _ = self.verify(
            f"`test_absent_x` {self.filler('the leading byte was eaten at c873b27')}"
        )
        self.assertEqual(errors, [])

    def test_the_floor_still_applies_to_a_reason_that_has_a_reference(self):
        # Both rules, not either: a bare id is not a statement either.
        self.assertEqual(parse(self.filler("SPEC-1-B07d")), {})

    def test_a_refused_reason_is_reported(self):
        errors, _ = self.verify(
            f"`test_absent_x` {self.filler('the the the the the the the the')}"
        )
        self.assertIn("names no commit sha or task id", errors[0].detail)

    def test_retired_cannot_be_fillered_past_the_reference_rule(self):
        # The auditor's exact attack: retired + long filler, no ledger row.
        directive = (
            "<!-- doc-claims:absent test_absent_x (retired) ; reason: the the"
            " the the the the the the the the the -->"
        )
        self.assertEqual(parse(directive), {})
        errors, _ = self.verify(f"`test_absent_x` {directive}")
        self.assertEqual([c.kind for c in errors], ["test_name"])


class PathCounterExampleTests(unittest.TestCase):
    """BUG-C [P3]: the corruption evidence in the ledger is a COUNTER-EXAMPLE.

    backend/docs/spec-run-state.md quotes what a byte-eaten write left behind
    -- "the file now reads `ests_returns.py`, `iews.py`" -- in order to REPORT
    that those paths do not exist. Cycle 1 called them prose typos and covered
    only test_name, so the motive for the directive was half-closed.
    """

    KNOWN = {"test_real_one"}
    PATHS = {
        "backend/orders/tests_returns.py": 2600,
        "backend/orders/views.py": 900,
        "backend/ops/views.py": 400,
    }

    def verify(self, line):
        return verify_claims(extract_claims(line, DOC), self.KNOWN, self.PATHS)

    def test_a_corrupted_path_fragment_stops_failing(self):
        errors, warnings = self.verify(
            f"the file now reads `ests_returns.py` {PATH_DIRECTIVE}"
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("corrupted-fragment-of", warnings[0].note)

    def test_the_intact_file_witness_must_resolve(self):
        # `views.py` is ambiguous across the apps, so it is NOT a witness. The
        # author has to name the file they mean, which is the point.
        directive = (
            "<!-- doc-claims:absent iews.py (corrupted-fragment-of views.py)"
            " ; reason: c873b27 ate the leading byte, reported as corruption -->"
        )
        errors, _ = self.verify(f"the file now reads `iews.py` {directive}")
        self.assertEqual([c.kind for c in errors], ["module_path"])
        self.assertIn("not in the tree", errors[0].detail)

    def test_an_app_relative_witness_resolves(self):
        directive = (
            "<!-- doc-claims:absent iews.py (corrupted-fragment-of"
            " orders/views.py) ; reason: c873b27 ate the leading byte, reported"
            " as the corruption it is -->"
        )
        errors, _ = self.verify(f"the file now reads `iews.py` {directive}")
        self.assertEqual(errors, [])

    def test_a_witness_that_is_merely_a_real_file_is_refused(self):
        # The narrowing. `orders/views.py` resolves, so before this rule it
        # silenced ANY absent path -- including a fragment of a different file
        # entirely. The witness has to be this fragment's intact form.
        directive = (
            "<!-- doc-claims:absent accounts/whatever.py"
            " (corrupted-fragment-of orders/views.py) ; reason: c873b27 ate the"
            " leading byte, reported here as the corruption -->"
        )
        errors, _ = self.verify(
            f"the file now reads `accounts/whatever.py` {directive}"
        )
        self.assertEqual([c.kind for c in errors], ["module_path"])
        # The witness DID resolve, so the error must not claim it is missing.
        self.assertIn("does not end with", errors[0].detail)
        self.assertNotIn("not in the tree", errors[0].detail)

    def test_a_witness_whose_path_does_not_end_with_the_fragment_is_refused(self):
        # Same resolved file, a fragment it has nothing to do with. A suffix
        # rule still refuses it: views.py does not end with whatever.py.
        directive = (
            "<!-- doc-claims:absent whatever.py (corrupted-fragment-of"
            " orders/views.py) ; reason: c873b27 ate the leading byte, reported"
            " here as the corruption -->"
        )
        errors, _ = self.verify(f"the file now reads `whatever.py` {directive}")
        self.assertEqual([c.kind for c in errors], ["module_path"])

    def test_the_witness_still_works_when_it_really_is_the_intact_form(self):
        # The rule must not cost the mechanism its one real use.
        for target, witness in (
            ("ests_returns.py", "tests_returns.py"),
            ("iews.py", "orders/views.py"),
        ):
            with self.subTest(target=target):
                directive = (
                    f"<!-- doc-claims:absent {target} (corrupted-fragment-of"
                    f" {witness}) ; reason: c873b27 ate the leading byte,"
                    f" reported here as the corruption -->"
                )
                errors, _ = self.verify(f"the file now reads `{target}` {directive}")
                self.assertEqual(errors, [])

    def test_a_path_directive_cannot_excuse_a_line_past_the_end_of_a_file(self):
        # The narrowing that keeps this from becoming a stale-reference pass:
        # the file RESOLVED, so nothing about the citation is a counter-example
        # and a wrong line number stays a wrong line number.
        directive = (
            "<!-- doc-claims:absent orders/views.py (corrupted-fragment-of"
            " orders/views.py) ; reason: SPEC-1-B07d reported the fragment -->"
        )
        errors, _ = self.verify(f"see `orders/views.py:4000` {directive}")
        self.assertEqual([c.kind for c in errors], ["path_line"])
        self.assertIn("past the end", errors[0].detail)

    def test_a_path_directive_honours_a_missing_path_line(self):
        directive = (
            "<!-- doc-claims:absent ests_returns.py (corrupted-fragment-of"
            " tests_returns.py) ; reason: c873b27 ate the leading byte, quoted"
            " here to report the corruption -->"
        )
        errors, warnings = self.verify(f"was `ests_returns.py:2551` {directive}")
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_retired_is_not_accepted_on_a_path(self):
        # The one unwitnessed kind must not be spendable on a path, or
        # extending to paths would have added a fresh escape hatch.
        directive = (
            "<!-- doc-claims:absent iews.py (retired) ; reason: the module was"
            " deleted in SPEC-1-B07d and is reported as absent -->"
        )
        self.assertEqual(parse(directive), {})
        errors, _ = self.verify(f"the file now reads `iews.py` {directive}")
        self.assertEqual([c.kind for c in errors], ["module_path"])

    def test_a_test_kind_on_a_path_target_is_refused(self):
        directive = (
            "<!-- doc-claims:absent iews.py (renamed-to test_real_one)"
            " ; reason: renamed under SPEC-1-B07d and reported as absent -->"
        )
        self.assertEqual(parse(directive), {})
        errors, _ = self.verify(f"the file now reads `iews.py` {directive}")
        self.assertEqual([c.kind for c in errors], ["module_path"])

    def test_a_path_directive_does_not_excuse_a_test_name(self):
        errors, _ = self.verify(
            f"the file now reads `ests_returns.py` {PATH_DIRECTIVE} and"
            f" `test_absent_x`"
        )
        self.assertEqual([c.kind for c in errors], ["test_name"])

    def test_two_path_directives_on_one_line_are_over_quota(self):
        # The consequence to hand to the ledger owner, pinned as a test: the
        # corruption sentence names two fragments on ONE source line, so the
        # cap means it cannot be cleared by a single edit there.
        second = (
            "<!-- doc-claims:absent iews.py (corrupted-fragment-of"
            " orders/views.py) ; reason: c873b27 ate the leading byte, reported"
            " as the corruption it is -->"
        )
        errors, _ = self.verify(
            f"the file now reads `ests_returns.py` {PATH_DIRECTIVE} and"
            f" `iews.py` {second}"
        )
        self.assertEqual([c.kind for c in errors], ["module_path", "module_path"])
        self.assertTrue(all("more than one directive" in c.detail for c in errors))


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

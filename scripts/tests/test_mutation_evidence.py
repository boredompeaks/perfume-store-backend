"""Unit tests for scripts/mutation_evidence.py.

The safety-critical behaviour -- that a mutated file is always restored, even
when the test run explodes -- is tested here against a temporary tree with
the runner stubbed. That is the one property that must never be taken on
trust: a replay that leaves the worktree mutated is worse than no replay.

Run from the repo root::

    python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mutation_evidence as me  # noqa: E402

SAMPLE_OUTPUT = """Found 12 test(s).
..........
======================================================================
FAIL: test_the_window_is_refused (orders.tests_returns.ReturnEligibilityWindowTests.test_the_window_is_refused)
----------------------------------------------------------------------
Ran 12 tests in 1.2s

FAILED (failures=1, expected failures=4)
"""

GREEN_OUTPUT = """Found 12 test(s).
............
----------------------------------------------------------------------
Ran 12 tests in 1.1s

OK (expected failures=4)
"""


ERRORS_ONLY_OUTPUT = """Found 12 test(s).
======================================================================
ERROR: test_the_window_is_refused (orders.tests_returns.ReturnTests.test_a)
----------------------------------------------------------------------
Ran 12 tests in 1.2s

FAILED (errors=1)
"""


class OccurrenceTests(unittest.TestCase):
    def test_counts_occurrences(self):
        self.assertEqual(me.count_occurrences(b"aXaXa", b"X"), 2)

    def test_zero_occurrences(self):
        self.assertEqual(me.count_occurrences(b"abc", b"z"), 0)

    def test_empty_find_text_is_refused(self):
        with self.assertRaises(me.EvidenceError):
            me.count_occurrences(b"abc", b"")


class ApplyMutationTests(unittest.TestCase):
    def test_replaces_exactly_one_occurrence(self):
        self.assertEqual(me.apply_mutation(b"aXa", b"X", b"Y"), b"aYa")

    def test_refuses_when_find_occurs_twice(self):
        # This is the SPEC-1-B07d M3/M4 trap: two parties measured different
        # edits and each believed the other was wrong.
        with self.assertRaises(me.EvidenceError) as caught:
            me.apply_mutation(b"aXaXa", b"X", b"Y")
        self.assertIn("occurs 2 times", str(caught.exception))

    def test_refuses_when_find_occurs_nowhere(self):
        with self.assertRaises(me.EvidenceError):
            me.apply_mutation(b"abc", b"z", b"Y")

    def test_deletion_mutation_is_supported(self):
        self.assertEqual(me.apply_mutation(b"abc", b"b", b""), b"ac")

    def test_lf_needle_matches_a_crlf_worktree_file(self):
        # core.autocrlf is on with no .gitattributes, so the worktree copy is
        # CRLF while a manifest is authored from the LF committed blob. Without
        # this the needle occurs zero times and every real replay refuses.
        crlf = b"    return now <= anchor\r\n    return None\r\n"
        mutated = me.apply_mutation(crlf, b"return now <= anchor", b"return True")
        self.assertEqual(mutated, b"    return True\r\n    return None\r\n")

    def test_replacement_newlines_take_the_files_ending(self):
        crlf = b"    x = 1\r\n    y = 2\r\n"
        mutated = me.apply_mutation(crlf, b"    y = 2\n", b"    y = 3\n    z = 4\n")
        self.assertEqual(mutated, b"    x = 1\r\n    y = 3\r\n    z = 4\r\n")

    def test_an_lf_file_is_never_rewritten_to_crlf(self):
        lf = b"    x = 1\n    y = 2\n"
        mutated = me.apply_mutation(lf, b"    y = 2\n", b"    y = 3\n")
        self.assertEqual(mutated, b"    x = 1\n    y = 3\n")

    def test_the_exactly_once_guard_survives_the_translation(self):
        # Translating the needle must not weaken the guard: two CRLF matches
        # are still two.
        crlf = b"x = 1\r\nx = 1\r\n"
        with self.assertRaises(me.EvidenceError) as caught:
            me.apply_mutation(crlf, b"x = 1\n", b"x = 2\n")
        self.assertIn("occurs 2 times", str(caught.exception))


class ParseFailureTests(unittest.TestCase):
    def test_parses_failing_test_identifiers(self):
        found = me.parse_failure_lines(SAMPLE_OUTPUT)
        self.assertIn("test_the_window_is_refused", found)
        self.assertIn(
            "orders.tests_returns.ReturnEligibilityWindowTests"
            ".test_the_window_is_refused",
            found,
        )

    def test_error_lines_parse_too(self):
        found = me.parse_failure_lines(
            "ERROR: test_boom (orders.tests_returns.ReturnTests.test_boom)\n"
        )
        self.assertIn("test_boom", found)

    def test_green_output_yields_no_failures(self):
        self.assertEqual(me.parse_failure_lines(GREEN_OUTPUT), [])

    def test_failure_count_comes_from_the_failed_line(self):
        self.assertEqual(me.parse_failure_count(SAMPLE_OUTPUT), 1)

    def test_green_output_has_no_failure_count(self):
        # Reading the run summary instead is how "0 dirty everywhere" and its
        # relatives get believed: the summary says nothing when there is no
        # failure, and nothing is not zero.
        self.assertIsNone(me.parse_failure_count(GREEN_OUTPUT))

    def test_an_errors_only_line_is_not_a_failure_count(self):
        # unittest prints "FAILED (errors=1)" with no `failures=` component
        # when nothing failed. Reading 1 off it would let a mutant that merely
        # breaks an import satisfy a claimed figure.
        self.assertIsNone(me.parse_failure_count(ERRORS_ONLY_OUTPUT))
        self.assertTrue(me.ran_red(ERRORS_ONLY_OUTPUT))

    def test_a_green_run_is_not_red(self):
        # "no count is readable" and "the run was green" are different facts.
        self.assertFalse(me.ran_red(GREEN_OUTPUT))
        self.assertTrue(me.ran_red(SAMPLE_OUTPUT))


class ConfirmTests(unittest.TestCase):
    def test_all_expected_present_confirms(self):
        ok, missing = me.confirm_expected_failures(["test_a"], ["test_a", "test_b"])
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_missing_expected_fails(self):
        ok, missing = me.confirm_expected_failures(["test_a", "test_z"], ["test_a"])
        self.assertFalse(ok)
        self.assertEqual(missing, ["test_z"])

    def test_manifest_naming_nothing_is_refused(self):
        # An expectation set that cannot be satisfied would certify anything.
        ok, missing = me.confirm_expected_failures([], ["test_a"])
        self.assertFalse(ok)
        self.assertIn("no expected_failing_tests", missing[0])


class ReplayRestoreTests(unittest.TestCase):
    """The file must come back byte-identical whatever the run does."""

    def replay(self, output, claimed_count=None, expected=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "backend" / "orders" / "views.py"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"def gate():\n    return now <= anchor\n")

            mutation = {
                "id": "M1",
                "description": "drop the window clause",
                "path": "backend/orders/views.py",
                "find": "return now <= anchor",
                "replace": "return True",
                "expected_failing_tests": expected or ["test_the_window_is_refused"],
                "claimed_count": claimed_count,
                "test_scope": ["orders.tests_returns"],
            }

            with mock.patch.object(me, "REPO_ROOT", root), mock.patch.object(
                me, "BACKEND_DIR", root
            ), mock.patch.object(me, "run_tests", return_value=output) as runner:
                outcome = me.replay_mutation(mutation)
            return outcome, target.read_bytes(), runner

    def test_held_when_every_expected_test_fails(self):
        outcome, content, _ = self.replay(SAMPLE_OUTPUT)
        self.assertEqual(outcome.status, "held", outcome.detail)
        self.assertTrue(outcome.restored)

    def test_original_bytes_are_restored_on_success(self):
        _, content, _ = self.replay(SAMPLE_OUTPUT)
        self.assertEqual(content, b"def gate():\n    return now <= anchor\n")

    def test_file_is_restored_even_when_run_tests_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "backend" / "orders" / "views.py"
            target.parent.mkdir(parents=True)
            original = b"def gate():\n    return now <= anchor\n"
            target.write_bytes(original)
            mutation = {
                "id": "M1",
                "description": "",
                "path": "backend/orders/views.py",
                "find": "return now <= anchor",
                "replace": "return True",
                "expected_failing_tests": ["test_the_window"],
            }

            def explode(*_args, **_kwargs):
                raise RuntimeError("the runner fell over")

            with mock.patch.object(me, "REPO_ROOT", root), mock.patch.object(
                me, "run_tests", side_effect=explode
            ):
                with self.assertRaises(RuntimeError):
                    me.replay_mutation(mutation)
            # The `finally` must have restored the file despite the raise.
            self.assertEqual(target.read_bytes(), original)

    def test_unexpected_edit_is_refused_before_anything_is_written(self):
        outcome, content, _ = self.replay(GREEN_OUTPUT)
        self.assertEqual(outcome.status, "failed")
        self.assertTrue(outcome.restored)
        self.assertEqual(content, b"def gate():\n    return now <= anchor\n")

    def test_a_surviving_mutant_is_named_as_one(self):
        # A green suite under mutation is the finding of the whole exercise. It
        # must not read as "replay could not find the name".
        outcome, _, _ = self.replay(GREEN_OUTPUT)
        self.assertIn("SURVIVED", outcome.detail)

    def test_errors_alone_do_not_satisfy_a_claimed_count(self):
        outcome, _, _ = self.replay(ERRORS_ONLY_OUTPUT, claimed_count=1)
        self.assertEqual(outcome.status, "failed")
        self.assertIsNone(outcome.observed_count)
        self.assertIn("errors only", outcome.detail)

    def test_wrong_claimed_count_is_caught(self):
        outcome, _, _ = self.replay(SAMPLE_OUTPUT, claimed_count=7)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("claimed_count 7", outcome.detail)

    def test_correct_claimed_count_is_accepted(self):
        outcome, _, _ = self.replay(SAMPLE_OUTPUT, claimed_count=1)
        self.assertEqual(outcome.status, "held", outcome.detail)

    def test_missing_target_file_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mutation = {
                "id": "M1",
                "path": "backend/gone/views.py",
                "find": "a",
                "replace": "b",
            }
            with mock.patch.object(me, "REPO_ROOT", root):
                outcome = me.replay_mutation(mutation)
            self.assertEqual(outcome.status, "error")


class DirtyWorktreeTests(unittest.TestCase):
    def test_dirty_tree_is_refused(self):
        with mock.patch.object(me, "_dirty_paths", return_value=[" M a.py"]):
            with self.assertRaises(me.EvidenceError) as caught:
                me.run_task("SPEC-1-B07d")
        self.assertIn("dirty worktree", str(caught.exception))


class ManifestTests(unittest.TestCase):
    def test_missing_manifest_lists_what_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(me, "MANIFEST_DIR", Path(tmp)):
                with self.assertRaises(me.EvidenceError) as caught:
                    me.load_manifest("NOPE")
        self.assertIn("no manifest for", str(caught.exception))

    def test_available_manifests_is_empty_without_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(me, "MANIFEST_DIR", Path(tmp) / "gone"):
                self.assertEqual(me.available_manifests(), [])

    def write(self, tmp, body):
        path = Path(tmp) / "TASK.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        with mock.patch.object(me, "MANIFEST_DIR", Path(tmp)):
            return path

    def test_manifest_without_mutations_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, {"mutations": []})
            with mock.patch.object(me, "MANIFEST_DIR", Path(tmp)):
                with self.assertRaises(me.EvidenceError) as caught:
                    me.load_manifest("TASK")
        self.assertIn("non-empty list", str(caught.exception))

    def test_mutation_missing_a_key_is_refused_by_name(self):
        # A KeyError halfway through a replay would leave the operator reading
        # a traceback instead of a malformed manifest.
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, {"mutations": [{"id": "M1", "path": "a.py"}]})
            with mock.patch.object(me, "MANIFEST_DIR", Path(tmp)):
                with self.assertRaises(me.EvidenceError) as caught:
                    me.load_manifest("TASK")
        self.assertIn("mutation #1 is missing find", str(caught.exception))

    def test_a_later_mutation_missing_a_key_is_still_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(
                tmp,
                {
                    "mutations": [
                        {"id": "M1", "path": "a.py", "find": "x"},
                        {"path": "b.py", "find": "x"},
                    ]
                },
            )
            with mock.patch.object(me, "MANIFEST_DIR", Path(tmp)):
                with self.assertRaises(me.EvidenceError) as caught:
                    me.load_manifest("TASK")
        self.assertIn("mutation #2 is missing id", str(caught.exception))

    def test_a_complete_manifest_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(
                tmp,
                {
                    "mutations": [
                        {"id": "M1", "path": "a.py", "find": "x", "replace": "y"},
                    ]
                },
            )
            with mock.patch.object(me, "MANIFEST_DIR", Path(tmp)):
                manifest = me.load_manifest("TASK")
        self.assertEqual(manifest["mutations"][0]["id"], "M1")


if __name__ == "__main__":
    unittest.main()

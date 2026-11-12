"""Unit tests for scripts/doc_metrics.py.

Mostly pure core: no git, no real tree, no Django. The shell (git index reads
and the file list) is exercised by running the script; the integration tests
pin the index rules, because those rules are the measurement's whole
reproducibility claim.

Run from the repo root::

    python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import doc_metrics as dm  # noqa: E402


def counts(source: str) -> dm.ScopeCounts:
    return dm.classify_source(source)


def scope(
    *,
    files: int = 1,
    logic: int = 0,
    comment: int = 0,
    docstring: int = 0,
    blank: int = 0,
    data_bytes: int = 0,
) -> dm.ScopeCounts:
    """A scope whose total is consistent by construction."""
    return dm.ScopeCounts(
        files=files,
        total_lines=logic + comment + docstring + blank,
        logic_lines=logic,
        comment_lines=comment,
        docstring_lines=docstring,
        blank_lines=blank,
        bytes=data_bytes,
    )


def measurement(
    *,
    logic: int = 10,
    comment: int = 3,
    docstring: int = 2,
    blank: int = 5,
    app_bytes: int = 100,
    docs_bytes: int = 50,
    ledger_bytes: int = 20,
    test_logic: int = 4,
) -> dm.Measurement:
    return dm.Measurement(
        app_python=scope(
            logic=logic,
            comment=comment,
            docstring=docstring,
            blank=blank,
            data_bytes=app_bytes,
        ),
        test_python=scope(logic=test_logic, data_bytes=40),
        docs_markdown=scope(files=2, logic=0, data_bytes=docs_bytes),
        ledger_markdown=scope(files=1, data_bytes=ledger_bytes),
    )


class LineClassificationTests(unittest.TestCase):
    def test_empty_source_has_no_lines(self):
        result = counts("")
        self.assertEqual(result.total_lines, 0)
        self.assertEqual(result.logic_lines, 0)
        self.assertEqual(result.blank_lines, 0)

    def test_comment_blank_and_logic(self):
        result = counts("# one\n\nx = 1\n")
        self.assertEqual(result.total_lines, 3)
        self.assertEqual(result.comment_lines, 1)
        self.assertEqual(result.blank_lines, 1)
        self.assertEqual(result.logic_lines, 1)

    def test_indented_comment_is_comment(self):
        result = counts("def f():\n    # why\n    return 1\n")
        self.assertEqual(result.comment_lines, 1)
        self.assertEqual(result.logic_lines, 2)

    def test_trailing_comment_line_is_logic(self):
        # The line is code; the trailing comment does not reclassify it.
        result = counts("x = 1  # why\n")
        self.assertEqual(result.logic_lines, 1)
        self.assertEqual(result.comment_lines, 0)

    def test_whitespace_only_lines_are_blank(self):
        result = counts("\n   \n\t\n")
        self.assertEqual(result.blank_lines, 3)
        self.assertEqual(result.total_lines, 3)

    def test_module_docstring_and_blank_inside_it(self):
        # The pinned rule: a blank line inside a docstring is BLANK, not a
        # docstring line. The count then tracks prose the author wrote rather
        # than how airy the docstring happens to be.
        result = counts('"""Module doc.\n\nMore text.\n"""\n')
        self.assertEqual(result.total_lines, 4)
        self.assertEqual(result.docstring_lines, 3)
        self.assertEqual(result.blank_lines, 1)
        self.assertEqual(result.logic_lines, 0)

    def test_class_and_function_docstrings(self):
        source = 'class A:\n    """class"""\n\n    def m(self):\n        """method"""\n'
        result = counts(source)
        self.assertEqual(result.docstring_lines, 2)
        self.assertEqual(result.logic_lines, 2)
        self.assertEqual(result.blank_lines, 1)

    def test_async_function_docstring(self):
        result = counts('async def f():\n    """async"""\n    return 1\n')
        self.assertEqual(result.docstring_lines, 1)
        self.assertEqual(result.logic_lines, 2)

    def test_decorated_function_docstring(self):
        source = '@decorator\ndef f():\n    """doc"""\n    return 1\n'
        result = counts(source)
        self.assertEqual(result.docstring_lines, 1)
        self.assertEqual(result.logic_lines, 3)

    def test_assigned_triple_quoted_string_is_logic(self):
        # A multiline string that is not a first-statement docstring is code,
        # even when its text looks like a comment.
        source = 'X = """\n# not a comment\n"""\n'
        result = counts(source)
        self.assertEqual(result.logic_lines, 3)
        self.assertEqual(result.comment_lines, 0)
        self.assertEqual(result.docstring_lines, 0)

    def test_blank_inside_plain_string_is_blank(self):
        result = counts('X = """\n\n"""\n')
        self.assertEqual(result.blank_lines, 1)
        self.assertEqual(result.logic_lines, 2)

    def test_hash_inside_string_is_not_a_comment(self):
        result = counts('X = "# heading"\n')
        self.assertEqual(result.logic_lines, 1)
        self.assertEqual(result.comment_lines, 0)

    def test_hash_line_inside_docstring_is_docstring_text(self):
        result = counts('"""Doc.\n\n# heading\n"""\n')
        self.assertEqual(result.docstring_lines, 3)
        self.assertEqual(result.comment_lines, 0)
        self.assertEqual(result.blank_lines, 1)

    def test_crlf_and_cr_sources_count_physical_lines(self):
        result = counts("# one\r\n\r\nx = 1\r\n")
        self.assertEqual(result.total_lines, 3)
        self.assertEqual(result.comment_lines, 1)
        self.assertEqual(result.blank_lines, 1)
        self.assertEqual(result.logic_lines, 1)

    def test_cr_only_source_keeps_tokenizer_rows_aligned(self):
        # A CR-only source is three physical lines. The tokenizer must see the
        # same three rows as the splitter, or the second comment reads as logic.
        result = counts("# a\r# b\rx = 1\r")
        self.assertEqual(result.total_lines, 3)
        self.assertEqual(result.comment_lines, 2)
        self.assertEqual(result.logic_lines, 1)

    def test_no_trailing_newline_does_not_add_a_line(self):
        result = counts("x = 1")
        self.assertEqual(result.total_lines, 1)
        self.assertEqual(result.logic_lines, 1)

    def test_unparsable_source_raises(self):
        with self.assertRaises(SyntaxError):
            counts("def broken(:\n    pass\n")


class ScopeRuleTests(unittest.TestCase):
    def test_app_python(self):
        self.assertTrue(dm.is_app_python("backend/accounts/models.py"))
        self.assertTrue(dm.is_app_python("backend/manage.py"))
        self.assertFalse(dm.is_app_python("frontend/src/lib/api.ts"))

    def test_test_modules(self):
        for path in (
            "backend/accounts/tests.py",
            "backend/orders/tests_returns.py",
            "backend/products/test_restock.py",
            "backend/tests/test_settings_security.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(dm.is_test_module(path))
                self.assertTrue(dm.is_test_python(path))
                self.assertFalse(dm.is_app_python(path))

    def test_migrations_are_excluded_from_both_python_scopes(self):
        path = "backend/accounts/migrations/0001_initial.py"
        self.assertTrue(dm.is_migration(path))
        self.assertFalse(dm.is_app_python(path))
        self.assertFalse(dm.is_test_python(path))

    def test_docs_scope_is_backend_docs_markdown_only(self):
        self.assertTrue(dm.is_doc_markdown("backend/docs/run/section-00.md"))
        self.assertTrue(dm.is_doc_markdown("backend/docs/adr/0000-template.md"))
        self.assertFalse(dm.is_doc_markdown("backend/README.md"))
        self.assertFalse(dm.is_doc_markdown("frontend/docs/PLAN.md"))
        self.assertFalse(dm.is_doc_markdown("backend/docs/conventions.txt"))

    def test_ledger_is_reported_separately_from_cuttable_docs(self):
        self.assertTrue(dm.is_ledger_markdown("backend/docs/changes.md"))
        self.assertFalse(dm.is_doc_markdown("backend/docs/changes.md"))
        self.assertFalse(dm.is_ledger_markdown("backend/docs/run/section-00.md"))


class PayloadTests(unittest.TestCase):
    def test_metrics_are_derived_from_counts(self):
        payload = dm.measurement_payload(measurement())
        self.assertEqual(payload["schema"], dm.SCHEMA_VERSION)
        self.assertEqual(payload["definition"], dm.DEFINITION)
        self.assertAlmostEqual(
            payload["metrics"]["comment_docstring_per_logic"], 0.5
        )
        self.assertAlmostEqual(
            payload["metrics"]["comment_docstring_share"], 5 / 15, places=5
        )
        self.assertAlmostEqual(payload["metrics"]["docs_to_app_python_bytes"], 0.5)

    def test_payload_is_deterministic(self):
        first = json.dumps(dm.measurement_payload(measurement()), sort_keys=True)
        second = json.dumps(dm.measurement_payload(measurement()), sort_keys=True)
        self.assertEqual(first, second)

    def test_ledger_bytes_do_not_enter_the_docs_ratio(self):
        without = dm.measurement_payload(measurement(ledger_bytes=0))
        with_ledger = dm.measurement_payload(measurement(ledger_bytes=999))
        self.assertEqual(without["metrics"], with_ledger["metrics"])
        self.assertEqual(with_ledger["scopes"]["ledger_markdown"]["bytes"], 999)

    def test_payload_has_no_timestamp_or_commit(self):
        payload = dm.measurement_payload(measurement())
        text = json.dumps(payload, sort_keys=True)
        self.assertNotIn("timestamp", text)
        self.assertNotIn("measured_at", text)
        self.assertNotIn("commit", text)


class CompareTests(unittest.TestCase):
    def test_identical_measurement_is_not_a_regression(self):
        current = measurement()
        baseline = dm.measurement_payload(current)
        self.assertEqual(dm.compare_to_baseline(current, baseline), [])

    def test_exactly_equal_ratio_is_not_a_regression(self):
        # Doubling every count keeps every ratio exactly equal. A float
        # comparison could round this into a regression; the integers cannot.
        baseline = dm.measurement_payload(
            measurement(logic=10, comment=3, docstring=2, app_bytes=100, docs_bytes=50)
        )
        current = measurement(
            logic=20, comment=6, docstring=4, app_bytes=200, docs_bytes=100
        )
        self.assertEqual(dm.compare_to_baseline(current, baseline), [])

    def test_added_comment_is_a_regression(self):
        baseline = dm.measurement_payload(measurement())
        current = measurement(comment=4)
        names = [name for name, _, _ in dm.compare_to_baseline(current, baseline)]
        self.assertIn("comment_docstring_per_logic", names)
        self.assertIn("comment_docstring_share", names)

    def test_added_code_without_comments_is_improvement(self):
        baseline = dm.measurement_payload(measurement())
        current = measurement(logic=20, app_bytes=200)
        self.assertEqual(dm.compare_to_baseline(current, baseline), [])

    def test_added_docs_bytes_is_a_docs_regression(self):
        baseline = dm.measurement_payload(measurement())
        current = measurement(docs_bytes=60)
        names = [name for name, _, _ in dm.compare_to_baseline(current, baseline)]
        self.assertEqual(names, ["docs_to_app_python_bytes"])

    def test_growing_app_bytes_improves_the_docs_ratio(self):
        baseline = dm.measurement_payload(measurement())
        current = measurement(app_bytes=200)
        self.assertEqual(dm.compare_to_baseline(current, baseline), [])

    def test_zero_denominator_baseline_is_an_error(self):
        baseline = dm.measurement_payload(measurement(logic=1))
        baseline["scopes"]["app_python"]["logic_lines"] = 0
        with self.assertRaises(dm.MeasurementError):
            dm.compare_to_baseline(measurement(), baseline)

    def test_zero_denominator_current_measurement_is_an_error(self):
        # An empty app scope must be rejected before comparison in BOTH check
        # modes; otherwise plain check reports a pass that --json cannot build.
        baseline = dm.measurement_payload(measurement())
        with self.assertRaises(dm.MeasurementError):
            dm.compare_to_baseline(measurement(logic=0), baseline)


class BaselineValidationTests(unittest.TestCase):
    def setUp(self):
        self.good = dm.measurement_payload(measurement())

    def test_valid_payload_round_trips(self):
        self.assertIs(dm.validate_baseline(self.good), self.good)

    def test_wrong_schema_is_an_error(self):
        bad = dict(self.good, schema=999)
        with self.assertRaises(dm.MeasurementError):
            dm.validate_baseline(bad)

    def test_wrong_definition_is_an_error(self):
        bad = dict(self.good, definition="something-else")
        with self.assertRaises(dm.MeasurementError):
            dm.validate_baseline(bad)

    def test_missing_scope_is_an_error(self):
        scopes = dict(self.good["scopes"])
        del scopes["app_python"]
        bad = dict(self.good, scopes=scopes)
        with self.assertRaises(dm.MeasurementError):
            dm.validate_baseline(bad)

    def test_non_integer_count_is_an_error(self):
        scopes = json.loads(json.dumps(self.good["scopes"]))
        scopes["app_python"]["logic_lines"] = "12"
        bad = dict(self.good, scopes=scopes)
        with self.assertRaises(dm.MeasurementError):
            dm.validate_baseline(bad)

    def test_negative_count_is_an_error(self):
        # A negative denominator would make every regression comparison false,
        # so a corrupt baseline must be rejected before check can use it.
        scopes = json.loads(json.dumps(self.good["scopes"]))
        scopes["app_python"]["logic_lines"] = -1
        bad = dict(self.good, scopes=scopes)
        with self.assertRaises(dm.MeasurementError):
            dm.validate_baseline(bad)


class BaselineFileTests(unittest.TestCase):
    def test_missing_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(dm.MeasurementError):
                dm.load_baseline(Path(tmp) / "absent.json")

    def test_invalid_json_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(dm.MeasurementError):
                dm.load_baseline(path)

    def test_first_record_writes_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            written, regressions = dm.record_baseline(measurement(), path)
            self.assertTrue(written)
            self.assertEqual(regressions, [])
            loaded = dm.load_baseline(path)
            self.assertEqual(loaded["definition"], dm.DEFINITION)

    def test_refuses_to_record_a_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            dm.record_baseline(measurement(), path)
            before = path.read_bytes()
            written, regressions = dm.record_baseline(
                measurement(comment=9), path
            )
            self.assertFalse(written)
            self.assertTrue(regressions)
            self.assertEqual(path.read_bytes(), before)

    def test_accept_increase_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            dm.record_baseline(measurement(), path)
            written, regressions = dm.record_baseline(
                measurement(comment=9), path, accept_increase=True
            )
            self.assertTrue(written)
            self.assertTrue(regressions)
            loaded = dm.load_baseline(path)
            self.assertEqual(loaded["scopes"]["app_python"]["comment_lines"], 9)


class GitEnvironmentTests(unittest.TestCase):
    def test_strips_repository_location_variables_and_keeps_the_rest(self):
        with mock.patch.dict(
            os.environ,
            {
                "GIT_INDEX_FILE": "hook-index",
                "GIT_DIR": "elsewhere",
                "DOC_METRICS_KEPT": "1",
            },
        ):
            cleaned = dm.git_environment()
        for name in dm.GIT_LOCATION_ENV_VARS:
            self.assertNotIn(name, cleaned)
        self.assertEqual(cleaned["DOC_METRICS_KEPT"], "1")


class IndexMeasurementTests(unittest.TestCase):
    def _init_repo(self, root: Path) -> None:
        subprocess.run(
            ["git", "init", "-q", str(root)],
            check=True,
            env=dm.git_environment(),
        )

    def _stage(self, root: Path, relpath: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), "add", relpath],
            check=True,
            capture_output=True,
            env=dm.git_environment(),
        )

    def _stage_sample(self, root: Path) -> Path:
        self._init_repo(root)
        sample = root / "backend" / "sample.py"
        sample.parent.mkdir()
        sample.write_text("# staged\nx = 1\n", encoding="utf-8")
        self._stage(root, "backend/sample.py")
        return sample

    def test_measure_reads_the_index_not_the_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = self._stage_sample(root)
            # The worktree now disagrees with the index; the measurement is the
            # staged (what-will-be-committed) content, not the edit.
            sample.write_text("y = 2\n", encoding="utf-8")
            measured = dm.measure(root)
            self.assertEqual(measured.app_python.files, 1)
            self.assertEqual(measured.app_python.total_lines, 2)
            self.assertEqual(measured.app_python.comment_lines, 1)
            self.assertEqual(measured.app_python.logic_lines, 1)

    def test_measure_ignores_an_inherited_git_index_file(self):
        # A pre-commit hook exports GIT_INDEX_FILE pointing at its temp index;
        # git would otherwise honour it and list nothing.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._stage_sample(root)
            with mock.patch.dict(
                os.environ, {"GIT_INDEX_FILE": str(root / "hook-index")}
            ):
                measured = dm.measure(root)
            self.assertEqual(measured.app_python.files, 1)
            self.assertEqual(measured.app_python.logic_lines, 1)

    def test_index_baseline_reads_the_staged_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            baseline = root / "scripts" / "doc_metrics_baseline.json"
            baseline.parent.mkdir()
            baseline.write_text(
                json.dumps(dm.measurement_payload(measurement()), sort_keys=True),
                encoding="utf-8",
            )
            self._stage(root, "scripts/doc_metrics_baseline.json")
            # The working-tree copy disappears, but the index still carries it.
            baseline.unlink()
            loaded = dm.load_index_baseline(root, baseline)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["definition"], dm.DEFINITION)
            absent = root / "scripts" / "absent.json"
            self.assertIsNone(dm.load_index_baseline(root, absent))

    def test_record_baseline_compares_against_the_indexed_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            baseline = root / "scripts" / "doc_metrics_baseline.json"
            baseline.parent.mkdir()
            baseline.write_text(
                json.dumps(dm.measurement_payload(measurement()), sort_keys=True),
                encoding="utf-8",
            )
            self._stage(root, "scripts/doc_metrics_baseline.json")
            baseline.unlink()
            written, regressions = dm.record_baseline(
                measurement(comment=9), baseline, repo_root=root
            )
            self.assertFalse(written)
            self.assertTrue(regressions)
            self.assertFalse(baseline.exists())

    def test_record_baseline_first_record_when_nothing_is_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            baseline = root / "scripts" / "doc_metrics_baseline.json"
            baseline.parent.mkdir()
            written, regressions = dm.record_baseline(
                measurement(), baseline, repo_root=root
            )
            self.assertTrue(written)
            self.assertEqual(regressions, [])
            self.assertTrue(baseline.exists())


if __name__ == "__main__":
    unittest.main()

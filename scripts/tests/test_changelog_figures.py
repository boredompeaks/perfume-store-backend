"""Unit tests for ``scripts/changelog_figures.py``'s parsers and its `check` gate.

This script exists to kill hand-transcribed figures, so its own failure mode is
the interesting one: a parser that reads a plausible number off the wrong token
would print a wrong figure with the same authority as a right one, and the
changelog row that quotes it would carry the error forward exactly as the typed
figure did. The cases below are therefore mostly negative -- the traps this
repo has already hit in a different script.

The runner output below is COPIED from real runs on this tree (SQLite and
PostgreSQL 17.11, Python 3.14) rather than invented. Two shapes are load
bearing and could not be guessed:

  * ``FAILED (failures=6, errors=1, expected failures=4)`` -- the xfail count
    shares the substring ``failures=`` with the failure count, which is how
    ``scripts/pg_gate.py`` once printed ``problems=5`` for a 7-problem run;
  * a green run prints ``OK (expected failures=4)`` and no failure count at
    all. Nothing is not zero, so ``failures`` must be None rather than 0 --
    a fabricated 0 is a figure nobody measured.

`cmd_check` is exercised against a real git repository built in a temp
directory rather than against this one, so the assertions do not move every
time a row is appended to ``docs/changes.md``.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import changelog_figures as cf  # noqa: E402

# Real `manage.py test --verbosity 0` tail, SQLite, full suite.
GREEN_RUN = (
    "INFO 2026-10-04 21:26:24,658 common.audit request_id=0ce4d64a106e4bcc8f541f8b84c3fa84d "
    "audit staff.roles_updated id=1 actor=tier-root order=None\n"
    "backend/tests/test_restore_drill.py:105: UserWarning: Overriding setting DATABASES\n"
    "----------------------------------------------------------------------\n"
    "Ran 1836 tests in 266.410s\n"
    "\n"
    "System check identified no issues (0 silenced).\n"
    "OK (expected failures=4)\n"
)

# Real tail on PostgreSQL 17.11, same commit.
RED_RUN = (
    "----------------------------------------------------------------------\n"
    "Ran 1836 tests in 291.884s\n"
    "\n"
    "System check identified no issues (0 silenced).\n"
    "FAILED (failures=6, errors=1, expected failures=4)\n"
)

# Real `coverage report` TOTAL rows: branch-inclusive (backend/.coveragerc sets
# branch = True) and the line-only shape it would be without that setting.
BRANCH_TOTAL = (
    "shipping\\views.py                     40      0       8      0 100.00%\n"
    "----------------------------------------------------------------------\n"
    "TOTAL                               9030      0    1310     21   99.80%\n"
)
LINE_TOTAL = (
    "shipping\\views.py                     40      0 100.00%\n"
    "----------------------------------------------------------------------\n"
    "TOTAL                               9030      0   100.00%\n"
)


class ParseRunTests(unittest.TestCase):
    """The runner's own summary line, read the way conventions.md requires."""

    def test_green_run_reports_no_failure_count_at_all(self):
        run = cf.parse_run(GREEN_RUN)
        self.assertEqual(run["verdict"], "OK")
        self.assertEqual(run["tests"], 1836)
        self.assertEqual(run["expected_failures"], 4)
        # Nothing is not zero: a green run reports no failures, and printing
        # failures=0 would be a count nobody measured.
        self.assertIsNone(run["failures"])
        self.assertIsNone(run["errors"])

    def test_red_run_reads_failures_and_errors_separately(self):
        run = cf.parse_run(RED_RUN)
        self.assertEqual(run["verdict"], "FAILED")
        self.assertEqual(run["failures"], 6)
        self.assertEqual(run["errors"], 1)
        self.assertEqual(run["expected_failures"], 4)
        self.assertEqual(run["tests"], 1836)

    def test_expected_failures_is_never_read_as_the_failure_count(self):
        """`expected failures=4` inside a FAILED body must not become 4."""
        output = (
            "Ran 1836 tests in 291.884s\n"
            "FAILED (failures=6, errors=1, expected failures=4)\n"
        )
        run = cf.parse_run(output)
        self.assertEqual(run["failures"], 6)
        self.assertEqual(run["expected_failures"], 4)

    def test_a_stray_ok_before_the_summary_does_not_win_the_verdict(self):
        """The runner prints its summary LAST, so the scan runs backwards.

        A test that echoes its own bare `OK` -- an assertion helper is enough --
        appears earlier in the stream than the runner's summary line. A forward
        scan would take that stray line and turn a red run green.
        """
        noisy = "OK\n" + RED_RUN
        self.assertEqual(cf.parse_run(noisy)["verdict"], "FAILED")
        self.assertEqual(cf.parse_run(noisy)["failures"], 6)

    def test_missing_summary_line_is_refused_rather_than_defaulted(self):
        with self.assertRaises(cf.FigureError) as caught:
            cf.parse_run("Ran 1836 tests in 266.410s\n")
        self.assertIn("summary line", str(caught.exception))

    def test_missing_ran_line_leaves_the_test_count_unknown(self):
        with self.assertRaises(cf.FigureError) as caught:
            cf.parse_run("OK (expected failures=4)\n")
        self.assertIn("Ran N tests", str(caught.exception))


class ParseCoverageTests(unittest.TestCase):
    """`coverage report`'s positional TOTAL row, in both of its shapes."""

    def test_branch_inclusive_row_carries_branches_and_partials(self):
        parsed = cf.parse_coverage(BRANCH_TOTAL)
        self.assertEqual(parsed["statements"], 9030)
        self.assertEqual(parsed["missed"], 0)
        self.assertEqual(parsed["branches"], 1310)
        self.assertEqual(parsed["partial_branches"], 21)
        self.assertEqual(parsed["coverage_percent"], "99.80")

    def test_line_only_row_leaves_the_branch_columns_none(self):
        parsed = cf.parse_coverage(LINE_TOTAL)
        self.assertEqual(parsed["statements"], 9030)
        self.assertIsNone(parsed["branches"])
        self.assertIsNone(parsed["partial_branches"])
        self.assertEqual(parsed["coverage_percent"], "100.00")

    def test_an_unrecognised_total_row_is_refused(self):
        with self.assertRaises(cf.FigureError):
            cf.parse_coverage("TOTAL   ?\n")

    def test_a_report_with_no_total_row_is_refused(self):
        with self.assertRaises(cf.FigureError):
            cf.parse_coverage("Name    Stmts   Miss  Cover\n")


class FormattingTests(unittest.TestCase):
    """The rendered bullet: a green run must not acquire a failure count."""

    def test_green_verdict_keeps_its_xfail_count_and_no_failures(self):
        """`xf` is a floor invariant, so the bullet has to carry it -- while the
        failure count the runner did not report stays out."""
        self.assertEqual(
            cf.format_verdict(cf.parse_run(GREEN_RUN)),
            "OK (expected failures=4)",
        )

    def test_red_verdict_renders_in_the_runner_own_order(self):
        self.assertEqual(
            cf.format_verdict(cf.parse_run(RED_RUN)),
            "FAILED (failures=6, errors=1, expected failures=4)",
        )

    def test_a_verdict_with_no_counts_at_all_stays_bare(self):
        """A bare `FAILED` line is still red; nothing is invented for it."""
        run = cf.parse_run("Ran 3 tests in 0.001s\nFAILED\n")
        self.assertEqual(cf.format_verdict(run), "FAILED")

    def test_a_green_run_never_prints_failures_zero(self):
        rendered = cf.format_verdict(cf.parse_run(GREEN_RUN))
        self.assertNotIn("failures=0", rendered)
        self.assertNotIn("errors=", rendered)

    def test_coverage_renders_the_measured_columns_only(self):
        self.assertEqual(
            cf.format_coverage(cf.parse_coverage(BRANCH_TOTAL)),
            "cov 99.80% (9030 stmts, 0 missed, 1310 branches, 21 partial)",
        )
        self.assertEqual(
            cf.format_coverage(cf.parse_coverage(LINE_TOTAL)),
            "cov 100.00% (9030 stmts, 0 missed)",
        )


class PostgresUrlTests(unittest.TestCase):
    """Only a URL with a postgres scheme may stand in for the second leg."""

    def setUp(self):
        self._saved = os.environ.pop("DATABASE_URL", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._saved

    def test_explicit_pg_url_is_accepted(self):
        self.assertEqual(
            cf.postgres_url("postgres://u:p@localhost:5432/db"),
            "postgres://u:p@localhost:5432/db",
        )

    def test_a_sqlite_url_is_refused_as_the_postgres_leg(self):
        with self.assertRaises(cf.FigureError):
            cf.postgres_url("sqlite:///db.sqlite3")

    def test_environment_postgres_url_is_used(self):
        os.environ["DATABASE_URL"] = "postgresql://u:p@localhost:5432/db"
        self.assertEqual(cf.postgres_url(""), "postgresql://u:p@localhost:5432/db")

    def test_a_schemeless_database_url_is_not_postgres(self):
        """The value in backend/.env here: a bare hostname, no scheme.

        settings.py reads that as unparseable and falls back to sqlite without
        a word, so accepting it as the postgres leg would label a SQLite run
        `postgresql`. The floor must come out SINGLE-ENGINE instead.
        """
        os.environ["DATABASE_URL"] = "database-1.cluster.example.com"
        self.assertEqual(cf.postgres_url(""), "")

    def test_a_sqlite_environment_url_is_not_postgres(self):
        os.environ["DATABASE_URL"] = "sqlite:///db.sqlite3"
        self.assertEqual(cf.postgres_url(""), "")


class RedactTests(unittest.TestCase):
    """A password must not reach a committed artifact."""

    def test_password_is_replaced_and_host_kept(self):
        self.assertEqual(
            cf._redact("postgres://perfume:hunter2@localhost:5432/perfume_store"),
            "postgres://perfume:***@localhost:5432/perfume_store",
        )

    def test_a_url_without_credentials_is_untouched(self):
        self.assertEqual(cf._redact("sqlite:///db.sqlite3"), "sqlite:///db.sqlite3")


class RowParsingTests(unittest.TestCase):
    """What counts as a floor figure, a mutation figure, and a task id."""

    # A real row from docs/changes.md (GATE-3), with the long tail trimmed.
    GATE3_ROW = (
        "| 2026-10-04 | GATE-3 | builder | `x` | Measured locally: **1836 tests, "
        "`FAILED (failures=6, errors=1, expected failures=4)`, coverage 99.80% "
        "branch-inclusive (exit 0)** | frontend: n/a | shipped |"
    )

    def test_floor_and_mutation_figures_are_both_found(self):
        found = cf.figures_in(self.GATE3_ROW)
        self.assertEqual(
            sorted({kind for kind, _ in found}),
            ["coverage", "expected_failures", "mutation", "tests"],
        )
        self.assertIn("1836 tests", [figure for _, figure in found])
        self.assertIn("coverage 99.80%", [figure for _, figure in found])

    def test_expected_failures_is_not_a_mutation_figure(self):
        """The xfail count on a red floor run is not a recorded mutation."""
        mutations = [
            figure
            for kind, figure in cf.figures_in(self.GATE3_ROW)
            if kind == "mutation"
        ]
        self.assertEqual(mutations, ["failures=6"])

    def test_the_xfail_count_is_a_floor_figure_in_both_spellings(self):
        """`xf 4` and the runner's own `expected failures=4` are one figure."""
        for row in (
            "| 2026-10-04 | GATE-7 | builder | a | 1836 tests OK (xf 4) | s |",
            "| 2026-10-04 | GATE-7 | builder | a | FAILED, expected failures=4 | s |",
        ):
            found = [
                figure
                for kind, figure in cf.figures_in(row)
                if kind == "expected_failures"
            ]
            self.assertEqual(len(found), 1, row)

    def test_a_prose_row_with_no_figure_yields_nothing(self):
        row = (
            "| 2026-10-04 | DOC-ONLY | builder | `x` | nothing was measured "
            "| n/a | x |"
        )
        self.assertEqual(cf.figures_in(row), [])

    def test_task_id_is_the_cell_after_the_date(self):
        self.assertEqual(cf.task_id_of(self.GATE3_ROW), "GATE-3")

    def test_a_task_cell_with_a_cycle_suffix_yields_the_task(self):
        row = "| 2026-10-04 | ASYNC-2c1 cycle 2 | builder | `x` | y | n/a | shipped |"
        self.assertEqual(cf.task_id_of(row), "ASYNC-2c1")

    def test_a_non_row_line_has_no_task_id(self):
        self.assertEqual(cf.task_id_of("a paragraph about 1836 tests"), "")


class AddedLinesTests(unittest.TestCase):
    """`git diff <base> -- <path>` reads the WORKING TREE, not HEAD."""

    def setUp(self):
        self._saved_root = cf.REPO_ROOT
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cf.REPO_ROOT = self.root
        self.addCleanup(self._restore_root)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.changelog = self.root / "backend" / "docs" / "changes.md"
        self.changelog.parent.mkdir(parents=True)
        self.changelog.write_text(
            "# Changelog\n\n| 2026-10-04 | OLD-1 | builder | a | b | 1 tests | s |\n",
            encoding="utf-8",
        )
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def _restore_root(self):
        cf.REPO_ROOT = self._saved_root

    def git(self, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return result.stdout

    def append(self, line):
        with self.changelog.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")

    def test_added_lines_are_numbered_by_their_position_in_the_new_file(self):
        self.append("| 2026-10-04 | GATE-7 | builder | a | b | 1836 tests | s |")
        added = cf.added_lines(self.base, "backend/docs/changes.md")
        self.assertEqual(list(added), [4])
        self.assertIn("GATE-7", added[4])

    def test_an_uncommitted_row_is_already_visible(self):
        """The gate has to work on a row that has not been committed yet."""
        self.append("| 2026-10-04 | GATE-8 | builder | a | b | 1836 tests | s |")
        self.assertIn(4, cf.added_lines(self.base, "backend/docs/changes.md"))

    def test_a_committed_row_stays_visible(self):
        self.append("| 2026-10-04 | GATE-9 | builder | a | b | 1836 tests | s |")
        self.git("add", "-A")
        self.git("commit", "-qm", "row")
        added = cf.added_lines(self.base, "backend/docs/changes.md")
        self.assertEqual(list(added), [4])

    def test_no_added_lines_is_an_empty_mapping_not_an_error(self):
        self.assertEqual(cf.added_lines(self.base, "backend/docs/changes.md"), {})


class CheckGateTests(unittest.TestCase):
    """`check` flags a figure with no artifact and stays quiet about prose."""

    def setUp(self):
        self._saved_root = cf.REPO_ROOT
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cf.REPO_ROOT = self.root
        self.addCleanup(self._restore_root)
        (self.root / "backend" / "docs").mkdir(parents=True)
        self.changelog = self.root / "backend" / "docs" / "changes.md"
        self.changelog.write_text("# Changelog\n\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def _restore_root(self):
        cf.REPO_ROOT = self._saved_root

    def git(self, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return result.stdout

    def run_check(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            status = cf.cmd_check(
                cf.build_parser().parse_args(
                    ["check", "--base", self.base, "--path", "backend/docs/changes.md"]
                )
            )
        return status, buffer.getvalue()

    def append(self, line):
        with self.changelog.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")

    def write_floor_artifact(self, task="GATE-7", failures=6, errors=1):
        (self.root / "scripts" / "figures").mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "floor",
            "task": task,
            "engines": [
                {
                    "engine": "postgresql",
                    "run": {
                        "verdict": "FAILED" if failures is not None else "OK",
                        "failures": failures,
                        "errors": errors,
                        "expected_failures": 4,
                    },
                }
            ],
        }
        (self.root / "scripts" / "figures" / f"{task}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_a_floor_figure_without_an_artifact_fails_with_its_row_number(self):
        self.append("| 2026-10-04 | GATE-7 | builder | a | b | 1836 tests | s |")
        status, out = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("backend/docs/changes.md:3", out)
        self.assertIn("GATE-7", out)
        self.assertIn("scripts/figures/GATE-7.json", out)

    def test_a_mutation_figure_wants_a_manifest_not_a_floor_artifact(self):
        self.append(
            "| 2026-10-04 | GATE-7 | builder | a | b | mutation M1: FAILED, "
            "failures=3 | s |"
        )
        status, out = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("scripts/mutation_evidence/GATE-7.json", out)
        self.assertNotIn("scripts/figures/GATE-7.json", out)

    def test_the_matching_artifact_makes_the_row_pass(self):
        self.append("| 2026-10-04 | GATE-7 | builder | a | b | 1836 tests | s |")
        (self.root / "scripts" / "figures").mkdir(parents=True)
        (self.root / "scripts" / "figures" / "GATE-7.json").write_text(
            json.dumps({"task": "GATE-7", "kind": "floor"}), encoding="utf-8"
        )
        status, out = self.run_check()
        self.assertEqual(status, 0)
        self.assertIn("0 figure(s) with no backing artifact", out)

    def test_a_mutation_manifest_satisfies_a_mutation_figure(self):
        self.append(
            "| 2026-10-04 | GATE-7 | builder | a | b | FAILED, failures=3 | s |"
        )
        (self.root / "scripts" / "mutation_evidence").mkdir(parents=True)
        (self.root / "scripts" / "mutation_evidence" / "GATE-7.json").write_text(
            json.dumps({"task": "GATE-7"}), encoding="utf-8"
        )
        self.assertEqual(self.run_check()[0], 0)

    def test_a_floor_artifact_that_records_the_count_satisfies_the_figure(self):
        """A red engine's failure count is a measured figure like any other."""
        self.append(
            "| 2026-10-04 | GATE-7 | builder | a | b | FAILED (failures=6, errors=1, "
            "expected failures=4), cov 99.80% | s |"
        )
        self.write_floor_artifact(failures=6, errors=1)
        status, out = self.run_check()
        self.assertEqual(status, 0, out)

    def test_a_floor_artifact_that_records_a_different_count_does_not(self):
        """Existence is not enough for this class: the value has to match."""
        self.append(
            "| 2026-10-04 | GATE-7 | builder | a | b | FAILED, failures=3 | s |"
        )
        self.write_floor_artifact(failures=6, errors=1)
        status, out = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("failures=3", out)

    def test_a_green_floor_artifact_never_vouches_for_a_failure_count(self):
        """A green engine records null, so it can never answer with 6."""
        self.append(
            "| 2026-10-04 | GATE-7 | builder | a | b | FAILED, failures=4 | s |"
        )
        self.write_floor_artifact(failures=None, errors=None)
        self.assertEqual(self.run_check()[0], 1)

    def test_a_corrupt_artifact_cannot_vouch_for_anything(self):
        self.append(
            "| 2026-10-04 | GATE-7 | builder | a | b | FAILED, failures=6 | s |"
        )
        (self.root / "scripts" / "figures").mkdir(parents=True)
        (self.root / "scripts" / "figures" / "GATE-7.json").write_text(
            "{not json", encoding="utf-8"
        )
        self.assertEqual(self.run_check()[0], 1)

    def test_a_figure_with_no_task_id_is_flagged_rather_than_skipped(self):
        self.append("| | | | | | `Ran 1836 / OK / xf 4 / 99.80%` | |")
        status, out = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("NO TASK ID", out)

    def test_a_row_with_no_figure_is_not_inspected(self):
        self.append(
            "| 2026-10-04 | DOC-ONLY | builder | a | no suite re-run | n/a | s |"
        )
        status, out = self.run_check()
        self.assertEqual(status, 0)
        self.assertIn("0 row(s) with figures", out)

    def test_a_mention_of_the_placeholder_is_not_a_figure(self):
        """`failures=N` in prose names the pattern; it carries no figure."""
        self.append(
            "| 2026-10-04 | GATE-7 | builder | a | `check` refuses a failures=N "
            "with no artifact | n/a | s |"
        )
        self.assertEqual(self.run_check()[0], 0)

    def test_nothing_added_is_an_honest_zero_not_a_pass(self):
        """The loud notice matters: an empty scan that exits 0 reads as green."""
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            status = cf.cmd_check(
                cf.build_parser().parse_args(
                    ["check", "--base", self.base, "--path", "backend/docs/changes.md"]
                )
            )
        self.assertEqual(status, 0)
        self.assertIn("honest zero", buffer.getvalue())


class RecordingRunTestsTests(unittest.TestCase):
    """The recorder must observe, not interfere, and must restore the module."""

    def test_captured_labels_and_output_are_passed_through_unchanged(self):
        class Fake:
            def __init__(self):
                self.calls = []

            def run_tests(self, labels, coverage=True):
                self.calls.append((labels, coverage))
                return f"OUTPUT for {labels}"

        module = Fake()
        with cf.recording_run_tests(module) as captured:
            first = module.run_tests(["orders"], False)
            self.assertEqual(first, "OUTPUT for ['orders']")
            module.run_tests(["cart"], True)
        self.assertEqual(
            captured,
            [(["orders"], "OUTPUT for ['orders']"), (["cart"], "OUTPUT for ['cart']")],
        )
        self.assertEqual(module.calls, [(["orders"], False), (["cart"], True)])

    def test_the_original_function_is_restored_on_error(self):
        class Fake:
            def run_tests(self, labels, coverage=True):
                return "OUT"

        module = Fake()
        self.assertNotIn("run_tests", module.__dict__)
        with self.assertRaises(RuntimeError):
            with cf.recording_run_tests(module):
                raise RuntimeError("replay blew up")
        # The instance attribute the recorder installed is gone, so the class
        # method is what a later replay resolves -- compared by __func__, since
        # each attribute access builds a fresh bound-method object.
        self.assertNotIn("run_tests", module.__dict__)
        self.assertIs(module.run_tests.__func__, Fake.run_tests)


class FakeOutcome:
    """The shape MutationOutcome exposes that this script reads."""

    def __init__(self, mutation_id, status, detail):
        self.mutation_id = mutation_id
        self.status = status
        self.detail = detail


class FakeMutationModule:
    """A stand-in for scripts/mutation_evidence.py, to test the pairing only.

    The real replay is mutation_evidence.py's job and is proven by running it;
    what is under test here is that a run is paired to the mutation whose
    `test_scope` produced it, and that a mutation which never reached a run is
    reported as having run nothing. `skip` names the mutations that abort before
    a run, which is what a `find` that no longer occurs does.
    """

    class EvidenceError(Exception):
        pass

    def __init__(self, mutations, outputs, skip=()):
        self._manifest = {"mutations": mutations}
        self._outputs = list(outputs)
        self._skip = set(skip)

    def load_manifest(self, task):
        return self._manifest

    def run_tests(self, labels, coverage=True):
        return self._outputs.pop(0)

    def run_task(self, task):
        outcomes = []
        for mutation in self._manifest["mutations"]:
            if mutation["id"] in self._skip:
                outcomes.append(
                    FakeOutcome(
                        mutation["id"], "error", "'find' text occurs 0 times"
                    )
                )
                continue
            self.run_tests(list(mutation.get("test_scope", [])))
            outcomes.append(
                FakeOutcome(
                    mutation["id"], "held", "every expected test failed as recorded"
                )
            )
        return outcomes


class MutationPairingTests(unittest.TestCase):
    """`mutation` pairs each run with its own mutation, by test scope."""

    def setUp(self):
        self._saved_loader = cf._load_mutation_module
        self.addCleanup(self._restore)

    def _restore(self):
        cf._load_mutation_module = self._saved_loader

    def install(self, module):
        cf._load_mutation_module = lambda: module
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            status = cf.cmd_mutation(
                cf.build_parser().parse_args(["mutation", "--task", "T-1"])
            )
        return status, buffer.getvalue()

    def test_each_bullet_carries_its_own_run_and_count(self):
        module = FakeMutationModule(
            [
                {"id": "M1", "test_scope": ["orders"]},
                {"id": "M2", "test_scope": ["cart"]},
            ],
            [
                "Ran 89 tests in 3.1s\nFAILED (failures=3, expected failures=1)\n",
                "Ran 41 tests in 1.9s\nFAILED (failures=1)\n",
            ],
        )
        status, out = self.install(module)
        self.assertEqual(status, 0)
        self.assertIn("mutation M1: FAILED (failures=3, expected failures=1), Ran 89 tests", out)
        self.assertIn("mutation M2: FAILED (failures=1), Ran 41 tests", out)
        self.assertIn("2/2 mutations killed", out)

    def test_a_mutation_that_never_ran_gets_no_figure(self):
        """The `find` that no longer occurs aborts before any suite runs.

        Pairing by position would hand that mutation the NEXT mutation's
        failures. It must print NO RUN and nothing countable.
        """
        module = FakeMutationModule(
            [
                {"id": "M1", "test_scope": ["orders"]},
                {"id": "M2", "test_scope": ["orders"]},
            ],
            [
                "Ran 89 tests in 3.1s\nFAILED (failures=3)\n",
                "Ran 89 tests in 3.2s\nFAILED (failures=7)\n",
            ],
        )
        status, out = self.install(module)
        self.assertEqual(status, 0)
        self.assertIn("mutation M1: FAILED (failures=3), Ran 89 tests", out)
        self.assertIn("mutation M2: FAILED (failures=7), Ran 89 tests", out)

    def test_a_mutation_with_no_run_at_all_is_named_as_such(self):
        module = FakeMutationModule(
            [
                {"id": "M1", "test_scope": ["orders"]},
                {"id": "M2", "test_scope": ["cart"]},
            ],
            ["Ran 89 tests in 3.1s\nFAILED (failures=3)\n"],
            skip=["M2"],
        )
        status, out = self.install(module)
        self.assertEqual(status, 0)
        self.assertIn(
            "mutation M2: NO RUN -- error: 'find' text occurs 0 times", out
        )
        self.assertIn("mutation M1: FAILED (failures=3), Ran 89 tests", out)
        self.assertNotIn("failures=0", out)
        self.assertIn("1/2 mutations killed", out)

    def test_an_outcome_order_that_does_not_match_the_manifest_is_refused(self):
        """A gate that reordered its outcomes would otherwise mislabel bullets."""

        class Reordering(FakeMutationModule):
            def run_task(self, task):
                outcomes = super().run_task(task)
                return list(reversed(outcomes))

        module = Reordering(
            [
                {"id": "M1", "test_scope": ["orders"]},
                {"id": "M2", "test_scope": ["cart"]},
            ],
            [
                "Ran 89 tests in 3.1s\nFAILED (failures=3)\n",
                "Ran 41 tests in 1.9s\nFAILED (failures=1)\n",
            ],
        )
        with self.assertRaises(cf.FigureError):
            self.install(module)


if __name__ == "__main__":
    unittest.main()

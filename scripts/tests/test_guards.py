"""Hermetic tests for the two silent-loss guards.

Both guards exist because the failure they detect is an ABSENCE: namespace
packages keep imports working after a marker is deleted, and a runner that
collected no tests still exits 0. A test that only passed could therefore be a
test whose input was never read, so every case below builds its own tree or
writes its own runner output in a temporary directory and asserts the exit
code and the message, not just "no exception".

The runner lines are the shapes unittest and Django actually render
(``Ran 1 test in ...`` singular, ``Found N test(s).`` literal), taken from
their sources rather than invented.
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import guard_discovery  # noqa: E402
import guard_packages  # noqa: E402

RAN_MANY = "Ran 2538 tests in 142.317s\n\nOK\n"
FOUND_MANY = "Found 2538 test(s).\nCreating test database for alias 'default'...\n"
RAN_ONE = "Ran 1 test in 0.001s\n\nOK\n"
RAN_ZERO = "Ran 0 tests in 0.000s\n\nOK\n"


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._registry = tempfile.TemporaryDirectory()
        self.addCleanup(self._registry.cleanup)
        self.tmp = Path(self._registry.name)


class PackageGuardTests(TempDirTest):
    def test_directory_with_marker_is_clean(self):
        (self.tmp / "app").mkdir()
        (self.tmp / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.tmp / "app" / "models.py").write_text("", encoding="utf-8")

        self.assertEqual(guard_packages.find_missing(self.tmp), [])

    def test_missing_marker_is_reported(self):
        (self.tmp / "app").mkdir()
        (self.tmp / "app" / "models.py").write_text("", encoding="utf-8")

        self.assertEqual(guard_packages.find_missing(self.tmp), [self.tmp / "app"])

    def test_nested_directory_missing_marker_is_reported(self):
        (self.tmp / "app" / "sub").mkdir(parents=True)
        (self.tmp / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.tmp / "app" / "sub" / "views.py").write_text("", encoding="utf-8")

        self.assertEqual(
            guard_packages.find_missing(self.tmp), [self.tmp / "app" / "sub"]
        )

    def test_excluded_directories_are_not_inspected(self):
        for name in sorted(guard_packages.EXCLUDED_DIRS):
            (self.tmp / name).mkdir()
            (self.tmp / name / "module.py").write_text("", encoding="utf-8")

        self.assertEqual(guard_packages.find_missing(self.tmp), [])

    def test_walk_root_is_not_required_to_be_a_package(self):
        # backend/ holds manage.py and is executed as a script; a marker there
        # would be a behaviour change, not a repair.
        (self.tmp / "manage.py").write_text("", encoding="utf-8")

        self.assertEqual(guard_packages.find_missing(self.tmp), [])

    def test_directory_without_python_files_is_not_a_package_candidate(self):
        (self.tmp / "docs").mkdir()
        (self.tmp / "docs" / "readme.md").write_text("", encoding="utf-8")

        self.assertEqual(guard_packages.find_missing(self.tmp), [])

    def test_exclusion_applies_below_the_root_too(self):
        (self.tmp / "app").mkdir()
        (self.tmp / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.tmp / "app" / "node_modules").mkdir()
        (self.tmp / "app" / "node_modules" / "index.js").write_text(
            "", encoding="utf-8"
        )
        (self.tmp / "app" / "node_modules" / "stray.py").write_text(
            "", encoding="utf-8"
        )

        self.assertEqual(guard_packages.find_missing(self.tmp), [])

    def test_every_missing_directory_is_listed_in_a_stable_order(self):
        for name in ("zulu", "alpha"):
            (self.tmp / name).mkdir()
            (self.tmp / name / "module.py").write_text("", encoding="utf-8")

        self.assertEqual(
            guard_packages.find_missing(self.tmp),
            [self.tmp / "alpha", self.tmp / "zulu"],
        )

    def test_cli_reports_the_exact_missing_directory(self):
        (self.tmp / "app").mkdir()
        (self.tmp / "app" / "models.py").write_text("", encoding="utf-8")

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = guard_packages.main([str(self.tmp)])

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stdout.getvalue().splitlines(), [(self.tmp / "app").as_posix()]
        )
        self.assertIn("lack __init__.py", stderr.getvalue())

    def test_cli_exits_zero_when_every_directory_is_a_package(self):
        (self.tmp / "app").mkdir()
        (self.tmp / "app" / "__init__.py").write_text("", encoding="utf-8")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = guard_packages.main([str(self.tmp)])

        self.assertEqual(exit_code, 0)
        self.assertIn("clean", stdout.getvalue())

    def test_missing_root_is_an_error_not_a_clean_tree(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = guard_packages.main([str(self.tmp / "absent")])

        self.assertEqual(exit_code, 2)
        self.assertIn("not a directory", stderr.getvalue())


class DiscoveryParsingTests(unittest.TestCase):
    def test_ran_line_yields_the_count(self):
        self.assertEqual(guard_discovery.extract_count(RAN_MANY), 2538)

    def test_singular_ran_line_yields_one(self):
        self.assertEqual(guard_discovery.extract_count(RAN_ONE), 1)

    def test_found_line_yields_the_count(self):
        self.assertEqual(guard_discovery.extract_count(FOUND_MANY), 2538)

    def test_genuinely_unparseable_output_is_refused(self):
        with self.assertRaises(guard_discovery.GuardError):
            guard_discovery.extract_count("collected nothing\n")

    def test_conflicting_found_and_ran_counts_are_refused(self):
        output = FOUND_MANY + RAN_MANY.replace("2538", "2537")

        with self.assertRaises(guard_discovery.GuardError):
            guard_discovery.extract_count(output)

    def test_two_different_ran_counts_in_one_log_are_refused(self):
        output = RAN_MANY + "Ran 12 tests in 1.000s\n"

        with self.assertRaises(guard_discovery.GuardError):
            guard_discovery.extract_count(output)

    def test_two_different_found_counts_in_one_log_are_refused(self):
        output = "Found 10 test(s).\nFound 11 test(s).\n"

        with self.assertRaises(guard_discovery.GuardError):
            guard_discovery.extract_count(output)


class DiscoveryGateTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.output_file = self.tmp / "runner.log"
        self.floor_file = self.tmp / "discovery_floor.json"

    def write_floor(self, tests):
        self.floor_file.write_text(json.dumps({"tests": tests}), encoding="utf-8")

    def run_guard(self, output, *extra):
        self.output_file.write_text(output, encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = guard_discovery.main(
                [
                    "--input",
                    str(self.output_file),
                    "--floor",
                    str(self.floor_file),
                    *extra,
                ]
            )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_count_below_the_floor_is_red(self):
        self.write_floor(100)

        exit_code, _, stderr = self.run_guard("Ran 99 tests in 4.000s\n")

        self.assertEqual(exit_code, 1)
        self.assertIn("below the floor", stderr)

    def test_count_at_the_floor_is_green(self):
        self.write_floor(100)

        exit_code, stdout, _ = self.run_guard("Ran 100 tests in 4.000s\n")

        self.assertEqual(exit_code, 0)
        self.assertIn("100 tests (floor 100)", stdout)

    def test_count_above_the_floor_is_green(self):
        self.write_floor(100)

        exit_code, stdout, _ = self.run_guard("Ran 101 tests in 4.000s\n")

        self.assertEqual(exit_code, 0)
        self.assertIn("101 tests (floor 100)", stdout)

    def test_zero_discovered_tests_are_red_even_at_the_smallest_floor(self):
        self.write_floor(1)

        exit_code, _, stderr = self.run_guard(RAN_ZERO)

        self.assertEqual(exit_code, 1)
        self.assertIn("0 tests", stderr)

    def test_absent_floor_fails_closed_instead_of_guessing(self):
        self.floor_file.unlink(missing_ok=True)
        self.output_file.write_text(RAN_MANY, encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = guard_discovery.main(
                ["--input", str(self.output_file), "--floor", str(self.floor_file)]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("floor file not found", stderr.getvalue())

    def test_floor_zero_is_refused(self):
        self.write_floor(0)

        exit_code, _, stderr = self.run_guard(RAN_MANY)

        self.assertEqual(exit_code, 2)
        self.assertIn(">= 1", stderr)

    def test_floor_boolean_is_refused(self):
        self.floor_file.write_text(json.dumps({"tests": True}), encoding="utf-8")

        exit_code, _, _ = self.run_guard(RAN_MANY)

        self.assertEqual(exit_code, 2)

    def test_unreadable_floor_json_is_refused(self):
        self.floor_file.write_text("{not json", encoding="utf-8")

        exit_code, _, stderr = self.run_guard(RAN_MANY)

        self.assertEqual(exit_code, 2)
        self.assertIn("unreadable", stderr)

    def test_floor_value_must_be_an_integer(self):
        self.floor_file.write_text(json.dumps({"tests": "100"}), encoding="utf-8")

        exit_code, _, stderr = self.run_guard(RAN_MANY)

        self.assertEqual(exit_code, 2)
        self.assertIn(">= 1", stderr)

    def test_floor_without_a_tests_key_is_refused(self):
        self.floor_file.write_text(json.dumps({"floor": 100}), encoding="utf-8")

        exit_code, _, stderr = self.run_guard(RAN_MANY)

        self.assertEqual(exit_code, 2)
        self.assertIn(">= 1", stderr)

    def test_input_dash_reads_stdin(self):
        self.write_floor(7)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("Ran 7 tests in 1.000s\n")):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = guard_discovery.main(
                    ["--input", "-", "--floor", str(self.floor_file)]
                )

        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertIn("7 tests (floor 7)", stdout.getvalue())

    def test_absent_input_file_is_an_error_not_a_zero_count(self):
        self.write_floor(7)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = guard_discovery.main(
                [
                    "--input",
                    str(self.tmp / "absent.log"),
                    "--floor",
                    str(self.floor_file),
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("could not read input", stderr.getvalue())

    def test_run_mode_parses_a_named_command(self):
        self.write_floor(3)
        command = [
            sys.executable,
            "-c",
            "print('Found 3 test(s).')",
        ]

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = guard_discovery.main(
                [
                    "--run",
                    "--floor",
                    str(self.floor_file),
                    "--command",
                    *command,
                ]
            )

        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertIn("3 tests (floor 3)", stdout.getvalue())

    def test_run_mode_command_failure_is_not_a_verdict(self):
        self.write_floor(3)
        command = [sys.executable, "-c", "raise SystemExit(3)"]

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = guard_discovery.main(
                ["--run", "--floor", str(self.floor_file), "--command", *command]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("exited 3", stderr.getvalue())


class DefaultDiscoveryCommandTests(TempDirTest):
    """The default ``--command`` must be an invocation Django actually accepts."""

    def setUp(self):
        super().setUp()
        self.floor_file = self.tmp / "discovery_floor.json"
        self.floor_file.write_text(json.dumps({"tests": 4}), encoding="utf-8")

    def test_default_command_builds_the_suite_instead_of_passing_an_unknown_flag(self):
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"] = command
            return subprocess.CompletedProcess(
                command, 0, stdout="Found 4 test(s).\n", stderr=""
            )

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(guard_discovery.subprocess, "run", fake_run):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = guard_discovery.main(
                    ["--run", "--floor", str(self.floor_file)]
                )

        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertEqual(seen["command"][:2], [sys.executable, "manage.py"])
        self.assertIn("-c", seen["command"])
        # Django 6.1.1 rejects `manage.py test --dry-run`, so the default asks
        # the runner for its suite rather than passing an unknown flag.
        self.assertNotIn("--dry-run", seen["command"])
        self.assertIn("shell", seen["command"])
        self.assertIn("4 tests (floor 4)", stdout.getvalue())

    def test_default_command_program_is_valid_python(self):
        compile(guard_discovery.DISCOVERY_PROGRAM, "<discovery>", "exec")

    def test_default_command_failure_is_not_read_as_a_count(self):
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom\n")

        stderr = io.StringIO()
        with mock.patch.object(guard_discovery.subprocess, "run", fake_run):
            with contextlib.redirect_stderr(stderr):
                exit_code = guard_discovery.main(
                    ["--run", "--floor", str(self.floor_file)]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("exited 1", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

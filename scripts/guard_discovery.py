"""Fail when test discovery finds zero tests or fewer than the recorded floor.

The runner's exit code is not evidence of discovery. An empty or partially
importable test tree makes Django collect a smaller suite (or none) and the
runner still exits 0 on what it collected, so a wipe that deletes test
directories reads as a green run. The floor lives in
``scripts/figures/discovery_floor.json``; this script never writes it and never
guesses a value when it is absent, because a floor the gate invents agrees with
whatever the run just did.

The count is read from rendered runner output: either the final
``Ran N tests in Xs`` line or Django's initial ``Found N test(s).`` line. That
output arrives from ``--input``, from stdin, or from a command the caller names
(``--run``; see ``DEFAULT_COMMAND`` for the discovery pass it uses by default).
The floor file is a JSON object with an integer ``tests`` key. A command that
fails is judged as an error, never as a count.

Exit codes: 0 = count at or above the floor, 1 = count below it or zero,
2 = the run could not be judged (no floor file, unreadable output, command).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FLOOR = REPO_ROOT / "scripts" / "figures" / "discovery_floor.json"
DEFAULT_WORKDIR = REPO_ROOT / "backend"

# Django's test command has no --dry-run flag (the 6.1.1 in this venv answers
# `manage.py test: error: unrecognized arguments: --dry-run`), so the cheap
# discovery pass asks the configured runner for its suite instead. That is the
# same build_suite pass the runner performs before creating a database, and it
# renders the same `Found N test(s).` line.
DISCOVERY_PROGRAM = (
    "from django.conf import settings;"
    "from django.test.utils import get_runner;"
    "print('Found %d test(s).' % "
    "get_runner(settings)(verbosity=0).build_suite(None).countTestCases())"
)
DEFAULT_COMMAND = [sys.executable, "manage.py", "shell", "-c", DISCOVERY_PROGRAM]

# Rendered by unittest as `Ran <N> tests in <T>s`, or `Ran 1 test in <T>s`.
RAN_RE = re.compile(r"^Ran (?P<count>\d+) tests? in \d+(?:\.\d+)?s\s*$", re.MULTILINE)
# Rendered by Django's runner as `Found <N> test(s).`
FOUND_RE = re.compile(r"^Found (?P<count>\d+) test\(s\)\.\s*$", re.MULTILINE)

# Enough context to name the cause when a caller-supplied command fails,
# without letting a traceback dominate the terminal.
DIAGNOSTIC_TAIL_LINES = 5


class GuardError(Exception):
    """A condition the guard cannot judge; never converted into a count."""


def _distinct_counts(pattern: re.Pattern[str], output: str) -> list[int]:
    return sorted({int(match.group("count")) for match in pattern.finditer(output)})


def extract_count(output: str) -> int:
    """Return the one test count in ``output``; refuse ambiguity.

    A log that contains two different counts is two runs or a partial capture
    in one file, and picking either one would be a guess.
    """
    ran = _distinct_counts(RAN_RE, output)
    found = _distinct_counts(FOUND_RE, output)
    if len(ran) > 1:
        raise GuardError(f"conflicting 'Ran' counts in the output: {ran}")
    if len(found) > 1:
        raise GuardError(f"conflicting 'Found' counts in the output: {found}")
    if ran and found and ran[0] != found[0]:
        raise GuardError(
            f"discovery reported {found[0]} tests but the run reported {ran[0]}"
        )
    if ran:
        return ran[0]
    if found:
        return found[0]
    raise GuardError(
        "no 'Ran N tests in ...' or 'Found N test(s).' line in the output; "
        "refusing to report a count"
    )


def load_floor(path: Path) -> int:
    """Read the floor; an absent or unusable file is an error, not a default."""
    if not path.is_file():
        raise GuardError(
            f"floor file not found: {path} -- record a measured count there "
            "first; refusing to guess"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"floor file is unreadable: {path}: {exc}") from exc
    tests = data.get("tests") if isinstance(data, dict) else None
    # bool is an int subclass; a JSON `true` is not a floor.
    if isinstance(tests, bool) or not isinstance(tests, int) or tests < 1:
        raise GuardError(f'floor file must hold an integer "tests" >= 1: {path}')
    return tests


def read_output(args: argparse.Namespace) -> str:
    if args.input is not None:
        if args.input == "-":
            return sys.stdin.read()
        return Path(args.input).read_text(encoding="utf-8", errors="replace")
    if args.run:
        command = list(args.command)
        if not command:
            raise GuardError("--command was given without a command to run")
        try:
            completed = subprocess.run(
                command,
                cwd=str(args.workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                # The return code is the verdict here, not an exception: a red
                # command is reported as an error with its diagnostic tail.
                check=False,
            )
        except OSError as exc:
            raise GuardError(f"could not run {command}: {exc}") from exc
        if completed.returncode != 0:
            tail = "\n".join(
                (completed.stderr or completed.stdout)
                .strip()
                .splitlines()[-DIAGNOSTIC_TAIL_LINES:]
            )
            raise GuardError(
                f"{' '.join(command)} exited {completed.returncode}: {tail}"
            )
        return completed.stdout + completed.stderr
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise GuardError("no output to judge; pass --input, pipe stdin, or use --run")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare discovered test count against the recorded floor."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--input",
        metavar="PATH",
        help="file holding runner output, or '-' for stdin",
    )
    source.add_argument(
        "--run",
        action="store_true",
        help="run --command and parse its output instead of reading a file",
    )
    parser.add_argument(
        "--command",
        nargs=argparse.REMAINDER,
        default=DEFAULT_COMMAND,
        help=(
            "command for --run (default: build the configured runner's suite "
            "through manage.py shell); must be last"
        ),
    )
    parser.add_argument(
        "--workdir",
        default=str(DEFAULT_WORKDIR),
        help="working directory for --run (default: the repo's backend/)",
    )
    parser.add_argument(
        "--floor",
        default=str(DEFAULT_FLOOR),
        help="floor JSON (default: scripts/figures/discovery_floor.json)",
    )
    args = parser.parse_args(argv)

    try:
        floor = load_floor(Path(args.floor))
        output = read_output(args)
        count = extract_count(output)
    except GuardError as error:
        print(f"guard_discovery: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"guard_discovery: could not read input: {error}", file=sys.stderr)
        return 2

    if count == 0:
        print("guard_discovery: discovery found 0 tests", file=sys.stderr)
        return 1
    if count < floor:
        print(
            f"guard_discovery: discovery found {count} tests, below the floor "
            f"of {floor}; test modules are being skipped",
            file=sys.stderr,
        )
        return 1
    print(f"guard_discovery: {count} tests (floor {floor})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

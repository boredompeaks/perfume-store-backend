#!/usr/bin/env python3
"""Print the changelog's figures instead of typing them (GATE-7).

Three subcommands, one job each:

  floor     run the backend suite with coverage on every engine available,
            save the measured numbers to scripts/figures/<task-id>.json, and
            print a bullet ready to paste into a docs/changes.md row
  mutation  replay a task's recorded mutations through scripts/mutation_evidence.py
            and print one paste-ready bullet per mutation: the verdict, the
            failure count, the test count
  check     read the rows docs/changes.md has gained since a base ref and fail
            on any floor figure or `failures=N` mutation figure that has no
            artifact behind it

The rule this enforces is the one in docs/conventions.md: a figure in the
changelog is printed by this script, not typed by a builder. A hand-typed
figure is the mechanism behind every "stale figures" finding in this repo's
history -- the number survives in prose long after the thing it described has
moved, and nothing in the suite notices.

Stdlib only, so it can run in a CI step with no install and cannot disagree
with requirements.txt about what it imports.

Every number this prints comes out of a subprocess it just ran. Nothing is
read from a previous report, a previous artifact or a comment: `floor` reads
the runner's own summary line and `coverage report`'s TOTAL row, `mutation`
replays the mutation and reads the runner again, and both record the exit codes
alongside the counts. Conventions this script exists to keep:

  * a green run reports NO failures, and nothing is not zero -- `failures` is
    null on an OK run, never 0;
  * `failures` is read off the `FAILED (failures=N)` line, never off a run
    summary or a count of headers;
  * the engine is measured, never declared. settings.py falls back to SQLite
    without a word when DATABASE_URL is unset or malformed, so "I did not set
    it" says nothing about which engine ran;
  * the floor is a pair (SQLite + PostgreSQL). A single engine is labelled
    SINGLE-ENGINE in the bullet it prints and in the artifact, so a half floor
    cannot be pasted into a row as if it were the whole thing.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"
CHANGELOG = "backend/docs/changes.md"


def figures_dir() -> pathlib.Path:
    """Where `floor` saves its artifact. Resolved per call, not at import.

    A module-level constant would be frozen against the tree this file was
    imported from, which is exactly what a unit test cannot then point at a
    fixture. `check` reads the same directory, so both halves move together.
    """
    return REPO_ROOT / "scripts" / "figures"


def mutation_dir() -> pathlib.Path:
    """Where `check` looks for the manifest behind a mutation figure."""
    return REPO_ROOT / "scripts" / "mutation_evidence"

# An explicit sqlite URL rather than the absence of DATABASE_URL:
# settings._databases_from_url() cannot tell "unset" from "malformed" -- both
# land on the same silent sqlite fallback -- whereas an explicit sqlite URL is
# honoured in every environment and is recorded in the artifact, so the engine
# a figure was measured on is a fact in the file rather than an assumption.
SQLITE_URL = "sqlite:///db.sqlite3"

ENGINE_VENDORS = {
    "django.db.backends.sqlite3": "sqlite",
    "django.db.backends.postgresql": "postgresql",
}

# Reads connection.settings_dict["ENGINE"] in a child process that carries the
# same environment as the suite run, which is the only way to be sure the two
# agree: a probe in this process would resolve .env and os.environ for itself,
# not for the run.
ENGINE_PROBE = (
    "import os;"
    "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings');"
    "import django;"
    "django.setup();"
    "from django.db import connection;"
    "print(connection.settings_dict['ENGINE'])"
)

# `FAILED (failures=6, errors=1, expected failures=4)` / `OK (expected
# failures=4)`. Anchored, and the whole comma-separated body is matched part by
# part, because a bare `(failures|errors)=(\d+)` scan of the body reads
# `expected failures=4` as `failures=4` and reports 4 for a run that failed 6.
SUMMARY_RE = re.compile(r"^(?P<verdict>FAILED|OK)\b\s*(?:\((?P<body>[^)]*)\))?\s*$")
COUNT_RE = re.compile(
    r"^\s*(?P<key>expected failures|expectedFailures|failures|errors)"
    r"\s*=\s*(?P<value>\d+)\s*$"
)
RAN_RE = re.compile(r"^Ran (\d+) tests?\b")

# The floor figure vocabulary this changelog actually uses. Kept narrow on
# purpose: a detector that flags prose which merely contains a number produces
# findings nobody acts on, and the rows here carry plenty of both.
FLOOR_PATTERNS = (
    ("tests", re.compile(r"\bRan (\d+) tests\b")),
    ("tests", re.compile(r"\b(\d[\d,]*)\s+tests\b")),
    ("coverage", re.compile(r"\bcov(?:erage)?\s+(\d+(?:\.\d+)?)\s*%")),
    ("stmts", re.compile(r"\b(\d[\d,]*)\s*stmts\b")),
    ("expected_failures", re.compile(r"\bxf\s+(\d+)\b")),
    # The long spelling of the same invariant, which is the one the runner
    # itself prints. Left out, a row could quote the xfail count in the runner's
    # own words and go unchecked -- the figure the xfail ceiling's blind spot
    # came from.
    ("expected_failures", re.compile(r"\bexpected failures\s*=\s*(\d+)")),
)
# A mutation figure. The lookbehind keeps `expected failures=4` (the runner's
# xfail count on a red floor run) out of this class, where it would demand a
# mutation manifest from a row that recorded no mutation at all.
MUTATION_FIGURE_RE = re.compile(r"(?<!expected )\bfailures\s*=\s*(\d+)")
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class FigureError(Exception):
    """A figure could not be measured. Never a figure of zero."""


def _run(command: list[str], cwd: pathlib.Path, env: dict[str, str]) -> tuple[int, str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    return result.returncode, result.stdout + result.stderr


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    if result.returncode != 0:
        raise FigureError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------


def parse_run(output: str) -> dict:
    """Read the runner's own verdict, counts and test count out of a run.

    Lines are scanned in reverse and the first summary line wins, so a test that
    prints its own `OK` cannot be mistaken for the runner's verdict. `failures`
    and `errors` are None on a green run: the runner reports nothing there, and
    reporting 0 would be inventing a count nobody measured.
    """
    verdict = None
    counts: dict[str, int] = {}
    for line in reversed(output.splitlines()):
        match = SUMMARY_RE.match(line.strip())
        if not match:
            continue
        verdict = match.group("verdict")
        for part in (match.group("body") or "").split(","):
            field = COUNT_RE.match(part)
            if not field:
                continue
            key = field.group("key")
            if key in ("expected failures", "expectedFailures"):
                key = "expected_failures"
            counts[key] = int(field.group("value"))
        break
    if verdict is None:
        raise FigureError(
            "no runner summary line (FAILED (...)/OK) in the output; the figures "
            "would be guesses, so nothing is printed"
        )

    tests = None
    for line in reversed(output.splitlines()):
        match = RAN_RE.match(line.strip())
        if match:
            tests = int(match.group(1))
            break
    if tests is None:
        raise FigureError(
            "no 'Ran N tests' line in the output; the test count is unknown"
        )

    green = verdict == "OK"
    return {
        "verdict": verdict,
        "tests": tests,
        # None, not 0, wherever the runner reported nothing: a zero here would
        # be a count nobody measured, and "nothing is not zero" is the rule this
        # script exists to keep.
        "failures": None if green else counts.get("failures"),
        "errors": None if green else counts.get("errors"),
        "expected_failures": counts.get("expected_failures"),
    }


def parse_coverage(output: str) -> dict:
    """Read the TOTAL row of `coverage report`.

    The row is positional: `TOTAL <stmts> <missed> [<branches> <partial>] <pct>`.
    Branch coverage adds the two middle columns (backend/.coveragerc sets
    branch = True), so the column count decides which shape this is. Anything
    else is a refusal rather than a guess.
    """
    for line in output.splitlines():
        if not line.startswith("TOTAL"):
            continue
        tokens = line.split()
        numbers = tokens[1:-1]
        if len(numbers) not in (2, 4) or not tokens[-1].endswith("%"):
            raise FigureError(f"unrecognised coverage TOTAL row: {line.strip()!r}")
        try:
            values = [int(value.replace(",", "")) for value in numbers]
        except ValueError as error:
            raise FigureError(
                f"unrecognised coverage TOTAL row: {line.strip()!r}"
            ) from error
        parsed = {
            "statements": values[0],
            "missed": values[1],
            "coverage_percent": tokens[-1].rstrip("%"),
            "branches": None,
            "partial_branches": None,
        }
        if len(values) == 4:
            parsed["branches"] = values[2]
            parsed["partial_branches"] = values[3]
        return parsed
    raise FigureError("no TOTAL row in `coverage report` output")


def format_verdict(run: dict) -> str:
    """Render the runner's verdict with the counts it actually reported.

    Only counts the summary line carried are rendered, and an absent count is
    None in `run` rather than 0, so nothing is invented: a green run prints the
    xfail count it reported and never a `failures=0`, and a bare `FAILED` line
    stays bare. The verdict itself is the runner's, never this function's.
    """
    parts = []
    for key in ("failures", "errors", "expected_failures"):
        value = run.get(key)
        if value is None:
            continue
        label = "expected failures" if key == "expected_failures" else key
        parts.append(f"{label}={value}")
    if not parts:
        return run["verdict"]
    return f"{run['verdict']} ({', '.join(parts)})"


def format_coverage(coverage: dict) -> str:
    detail = [f"{coverage['statements']} stmts", f"{coverage['missed']} missed"]
    if coverage["branches"] is not None:
        detail.append(f"{coverage['branches']} branches")
        detail.append(f"{coverage['partial_branches']} partial")
    return f"cov {coverage['coverage_percent']}% ({', '.join(detail)})"


# --------------------------------------------------------------------------
# floor
# --------------------------------------------------------------------------


def detect_engine(env: dict[str, str]) -> str:
    """Return the vendor ('sqlite'/'postgresql') the suite would really use."""
    code, output = _run([sys.executable, "-c", ENGINE_PROBE], BACKEND_DIR, env)
    printed = [line.strip() for line in output.splitlines() if line.strip()]
    if code != 0 or not printed:
        raise FigureError(
            "could not determine the database engine "
            f"(probe exit {code}): {output.strip()[:400]}"
        )
    vendor = ENGINE_VENDORS.get(printed[-1])
    if vendor is None:
        raise FigureError(f"unrecognised database backend {printed[-1]!r}")
    return vendor


def measure_engine(vendor: str, url: str) -> dict:
    """Run the suite with coverage on one engine and return what it reported."""
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    measured = detect_engine(env)
    if measured != vendor:
        raise FigureError(
            f"asked for {vendor} but the engine resolved to {measured}; settings "
            "falls back to sqlite without a word, so the run is refused rather "
            "than labelled with the wrong engine"
        )

    test_command = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        "manage.py",
        "test",
        "--verbosity",
        "0",
    ]
    test_exit, test_output = _run(test_command, BACKEND_DIR, env)
    # `coverage report` runs even on a red suite: the floor is the pair of the
    # verdict AND the coverage figure, and a gate that only prints the first
    # invites the second to be carried forward from somewhere else.
    cov_command = [sys.executable, "-m", "coverage", "report"]
    cov_exit, cov_output = _run(cov_command, BACKEND_DIR, env)

    return {
        "engine": measured,
        "database_url": _redact(url),
        "commands": [" ".join(test_command), " ".join(cov_command)],
        "run": parse_run(test_output),
        "coverage": parse_coverage(cov_output),
        "test_exit": test_exit,
        "coverage_exit": cov_exit,
    }


def _redact(url: str) -> str:
    """Strip the password out of a database URL before it reaches a file."""
    if "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if "@" not in rest:
        return url
    userinfo, _, host = rest.rpartition("@")
    user = userinfo.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def postgres_url(explicit: str) -> str:
    """Return the PostgreSQL URL to measure, or '' when there is none.

    Only a URL with a postgres scheme qualifies. A DATABASE_URL that merely
    looks like a database location -- the one in backend/.env here is a bare
    hostname with no scheme -- is exactly the value that makes settings fall
    back to sqlite, so accepting it would label a SQLite run as PostgreSQL.
    """
    if explicit:
        if explicit.split("://", 1)[0].lower() not in ("postgres", "postgresql"):
            raise FigureError(
                "--pg-url must carry a postgres:// or postgresql:// scheme"
            )
        return explicit
    raw = os.environ.get("DATABASE_URL", "")
    if raw.split("://", 1)[0].lower() in ("postgres", "postgresql"):
        return raw
    return ""


def cmd_floor(args: argparse.Namespace) -> int:
    """Measure the floor, write the artifact, print the bullet."""
    pg = postgres_url(args.pg_url)
    engines = [measure_engine("sqlite", SQLITE_URL)]
    if pg:
        engines.append(measure_engine("postgresql", pg))
    else:
        print(
            "changelog_figures: no PostgreSQL URL available (pass --pg-url, or "
            "export a DATABASE_URL with a postgres scheme), so this floor is "
            "SINGLE-ENGINE. The floor is a pair; a single engine is not a floor.",
            file=sys.stderr,
        )

    artifact = {
        "kind": "floor",
        "task": args.task,
        "tool": "scripts/changelog_figures.py",
        "measured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "pair_complete": len(engines) == 2,
        "engines": engines,
    }
    figures_dir().mkdir(parents=True, exist_ok=True)
    path = figures_dir() / f"{args.task}.json"
    path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    artifact_path = path.relative_to(REPO_ROOT).as_posix()
    print(
        f"{args.task} floor -- printed by `python scripts/changelog_figures.py "
        f"floor --task {args.task}`, artifact `{artifact_path}`"
    )
    for engine in engines:
        run = engine["run"]
        print(
            f"- {engine['engine']}: Ran {run['tests']} tests, "
            f"{format_verdict(run)}, "
            f"{format_coverage(engine['coverage'])}, "
            f"test exit {engine['test_exit']}, coverage exit {engine['coverage_exit']}"
        )
    if not artifact["pair_complete"]:
        print(
            "- SINGLE-ENGINE: no PostgreSQL leg was measured, so this bullet is "
            "half the floor and must not be recorded as the whole of it"
        )
    print(
        f"- artifact written: {path.relative_to(REPO_ROOT).as_posix()}"
    )
    # Exit 0 means "the figures were measured and printed", not "the suite was
    # green": a red engine is a finding to record, and the bullet above carries
    # its exit code. Exit 1 is reserved for a figure that could not be measured.
    return 0


# --------------------------------------------------------------------------
# mutation
# --------------------------------------------------------------------------


def _load_mutation_module():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import mutation_evidence
    except Exception as error:  # pragma: no cover - import failure is the message
        raise FigureError(
            f"cannot import scripts/mutation_evidence.py: {error}"
        ) from error
    return mutation_evidence


@contextlib.contextmanager
def recording_run_tests(module):
    """Capture each `run_tests` call and its output while replaying a manifest.

    MutationOutcome carries the gate's verdict and its failure count but not the
    runner's `Ran N tests` line, and this bullet has to print that count as a
    measured figure rather than a transcribed one. The labels are kept with the
    output because they are what pairs a run to its mutation: not every
    mutation reaches a suite run -- a `find` that no longer occurs aborts before
    one -- and pairing by position alone would then report one mutation's counts
    under another's name.

    The wrapped function still runs unchanged and its output is passed through
    untouched, so the verdict and the failure count in every bullet are read
    from that captured output and there is exactly one parse of it. Restored on
    the way out, including on error, so the module is left as it was found.
    """
    captured: list[tuple[list, str]] = []
    original = module.run_tests
    # Whether run_tests was an INSTANCE attribute before the swap decides how it
    # is put back: reassigning a class method as an instance attribute would
    # leave a shadow behind that no later assignment could see.
    was_instance = "run_tests" in vars(module)

    def wrapper(*args, **kwargs):
        output = original(*args, **kwargs)
        labels = args[0] if args else kwargs.get("labels", [])
        captured.append((list(labels), output))
        return output

    module.run_tests = wrapper
    try:
        yield captured
    finally:
        if was_instance:
            module.run_tests = original
        else:
            del module.run_tests


def cmd_mutation(args: argparse.Namespace) -> int:
    """Replay a manifest and print one paste-ready bullet per mutation."""
    module = _load_mutation_module()
    try:
        manifest = module.load_manifest(args.task)
        mutations = list(manifest["mutations"])
        with recording_run_tests(module) as captured:
            outcomes = module.run_task(args.task)
    except module.EvidenceError as error:
        raise FigureError(str(error)) from error

    if len(outcomes) != len(mutations):
        raise FigureError(
            f"the gate returned {len(outcomes)} outcomes for "
            f"{len(mutations)} recorded mutations; the pairing would be a guess"
        )

    held = 0
    problems = 0
    available = list(captured)
    manifest_path = (mutation_dir() / f"{args.task}.json").relative_to(REPO_ROOT)
    print(
        f"{args.task} mutation replay -- printed by `python "
        f"scripts/changelog_figures.py mutation --task {args.task}`, manifest "
        f"`{manifest_path.as_posix()}`"
    )
    for mutation, outcome in zip(mutations, outcomes):
        if outcome.mutation_id != mutation.get("id"):
            raise FigureError(
                f"outcome {outcome.mutation_id!r} does not line up with recorded "
                f"mutation {mutation.get('id')!r}; the pairing would be a guess"
            )
        scope = list(mutation.get("test_scope", []))
        index = next(
            (i for i, (labels, _) in enumerate(available) if labels == scope), None
        )
        if index is None:
            # No suite ran for this one, so there is no figure to print. Saying
            # so is the whole point: a bullet with a count on it would be a
            # number no run produced.
            problems += 1
            print(
                f"- mutation {outcome.mutation_id}: NO RUN -- {outcome.status}: "
                f"{outcome.detail}"
            )
            continue
        _, output = available.pop(index)
        run = parse_run(output)
        # The verdict and the counts below are read from the replayed run's own
        # summary, not from the manifest: claimed_count is the hand-typed figure
        # this whole script exists to stop anyone publishing.
        figures = (
            f"mutation {outcome.mutation_id}: {format_verdict(run)}, "
            f"Ran {run['tests']} tests"
        )
        if outcome.status == "held":
            held += 1
        else:
            problems += 1
        print(f"- {figures} -- {outcome.status}: {outcome.detail}")
    print(
        f"- {held}/{len(mutations)} mutations killed (the replay ran the suite "
        "once per mutation that reached a run; no whole-suite re-run, so each "
        "bullet's test count is that mutation's own scope)"
    )
    if problems:
        print(
            f"changelog_figures: {problems} of {len(mutations)} mutations did not "
            "read 'held'; each bullet above carries its own verdict and detail "
            "rather than a uniform claim",
            file=sys.stderr,
        )
    return 0


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def added_lines(base: str, path: str) -> dict[int, str]:
    """Map each line `path` gained since `base` to its line number in the file.

    `git diff <base> -- <path>` compares the base blob to the WORKING TREE, not
    to HEAD, so an uncommitted row is checked before it is committed and the
    same command keeps working afterwards. `--unified=0` is required: with
    context lines, unchanged prose is reported as added.
    """
    raw = _git("diff", "--unified=0", base, "--", path)
    added: dict[int, str] = {}
    current: str | None = None
    number = 0
    for line in raw.splitlines():
        if line.startswith("+++ "):
            post = line[6:].strip()
            current = None if post == "/dev/null" else post
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if not match or current is None:
                continue
            number = int(match.group(1))
            continue
        if current is None:
            continue
        if line.startswith("+"):
            added[number] = line[1:]
            number += 1
        elif line.startswith("-"):
            continue
    return added


def task_id_of(row: str) -> str:
    """Return the task id a changelog row belongs to, or '' when it has none.

    The id is the first cell after the date column, trimmed to its leading
    token, so `ASYNC-2c1 cycle 2` yields `ASYNC-2c1`. A row that carries a
    figure and names no task cannot be given a backing artifact by anyone, which
    is why an empty result is a finding rather than a silent skip.
    """
    if not row.startswith("|"):
        return ""
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    if len(cells) < 2:
        return ""
    match = TASK_ID_RE.match(cells[1].lstrip("*` "))
    return match.group(0) if match else ""


def figures_in(row: str) -> list[tuple[str, str]]:
    """Return (kind, figure) pairs for every floor/mutation figure in a row."""
    found = [
        (kind, match.group(0))
        for kind, pattern in FLOOR_PATTERNS
        for match in pattern.finditer(row)
    ]
    found.extend(
        ("mutation", match.group(0))
        for match in MUTATION_FIGURE_RE.finditer(row)
    )
    return found


def artifact_records_failures(path: pathlib.Path, count: int) -> bool:
    """True when a saved artifact records this exact failure count.

    The floor artifact records the failure count of a red engine, so a row may
    quote `failures=N` from the floor run rather than from a mutation replay.
    Requiring the VALUE to match, rather than only the file to exist, keeps the
    check stricter than mere existence for this class: a row that quotes 3 while
    its artifact records 6 is flagged, where an existence test would wave it
    through. Anything unreadable or malformed answers False, so a broken
    artifact cannot vouch for a figure.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("kind") == "floor":
        engines = data.get("engines")
        if not isinstance(engines, list):
            return False
        return any(
            isinstance(engine, dict)
            and isinstance(engine.get("run"), dict)
            and engine["run"].get("failures") == count
            for engine in engines
        )
    mutations = data.get("mutations")
    if not isinstance(mutations, list):
        return False
    return any(
        isinstance(mutation, dict) and mutation.get("claimed_count") == count
        for mutation in mutations
    )


def cmd_check(args: argparse.Namespace) -> int:
    """Fail on any figure in the rows added since `args.base` with no artifact."""
    added = added_lines(args.base, args.path)
    if not added:
        print(
            f"changelog_figures: {args.path} has no added lines in the diff "
            f"against {args.base} (uncommitted rows included). Nothing was "
            "checked; this is an honest zero, not a pass.",
            file=sys.stderr,
        )
        return 0

    rows = 0
    flagged = 0
    for number in sorted(added):
        row = added[number]
        if not row.startswith("|"):
            continue
        figures = figures_in(row)
        if not figures:
            continue
        rows += 1
        task = task_id_of(row)
        if not task:
            for _, figure in figures:
                flagged += 1
                print(
                    f"{args.path}:{number}  NO TASK ID  {figure!r}  -- a figure with "
                    "no task cannot be given a backing artifact"
                )
            continue
        for kind, figure in figures:
            if kind == "mutation":
                mutation_figure = MUTATION_FIGURE_RE.search(figure)
                count = int(mutation_figure.group(1)) if mutation_figure else None
                manifest = mutation_dir() / f"{task}.json"
                floor = figures_dir() / f"{task}.json"
                if manifest.exists() or (
                    count is not None and artifact_records_failures(floor, count)
                ):
                    continue
                artifact = manifest
            else:
                artifact = figures_dir() / f"{task}.json"
                if artifact.exists():
                    continue
            flagged += 1
            print(
                f"{args.path}:{number}  {task}  {kind}  {figure!r}  -- no backing "
                f"artifact recording it ({artifact.relative_to(REPO_ROOT).as_posix()})"
            )

    print(
        f"changelog_figures: {rows} row(s) with figures among the {len(added)} "
        f"line(s) {args.path} gained since {args.base}; {flagged} figure(s) with "
        "no backing artifact"
    )
    return 1 if flagged else 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="changelog_figures.py",
        description=(
            "Print the figures docs/changes.md records instead of typing them."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    floor = sub.add_parser(
        "floor",
        help="run the suite with coverage, save the artifact, print the bullet",
    )
    floor.add_argument("--task", required=True, help="task id, e.g. GATE-7")
    floor.add_argument(
        "--pg-url",
        default="",
        help=(
            "PostgreSQL URL for the second leg of the floor pair. Falls back to "
            "a DATABASE_URL carrying a postgres scheme; without one the floor "
            "is measured on SQLite only and labelled SINGLE-ENGINE."
        ),
    )
    floor.set_defaults(handler=cmd_floor)

    mutation = sub.add_parser(
        "mutation",
        help="replay a manifest and print one bullet per mutation",
    )
    mutation.add_argument("--task", required=True, help="task id, e.g. SPEC-1-B07d")
    mutation.set_defaults(handler=cmd_mutation)

    check = sub.add_parser(
        "check",
        help="fail on a figure in docs/changes.md with no backing artifact",
    )
    check.add_argument("--base", required=True, help="base ref, e.g. origin/spec-comp")
    check.add_argument("--path", default=CHANGELOG, help="changelog path")
    check.set_defaults(handler=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except FigureError as error:
        print(f"changelog_figures: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

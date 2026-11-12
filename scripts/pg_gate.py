"""Gate a PostgreSQL suite run against the CERTIFIED FAILURE SET.

The baseline lives in ``pg_baseline.json`` and is never written by this
script: ``--emit-baseline`` prints a skeleton for a human to review. A new
failure is red; a fixed one is merely reported. Two guards keep it honest:
the runner's summary line is the authority on how many problems occurred,
and a test id is never guessed -- an ambiguous short name is an error.

Exit codes: 0 = no failure outside the baseline, 1 = a new failure, 2 = the
run could not be judged (unreadable log, unresolvable name, count mismatch).
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import pathlib
import re
import sys

# `FAILED (failures=6, errors=1, expected failures=4)` -- the runner's own
# count of problems, which the extracted headers are checked against.
SUMMARY_RE = re.compile(r"^(?P<verdict>FAILED|OK)\b\s*(?:\((?P<body>[^)]*)\))?\s*$")

# One whole comma-separated part of the summary body, so `expected
# failures=4` cannot be mistaken for `failures=4`.
COUNT_RE = re.compile(r"^\s*(?P<key>failures|errors)\s*=\s*(?P<value>\d+)\s*$")

# A failure header. unittest renders plain, subTest (a trailing `key=value`
# group) and docstring-bare shapes; the id must be a DOTTED path so a subTest
# parameter can never be certified as a test id.
HEADER_RE = re.compile(
    r"^(?P<kind>FAIL|ERROR):\s+(?P<short>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+\((?P<qualified>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\))?"
    r"(?:\s+\((?P<params>[^)]*)\))?\s*$"
)


def decode(raw: bytes) -> str:
    """Decode suite output by sniffing the byte-order mark.

    Captured through PowerShell's ``Tee-Object`` the log can be UTF-16LE, and
    read naively as UTF-8 no summary line or header is findable at all."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace")
    return raw.decode("utf-8", errors="replace")


def read_source(log: str) -> str:
    if log == "-":
        buffer = getattr(sys.stdin, "buffer", None)
        raw = buffer.read() if buffer is not None else sys.stdin.read().encode()
    else:
        raw = pathlib.Path(log).read_bytes()
    return decode(raw)


def parse_summary(text: str) -> tuple[str | None, int, int]:
    """Return (verdict, failures, errors) from the runner's summary line.

    Counts come from the ``FAILED (...)`` line, never from a count of headers:
    a green run reports nothing, and nothing is not zero. Each comma-separated
    part is matched whole, so ``expected failures=4`` cannot read as 4."""
    for line in reversed(text.splitlines()):
        match = SUMMARY_RE.match(line.strip())
        if not match:
            continue
        body = match.group("body") or ""
        numbers = {"failures": 0, "errors": 0}
        for part in body.split(","):
            field = COUNT_RE.match(part)
            if field:
                numbers[field.group("key")] = int(field.group("value"))
        return match.group("verdict"), numbers["failures"], numbers["errors"]
    return None, 0, 0


def collect_headers(text: str) -> list[tuple[str, str | None]]:
    """Return the (short_name, qualified_id_or_None) pairs from FAIL/ERROR lines.

    Only lines that start the line, case-sensitively: a Django log record such
    as ``ERROR 2026-10-04 ...`` in the same stream is not a failure."""
    headers: list[tuple[str, str | None]] = []
    for line in text.splitlines():
        match = HEADER_RE.match(line.rstrip())
        if match:
            headers.append((match.group("short"), match.group("qualified")))
    return headers


def _iter_ids(suite):
    for item in suite:
        if hasattr(item, "__iter__"):
            yield from _iter_ids(item)
        else:
            yield item.id()


def collected_short_names() -> dict[str, list[str]]:
    """Map each collected test's short method name to its qualified ids.

    Resolution comes from the suite Django builds, so it cannot invent an id;
    ``_FailedTest`` import failures are excluded. The working directory is
    changed and restored because DiscoverRunner discovers from ``'.'``."""
    backend = pathlib.Path(__file__).resolve().parent.parent / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    import django
    from django.conf import settings
    from django.test.utils import get_runner

    previous = os.getcwd()
    os.chdir(backend)
    try:
        django.setup()
        runner = get_runner(settings)(verbosity=0, interactive=False)
        collected = list(_iter_ids(runner.build_suite([])))
    finally:
        os.chdir(previous)
    mapping: dict[str, list[str]] = {}
    for test_id in collected:
        if "_FailedTest" in test_id:
            continue
        mapping.setdefault(test_id.rsplit(".", 1)[-1], []).append(test_id)
    return mapping


def extract_failing_ids(text: str) -> tuple[set[str], list[str], int]:
    """Return (resolved ids, problems, header count).

    Header count is NOT the id count: a ``subTest`` emits one header per
    sub-case, so the cross-check compares headers to problems, never ids."""
    headers = collect_headers(text)
    resolved: set[str] = set()
    problems: list[str] = []
    needs_lookup = [short for short, qualified in headers if not qualified]
    mapping: dict[str, list[str]] = {}
    if needs_lookup:
        try:
            mapping = collected_short_names()
        except Exception as exc:  # noqa: BLE001 - reported, never guessed past
            problems.append(
                f"cannot resolve unqualified failure names ({exc}); "
                "run the suite so its output carries qualified ids"
            )
            mapping = {}

    for short, qualified in headers:
        if qualified:
            resolved.add(qualified)
            continue
        candidates = mapping.get(short, [])
        if len(candidates) == 1:
            resolved.add(candidates[0])
        elif not candidates:
            problems.append(f"no collected test is named {short}")
        else:
            problems.append(
                f"{short} matches {len(candidates)} collected tests: "
                + ", ".join(sorted(candidates))
            )
    return resolved, problems, len(headers)


def detect_engine() -> str | None:
    """Return the live database backend's ENGINE, or None if undeterminable.

    Measured from ``connection.settings_dict``: settings.py falls back to
    SQLite silently when DATABASE_URL is unset, which is how a SQLite run could
    be compared against a PostgreSQL baseline. Diagnostic only, never fatal."""
    backend = pathlib.Path(__file__).resolve().parent.parent / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    previous = os.getcwd()
    try:
        os.chdir(backend)
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django
        from django.db import connection

        django.setup()
        return str(connection.settings_dict["ENGINE"])
    except Exception:  # noqa: BLE001 - diagnostic only, never fatal
        return None
    finally:
        os.chdir(previous)


def load_baseline(path: pathlib.Path) -> tuple[set[str], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = data.get("failing_tests")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError(f"{path}: 'failing_tests' must be a list of strings")
    return set(ids), data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare a PostgreSQL suite run against the certified set."
    )
    parser.add_argument(
        "--log",
        default="-",
        help="file holding the suite output, or '-' for stdin (default: -)",
    )
    parser.add_argument(
        "--baseline",
        default=str(pathlib.Path(__file__).resolve().parent / "pg_baseline.json"),
        help="path to the certified baseline JSON",
    )
    parser.add_argument(
        "--emit-baseline",
        action="store_true",
        help="print a reviewable baseline skeleton for the extracted set and exit",
    )
    args = parser.parse_args(argv)

    try:
        text = read_source(args.log)
    except OSError as exc:
        # An unreadable log is the documented exit 2, not a traceback with exit 1.
        print(
            f"pg_gate: cannot read the suite log {args.log!r}: {exc}", file=sys.stderr
        )
        return 2

    verdict, failures, errors = parse_summary(text)
    extracted, problems, header_count = extract_failing_ids(text)

    # Anti-vacuous guard, BEFORE --emit-baseline: the runner says N problems, so
    # N headers must be read. Fewer means an under-read set compares as "clean".
    reported = failures + errors
    if verdict is None and not args.emit_baseline:
        print(
            "pg_gate: no runner summary line (FAILED (...)/OK) in the output; "
            "the run cannot be judged.",
            file=sys.stderr,
        )
        return 2

    if problems:
        for problem in problems:
            print(f"pg_gate: {problem}", file=sys.stderr)
        return 2

    if verdict is None or header_count != reported:
        print(
            f"pg_gate: runner reported {reported} problem(s) but {header_count} "
            "failure header(s) were read; refusing to judge a run this gate did "
            "not read in full.",
            file=sys.stderr,
        )
        return 2

    if args.emit_baseline:
        print(
            json.dumps(
                {
                    "engine": "postgresql",
                    "certified_on": "<date of the run>",
                    "failing_tests": sorted(extracted),
                },
                indent=2,
            )
        )
        return 0

    baseline_path = pathlib.Path(args.baseline)
    try:
        baseline, data = load_baseline(baseline_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # A missing or corrupt baseline cannot be judged against: report exit 2.
        print(
            f"pg_gate: cannot read the baseline {baseline_path}: {exc}",
            file=sys.stderr,
        )
        return 2

    new = sorted(extracted - baseline)
    healed = sorted(baseline - extracted)

    # Two engine facts, kept apart: baseline_engine is a string copied from the
    # baseline (what it was CERTIFIED against, nothing more); connection_engine
    # is measured live, and catches a SQLite log pointed at a PG baseline.
    print(f"pg_gate: verdict={verdict} (from the log)")
    print(
        f"pg_gate: baseline_engine={data.get('engine', 'unknown')} "
        f"(declared in {baseline_path.name}) connection_engine="
        f"{detect_engine() or 'undetermined'}"
    )
    print(
        f"pg_gate: problems={reported} extracted_ids={len(extracted)} "
        f"baseline_ids={len(baseline)} new={len(new)} no_longer_failing={len(healed)}"
    )

    for test_id in healed:
        # Staying green is deliberate: a healed entry is the fix landing, and the
        # ratchet is deleting the entry in the same commit, not going red.
        print(f"pg_gate: WARNING baseline entry no longer fails: {test_id}")

    if new:
        for test_id in new:
            print(f"pg_gate: NEW FAILURE outside the certified set: {test_id}")
        return 1

    print("pg_gate: every failure is in the certified baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

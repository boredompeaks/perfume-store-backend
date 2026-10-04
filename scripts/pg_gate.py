"""Gate the PostgreSQL suite against the CERTIFIED FAILURE SET.

The backend suite is read on two engines. SQLite is green; PostgreSQL is not,
and it has not been for a documented stretch of the run: a set of known
failures lives on PG that does not exist on SQLite (index introspection sees
Postgres-only ``varchar_pattern_ops`` duplicates, and Postgres sequences are not
transactional so hardcoded primary keys do not survive rollback). Those
failures are certified in ``pg_baseline.json`` and are deliberately NOT fixed
here -- they are tracked as their own rows (PG-2b, PG-2e), and a fix lands by
editing the baseline in the same commit that fixes the test.

The defect this gate closes is that the certified set lived only in prose. A
ledger line saying "PG: failures=6, errors=1" is a claim about a SET, and a
count cannot tell two things apart: the same six failing forever (nothing
changed) and six failing plus one new one (a regression). This script compares
the SET, so a new failure is red and a fixed one is merely reported.

Two rules keep the gate honest, and both exist because a gate that reads
nothing looks exactly like a gate that passed:

  * The summary line is the authority on HOW MANY problems occurred. If it
    reports failures/errors and the headers yield no resolvable test id, this
    exits non-zero instead of reporting an empty (therefore "clean") set.
  * A test id is never guessed. unittest's own output is prose, and at
    ``--verbosity 2`` ``getDescription`` prefers a test's DOCSTRING over its
    id, so the qualified id is present in some renderings and absent in
    others. Where the output carries only the short method name, the name is
    resolved against the suite Django actually collected; a short name matching
    more than one test is an error, not a coin flip.

The baseline is never written by this script. A self-updating baseline records
whatever the run just did, which is the same defect this file exists to catch
(see ``doc_claims_baseline.json`` and the same argument in
``doc_claims.py``). ``--emit-baseline`` prints a skeleton for a human to review
and paste; the gate cannot launder a regression into a baseline.

Exit codes: 0 = no failure outside the baseline, 1 = a new failure, 2 = the run
could not be judged (unreadable log, unresolvable name, count mismatch).
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
# count of problems, which is what the extracted headers are checked against.
SUMMARY_RE = re.compile(r"^(?P<verdict>FAILED|OK)\b\s*(?:\((?P<body>[^)]*)\))?\s*$")

# One whole comma-separated part of the summary body, so that `expected
# failures=4` cannot be mistaken for `failures=4`.
COUNT_RE = re.compile(r"^\s*(?P<key>failures|errors)\s*=\s*(?P<value>\d+)\s*$")

# A failure header. unittest renders three shapes, all measured on Python 3.14
# against PostgreSQL 17.11:
#   FAIL: test_x (module.Class.test_x)                          plain failure
#   FAIL: test_x (module.Class.test_x) (column='razorpay_order_id')
#                                                              a subTest case
#   FAIL: test_x                                    a docstring-bearing test,
#                                                              id rendered bare
# The trailing subTest group is the second shape and it is NOT optional noise:
# a header regex anchored with `\s*$` after the id matches 5 of a 7-problem run,
# the anti-vacuous count cross-check then refuses the run, and the gate is red
# on every execution (BUG-1).
#
# The id is required to be a DOTTED path, which is what separates shape 2 from
# shape 3: a subTest group is rendered `key=value`, so it never matches a
# dotted identifier, and a bare `(alpha)`-style parameter cannot be promoted to
# a test id either. Without that restriction the regex would happily certify
# `alpha` as a failing test id.
HEADER_RE = re.compile(
    r"^(?P<kind>FAIL|ERROR):\s+(?P<short>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+\((?P<qualified>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\))?"
    r"(?:\s+\((?P<params>[^)]*)\))?\s*$"
)


def decode(raw: bytes) -> str:
    """Decode suite output by sniffing the byte-order mark.

    Measured, not hypothetical: capturing the suite through PowerShell's
    ``Tee-Object`` on Windows writes UTF-16LE (BOM ``FF FE``), and reading that
    as UTF-8 yields a file in which NO summary line and NO failure header is
    findable. Read naively, this gate would then compare an empty extracted set
    against the baseline and call it clean. The gate refuses to judge a run it
    could not read, but a gate that cannot read a log is useless, so the
    encoding is sniffed instead of assumed.
    """
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace")
    return raw.decode("utf-8", errors="replace")


def read_source(log: str) -> str:
    """Read the suite output from a file or stdin and return decoded text."""
    if log == "-":
        buffer = getattr(sys.stdin, "buffer", None)
        raw = buffer.read() if buffer is not None else sys.stdin.read().encode()
    else:
        raw = pathlib.Path(log).read_bytes()
    return decode(raw)


def parse_summary(text: str) -> tuple[str | None, int, int]:
    """Return (verdict, failures, errors) from the runner's summary line.

    Counts come from the ``FAILED (...)`` line and never from a count of
    headers: on a green run the summary reports nothing, and nothing is not
    zero.

    Each comma-separated part is matched whole. Matching the keys with a bare
    ``(failures|errors)\\s*=\\s*(\\d+)`` scans the body twice, because
    ``expected failures=4`` contains ``failures=4``: a dict built from that
    keeps the LAST match and silently reports 4 instead of 6, which is how this
    printed ``problems=5`` for a run that reported 7.
    """
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

    Only lines that start the line are read, case-sensitively: a Django log
    record such as ``ERROR 2026-10-04 ... django.request`` is interleaved into
    this same stream, and a case-insensitive scan sweeps those in as failures.
    """
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

    Resolution comes from the suite Django builds, so it cannot invent an id
    that no test has. Import failures are excluded: a ``_FailedTest`` id names a
    module that did not load, which is a different failure from the ones this
    gate certifies.

    The working directory is changed and restored because Django's
    ``DiscoverRunner.build_suite`` discovers from ``'.'``: run this script from
    the repository root -- where CI runs it -- and it would collect nothing and
    resolve no name at all. Measured, and silent: an empty collection turns
    every failure header into an unresolvable name.
    """
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

    The header count is returned separately because it is NOT the length of the
    id set: a ``subTest`` emits one header per sub-case, so one test can
    account for several problems. ``test_payment_provider_references_stay_
    constraint_covered`` fails for two columns and reports twice, which is why
    the certified set has six ids while the runner counts seven problems. The
    count cross-check below therefore compares headers to problems, never ids
    to problems.
    """
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

    Measured, not declared: this reads ``connection.settings_dict['ENGINE']``,
    so it reports the engine this process would actually use. That matters
    because settings.py falls back to SQLite without complaint when
    DATABASE_URL is unset or malformed -- a silent fallback is precisely how a
    SQLite run could be compared against a PostgreSQL baseline.

    Returns None rather than raising when Django or the settings cannot be
    loaded: the engine is a diagnostic, and a gate that cannot measure it must
    still be able to judge the failure set.
    """
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
        # An unreadable log is the documented exit 2, not a traceback with exit
        # 1: a mistyped path in CI must report "cannot be judged", the same as
        # any other unreadable input, rather than dumping a stack (OBS-1).
        print(
            f"pg_gate: cannot read the suite log {args.log!r}: {exc}", file=sys.stderr
        )
        return 2

    verdict, failures, errors = parse_summary(text)
    extracted, problems, header_count = extract_failing_ids(text)

    # The count cross-check is the anti-vacuous-pass guard: the runner says N
    # problems, so N headers must be present. Fewer means the extractor read
    # less than the run produced, and an under-read set compares as "clean".
    # It runs BEFORE --emit-baseline, because seeding a baseline from a run the
    # extractor did not fully read is how a vacuous gate gets certified into the
    # repo as if it were the measured thing.
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
        # Same class as the unreadable log above: a missing or corrupt baseline
        # cannot be judged against, so it reports exit 2 instead of tracing back.
        print(
            f"pg_gate: cannot read the baseline {baseline_path}: {exc}",
            file=sys.stderr,
        )
        return 2

    new = sorted(extracted - baseline)
    healed = sorted(baseline - extracted)

    # Two engine facts, kept apart because they answer different questions and
    # only one of them is measured here. `baseline_engine` is a string copied
    # out of the baseline JSON: it says what the baseline was CERTIFIED against,
    # and printing it as `engine=` read as though this run had been measured to
    # be on that engine (OBS-2) -- it is a claim in a file, nothing more.
    # `connection_engine` is measured, from the live Django connection, and is
    # the one that can catch the real mistake: pointing this gate at a SQLite
    # log while the baseline is a PostgreSQL baseline.
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
        # Staying green is deliberate: a baseline entry that stopped failing is
        # the fix landing, and the ratchet is to delete the entry in the same
        # commit. A gate that went red on it would punish the fix.
        print(f"pg_gate: WARNING baseline entry no longer fails: {test_id}")

    if new:
        for test_id in new:
            print(f"pg_gate: NEW FAILURE outside the certified set: {test_id}")
        return 1

    print("pg_gate: every failure is in the certified baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

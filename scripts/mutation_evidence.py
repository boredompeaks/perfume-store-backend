"""Replay recorded mutations and verify the evidence they claim.

Seven separate findings in the SPEC-1 run were unmeasured figures written as
if they had been measured. The inflated set from SPEC-1-B07d cycle 1 is the
 clearest: M2/M3/M4/M5/M6 were claimed at 4/4/8/10/12 and measured 1/3/7/2/6.
The conclusion ("6 of 6 killed") was true; every figure but one was invented.
The recurring shape is a tidy ascending run, which is what a narrative about
having run a battery looks like -- a real measurement is never tidy.

``doc_claims.py`` enumerates those numbers so a human sees them. This script
does the stronger thing: it makes the number *unrepeatably wrong* to publish,
by replaying the mutation and checking the claim.

A manifest names, per mutation, the exact bytes to find, the replacement, and
the tests that must fail. Replay refuses to proceed unless the `find` text
occurs EXACTLY ONCE in the target file -- which is precisely the trap that
produced the M3/M4 dispute, where two parties measured different edits and
each believed the other was wrong.

Safety, in order:

  1. refuses to run against a dirty worktree, so a failure can never leave
     uncommitted work half-mutated
  2. holds every original file's bytes in memory and restores from those
     bytes in a ``finally``, never via git (``git checkout`` is denied in
     this repo; the byte-equivalent restore is used instead)
  3. verifies the restore by sha256 and reports the mismatch rather than
     trusting it

Nothing here reaches the network, and nothing is Django-aware -- it shells
out to whatever runner it is told to use, defaulting to the project's own.

Usage::

    python scripts/mutation_evidence.py --list
    python scripts/mutation_evidence.py SPEC-1-B07d
    python scripts/mutation_evidence.py SPEC-1-B07d --verify-count
    python scripts/mutation_evidence.py SPEC-1-B07d --json > evidence.json

Exit codes: 0 every replay held, 1 at least one failed, 2 usage/environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO_ROOT / "scripts" / "mutation_evidence"
BACKEND_DIR = REPO_ROOT / "backend"

# "FAIL: test_x (orders.tests_returns.SomeTests.test_x)" and the ERROR twin.
RE_RESULT_LINE = re.compile(r"^(FAIL|ERROR):\s+(\S+)\s+\(([^)]+)\)")
# Failures are counted from this line, never from the run summary. The summary
# prints "FAILED (failures=2)" only on a red run and says nothing on green.
#
# `failures=` specifically, not `failures|errors`: unittest prints the two
# components in that order and prints only the non-zero ones, so an
# "FAILED (errors=2)" line is an ERROR count. Reading it as a failure count
# would let a mutant that merely breaks an import satisfy a claimed figure.
RE_FAILURES_LINE = re.compile(r"^FAILED \(failures=(\d+)")
RE_ANY_FAILED_LINE = re.compile(r"^FAILED \((?:failures|errors)=")

# Keys every mutation entry must carry, or the replay would die on a KeyError
# halfway through rather than naming the malformed manifest.
REQUIRED_KEYS = ("id", "path", "find")


class EvidenceError(RuntimeError):
    """A manifest is malformed, or the world is not in a fit state to replay."""


@dataclass
class MutationOutcome:
    mutation_id: str
    description: str
    path: str
    status: str = "pending"
    detail: str = ""
    expected_failures: list[str] = field(default_factory=list)
    observed_failures: list[str] = field(default_factory=list)
    claimed_count: int | None = None
    observed_count: int | None = None
    restored: bool = False


# --------------------------------------------------------------------------
# Pure core. No filesystem, no subprocess.
# --------------------------------------------------------------------------


def count_occurrences(haystack: bytes, needle: bytes) -> int:
    """Count non-overlapping occurrences of ``needle`` in ``haystack``."""
    if not needle:
        raise EvidenceError("mutation 'find' text is empty")
    return haystack.count(needle)


def match_bytes(original: bytes, find: bytes) -> bytes:
    """Re-express ``find`` in the target file's own line ending.

    A manifest is authored from the COMMITTED blob, which is LF-only, but
    `core.autocrlf` is on with no `.gitattributes`, so the worktree copy this
    script edits is CRLF. A byte-exact LF needle therefore occurs ZERO times in
    a CRLF file and every replay would refuse for a reason that has nothing to
    do with the mutation. Translating the needle -- and only the needle -- keeps
    "exactly one occurrence" honest while letting the write-back preserve the
    file's own ending, so the restore stays byte-exact.
    """
    if b"\r\n" not in original:
        return find
    if b"\r\n" in find:
        return find
    return find.replace(b"\n", b"\r\n")


def apply_mutation(original: bytes, find: bytes, replace: bytes) -> bytes:
    """Return the mutated bytes, or refuse.

    Exactly-one occurrence is the whole point. A mutation that matches twice
    is not the mutation that was measured, and replaying it would certify a
    claim about a different edit -- the SPEC-1-B07d M3/M4 defect.
    """
    occurrences = count_occurrences(original, match_bytes(original, find))
    if occurrences != 1:
        raise EvidenceError(
            f"'find' text occurs {occurrences} times; replay requires exactly "
            f"1 so the replayed edit is provably the recorded one"
        )
    return original.replace(
        match_bytes(original, find), match_bytes(original, replace), 1
    )


def parse_failure_lines(output: str) -> list[str]:
    """Extract failing test identifiers from unittest-style output.

    Returns dotted ids where available, so a manifest may name either a bare
    method name or the full dotted path.
    """
    found: list[str] = []
    for line in output.splitlines():
        match = RE_RESULT_LINE.match(line.strip())
        if not match:
            continue
        method, location = match.group(2), match.group(3)
        if method and method not in found:
            found.append(method)
        if location and location not in found:
            found.append(location)
        bare = location.rsplit(".", 1)[-1]
        if bare and bare not in found:
            found.append(bare)
    return found


def parse_failure_count(output: str) -> int | None:
    """Read the FAILURE count off the ``FAILED (failures=N`` line.

    Never read the run summary: on a green run it reports nothing, which is
    how "0 dirty everywhere" and similar vanishances get believed. A line that
    reports errors alone returns ``None`` rather than an error count, because
    an error is not a failure and must not satisfy a claimed figure.
    """
    for line in output.splitlines():
        match = RE_FAILURES_LINE.match(line.strip())
        if match:
            return int(match.group(1))
    return None


def ran_red(output: str) -> bool:
    """Whether the run finished red at all.

    Distinct from :func:`parse_failure_count`: "no count is readable" and "the
    run was green" are different facts, and collapsing them is how a mutant
    that breaks an import passes for a clean run.
    """
    return any(RE_ANY_FAILED_LINE.match(line.strip()) for line in output.splitlines())


def confirm_expected_failures(
    expected: list[str], observed: list[str]
) -> tuple[bool, list[str]]:
    """Did every expected test actually show up as a failure?"""
    if not expected:
        return False, ["manifest names no expected_failing_tests"]
    seen = set(observed)
    missing = [name for name in expected if name not in seen]
    return (not missing), missing


# --------------------------------------------------------------------------
# Shell.
# --------------------------------------------------------------------------


def _dirty_paths() -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def load_manifest(task: str) -> dict:
    path = MANIFEST_DIR / f"{task}.json"
    if not path.exists():
        available = sorted(p.stem for p in MANIFEST_DIR.glob("*.json"))
        raise EvidenceError(
            f"no manifest for {task!r}. Available: {available or ['none']}"
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutations = manifest.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        raise EvidenceError(f"{path.name}: 'mutations' must be a non-empty list")
    for position, mutation in enumerate(mutations, start=1):
        missing = [key for key in REQUIRED_KEYS if not mutation.get(key)]
        if missing:
            raise EvidenceError(
                f"{path.name}: mutation #{position} is missing "
                f"{', '.join(missing)}; a manifest that would KeyError "
                f"mid-replay is not replayable"
            )
    return manifest


def run_tests(labels: list[str], coverage: bool = True) -> str:
    """Run the project's suite for the given labels and return its output.

    Defaults to the project's own command so a replay measures the same thing
    the changelog claims about. ``manage.py test`` is invoked through
    coverage exactly as the floor is measured. An argv list, not a command
    string, so a label containing a space is one argument and not two.
    """
    prefix = ["-m", "coverage", "run"] if coverage else ["-m"]
    argv = [sys.executable, *prefix, "manage.py", "test", *labels]
    result = subprocess.run(
        argv,
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    return result.stdout + result.stderr


def replay_mutation(mutation: dict, coverage: bool = True) -> MutationOutcome:
    """Apply one mutation, run its tests, assert the failures, restore.

    The original bytes are captured before anything is written and restored in
    a ``finally``. If restoration fails the outcome says so loudly rather than
    leaving a mutated file behind quietly.
    """
    target = REPO_ROOT / mutation["path"]
    outcome = MutationOutcome(
        mutation_id=mutation["id"],
        description=mutation.get("description", ""),
        path=mutation["path"],
        claimed_count=mutation.get("claimed_count"),
    )

    if not target.exists():
        outcome.status = "error"
        outcome.detail = f"target file missing: {mutation['path']}"
        return outcome

    original = target.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    try:
        mutated = apply_mutation(
            original,
            mutation["find"].encode("utf-8"),
            mutation.get("replace", "").encode("utf-8"),
        )
    except EvidenceError as error:
        outcome.status = "error"
        outcome.detail = str(error)
        return outcome

    target.write_bytes(mutated)
    try:
        output = run_tests(list(mutation.get("test_scope", [])), coverage)
        observed = parse_failure_lines(output)
        outcome.observed_failures = observed
        outcome.observed_count = parse_failure_count(output)

        expected = list(mutation.get("expected_failing_tests", []))
        outcome.expected_failures = expected
        confirmed, missing = confirm_expected_failures(expected, observed)
        if not confirmed and not ran_red(output):
            # The mutant survived. This is the finding the whole exercise
            # exists to surface, so it is named rather than reported as a
            # replay that merely failed to find a name.
            outcome.status = "failed"
            outcome.detail = (
                "SURVIVED: the suite stayed green under this mutation, so "
                f"none of {missing} failed and the behaviour they claim to "
                "pin is unpinned"
            )
        elif not confirmed:
            outcome.status = "failed"
            outcome.detail = (
                f"expected failures not observed: {missing}. "
                f"Observed: {observed or 'none'}"
            )
        elif (
            outcome.claimed_count is not None
            and outcome.observed_count != outcome.claimed_count
        ):
            outcome.status = "failed"
            detail = (
                f"claimed_count {outcome.claimed_count} but the run reported "
                f"{outcome.observed_count} failures"
            )
            if outcome.observed_count is None:
                detail += (
                    " (no 'FAILED (failures=N)' line: the run went red with "
                    "errors only, which is not a failure count)"
                )
            outcome.detail = detail
        else:
            outcome.status = "held"
            outcome.detail = "every expected test failed as recorded"
    finally:
        target.write_bytes(original)
        restored_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        outcome.restored = restored_hash == original_hash
        if not outcome.restored:
            outcome.status = "error"
            outcome.detail += (
                f" RESTORE FAILED (sha256 {restored_hash} != {original_hash}) "
                f"-- {mutation['path']} may still be mutated"
            )
    return outcome


def run_task(
    task: str, coverage: bool = True, verify_count: bool = False
) -> list[MutationOutcome]:
    """Replay every mutation in a manifest, or every manifest with no task."""
    dirty = _dirty_paths()
    if dirty:
        raise EvidenceError(
            "refusing to replay against a dirty worktree; a crash would "
            "leave mutated files mixed with uncommitted work. Stash or "
            f"commit first. Dirty: {dirty[:5]}"
        )

    manifest = load_manifest(task)
    outcomes: list[MutationOutcome] = []
    for mutation in manifest["mutations"]:
        outcome = replay_mutation(mutation, coverage=coverage)
        if (
            verify_count
            and outcome.claimed_count is not None
            and outcome.status == "held"
        ):
            whole = run_tests([], coverage)
            outcome.observed_count = parse_failure_count(whole)
            if outcome.observed_count != outcome.claimed_count:
                outcome.status = "failed"
                outcome.detail = (
                    f"whole-suite count {outcome.observed_count} != claimed "
                    f"{outcome.claimed_count}"
                )
        outcomes.append(outcome)
    return outcomes


def available_manifests() -> list[str]:
    if not MANIFEST_DIR.is_dir():
        return []
    return sorted(p.stem for p in MANIFEST_DIR.glob("*.json"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", help="manifest name, e.g. SPEC-1-B07d")
    parser.add_argument("--list", action="store_true", help="list available manifests")
    parser.add_argument(
        "--verify-count",
        action="store_true",
        help="also run the whole suite per mutation to check "
        "claimed_count. Slow: this is what the inflated "
        "figures cost, so it is opt-in, not default.",
    )
    parser.add_argument(
        "--no-coverage",
        action="store_true",
        help="run tests without the coverage wrapper",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit outcomes as JSON on stdout"
    )
    args = parser.parse_args(argv)

    if args.list or not args.task:
        names = available_manifests()
        if args.json:
            print(json.dumps({"manifests": names}, indent=2))
        else:
            print("\n".join(names) if names else "no manifests recorded")
        return 0 if names or args.list else 2

    try:
        outcomes = run_task(
            args.task,
            coverage=not args.no_coverage,
            verify_count=args.verify_count,
        )
    except EvidenceError as error:
        print(f"mutation_evidence: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([asdict(o) for o in outcomes], indent=2, sort_keys=True))
    else:
        for outcome in outcomes:
            print(
                f"  {outcome.status.upper():7} {outcome.mutation_id:5} "
                f"claimed={outcome.claimed_count} "
                f"observed={outcome.observed_count}  {outcome.detail}"
            )
        held = sum(1 for o in outcomes if o.status == "held")
        uncommitted = [o.path for o in outcomes if not o.restored]
        print(f"mutation_evidence: {held}/{len(outcomes)} mutation(s) held")
        if uncommitted:
            print(f"mutation_evidence: NOT RESTORED: {uncommitted}")
    return 0 if outcomes and all(o.status == "held" for o in outcomes) else 1


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    raise SystemExit(main())

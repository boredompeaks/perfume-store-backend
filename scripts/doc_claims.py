"""Scan documentation diffs for mechanically checkable claims.

The changelog is the only part of this repository with no oracle. Code has
tests; prose has a reader. That asymmetry is the root condition behind every
drift finding in the SPEC-1 run -- an unmeasured figure, a quoted test name
that does not exist, a byte count that changed because a PowerShell write
mangled the file.

This script does not judge prose and never will. It refuses to let a
checkable claim pass unlisted. Three classes are decidable by a machine and
are hard failures:

  * a quoted ``test_*`` name with no matching ``def`` anywhere in the tree
  * a quoted module path that does not exist
  * a ``file.py:123`` reference whose file is missing or whose line is past
    the end of the file

Everything else is reported, never failed, because a machine cannot decide
whether "kills 7 tests" was true. Those become the auditor's checklist:

  * ``count``      -- a number attached to a countable noun
  * ``universal``  -- an absolute quantifier that needs a witness
  * ``self_count`` -- a count a file makes about its own contents, which is
                      false on commit by construction (BUG-7, SPEC-1-B07d c3)

A fourth check is orthogonal to the diff: byte integrity of tracked
documentation blobs. ``changes.md`` has been corrupted three times in this
run, each time by a shell write that ate bytes invisibly. The baseline is a
whole-file property, so this check is deliberately NOT diff-scoped.

Scope discipline: claim extraction is diff-scoped against a base ref, so
pre-existing debt is never re-reported and the output cannot die of noise.
Run it with a wide base after a long-lived branch to see everything at once.

Usage::

    python scripts/doc_claims.py --base origin/spec-comp
    python scripts/doc_claims.py --base origin/spec-comp --json > claims.json
    python scripts/doc_claims.py --update-baseline

Exit codes: 0 clean, 1 errors present, 2 usage or environment failure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "scripts" / "doc_claims_baseline.json"

DEFAULT_DOCS = ("backend/docs/",)
BYTE_INTEGRITY_DOCS = ("backend/docs/changes.md",)

# Directories never walked when building the repository index.
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "venv",
        "env",
        "__pycache__",
        "htmlcov",
        ".next",
        "test-results",
        "coverage",
        ".mypy_cache",
        ".pytest_cache",
    }
)

# A token boundary that will not match inside a longer identifier.
_BOUNDARY = r"(?<![A-Za-z0-9_])"

# One character after the prefix is enough: a cited name is a name whatever its
# length, and a length floor here would silently drop short names from the
# check rather than report them.
RE_TEST_NAME = re.compile(_BOUNDARY + r"(test_[A-Za-z0-9_]+)")
RE_MODULE_PATH = re.compile(_BOUNDARY + r"([A-Za-z0-9_][A-Za-z0-9_./-]*\.py)\b")
RE_PATH_LINE = re.compile(_BOUNDARY + r"([A-Za-z0-9_][A-Za-z0-9_./-]*\.py):(?:L)?(\d+)")
RE_DEF_TEST = re.compile(_BOUNDARY + r"def\s+(test_[A-Za-z0-9_]+)")

# A number welded to something countable. Deliberately verbose: a bare digit
# is not a claim, "7 tests" is.
RE_COUNT = re.compile(
    r"(?<![\w.])(\d+)\s+"
    r"(tests?|fixtures?|failures?|errors?|migrations?|statements?|stmts?|"
    r"occurrences?|places?|lines?|columns?|models?|endpoints?|rows?|"
    r"suites?|methods?|classes?|paths?|files?|fields?|capabilities)\b",
    re.IGNORECASE,
)
RE_MEASURED = re.compile(r"\bmeasured\s+(\d+)", re.IGNORECASE)
# The BUG-7 sentence in docs/changes.md spells the numeral ("occurs at exactly
# ONE place"), so a digits-only pattern misses the very sentence this rule was
# written for. Spellings are admitted inside this narrow frame only, where a
# stray "one" cannot become a count claim.
RE_NUMBER = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
RE_EXACTLY_PLACES = re.compile(
    r"\bexactly\s+" + RE_NUMBER + r"\s+(?:place|occurrence|instance)s?\b",
    re.IGNORECASE,
)

# Narrow on purpose. Bare "all" and "none" fire on almost every honest
# sentence ("all tests pass") and would bury the real findings.
RE_UNIVERSAL = re.compile(
    r"\b(always|never|only|sole|solely|exclusively|impossible|cannot|"
    r"can not|must not|no code path|every single|without exception)\b",
    re.IGNORECASE,
)
RE_SELF_REF = re.compile(
    r"\b(changes\.md|this file|this row|this section|this commit)\b",
    re.IGNORECASE,
)

# Decidable -> hard failure. Not decidable -> report only.
# ``byte_integrity`` is decided by this script, so a failure there is an error.
ERROR_KINDS = frozenset({"test_name", "module_path", "path_line", "byte_integrity"})


@dataclass(frozen=True)
class Claim:
    """One extracted claim. ``value`` is the matched text a human checks."""

    kind: str
    value: str
    path: str
    line: int
    detail: str = ""

    @property
    def severity(self) -> str:
        return "error" if self.kind in ERROR_KINDS else "warn"


@dataclass
class Report:
    """Scan result. Serialised to JSON verbatim."""

    base: str
    head: str
    files_scanned: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    errors: list[Claim] = field(default_factory=list)
    warnings: list[Claim] = field(default_factory=list)
    byte_integrity: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Pure core. No filesystem, no subprocess -- this is what the unit tests hit.
# --------------------------------------------------------------------------


def extract_claims(text: str, path: str, first_line: int = 1) -> list[Claim]:
    """Pull every claim out of one blob of added documentation text.

    ``first_line`` is the line number of ``text``'s first line in the file, so
    that findings point somewhere a human can navigate to.
    """
    claims: list[Claim] = []
    for offset, raw in enumerate(text.splitlines()):
        line_no = first_line + offset
        line = raw.strip()
        if not line:
            continue

        # A path cited as `file.py:123` is one claim, not two. Record which
        # names were consumed as path:line so the bare-module pass skips them.
        cited_with_line: set[str] = set()
        for match in RE_PATH_LINE.finditer(line):
            cited_with_line.add(match.group(1))
            claims.append(
                Claim(
                    kind="path_line",
                    value=match.group(0),
                    path=path,
                    line=line_no,
                    detail=f"path={match.group(1)} line={match.group(2)}",
                )
            )

        for match in RE_MODULE_PATH.finditer(line):
            if match.group(1) in cited_with_line:
                continue
            claims.append(Claim("module_path", match.group(1), path, line_no))

        for match in RE_TEST_NAME.finditer(line):
            claims.append(Claim("test_name", match.group(1), path, line_no))

        # A figure already claimed by a narrower pattern is not claimed twice:
        # "exactly 2 places" is one figure, not two, and a doubled figure makes
        # a reviewer count the wrong number of things to check.
        covered: list[tuple[int, int]] = []
        for regex in (RE_MEASURED, RE_EXACTLY_PLACES):
            for match in regex.finditer(line):
                covered.append(match.span())
                claims.append(
                    Claim(
                        kind="count",
                        value=match.group(0).strip(),
                        path=path,
                        line=line_no,
                        detail=f"n={match.group(1)}",
                    )
                )
        for match in RE_COUNT.finditer(line):
            if any(start <= match.start(1) < end for start, end in covered):
                continue
            claims.append(
                Claim(
                    kind="count",
                    value=match.group(0).strip(),
                    path=path,
                    line=line_no,
                    detail=f"n={match.group(1)}",
                )
            )

        universal = RE_UNIVERSAL.search(line)
        if universal:
            claims.append(Claim("universal", universal.group(0), path, line_no))

        # A count a file makes about itself cannot survive being written.
        counts_here = any(c.kind == "count" for c in claims if c.line == line_no)
        if counts_here and RE_SELF_REF.search(line):
            claims.append(
                Claim(
                    kind="self_count",
                    value=line[:120],
                    path=path,
                    line=line_no,
                    detail="a file counting its own occurrences is wrong "
                    "the moment it cites one (SPEC-1-B07d BUG-7)",
                )
            )
    return claims


def verify_claims(
    claims: Iterable[Claim],
    known_tests: set[str],
    known_paths: dict[str, int],
) -> tuple[list[Claim], list[Claim]]:
    """Split claims into hard errors and unverified warnings.

    ``known_paths`` maps a repo-relative posix path to its line count.
    """
    errors: list[Claim] = []
    warnings: list[Claim] = []
    for claim in claims:
        if claim.kind == "test_name":
            if claim.value not in known_tests:
                errors.append(
                    Claim(
                        claim.kind,
                        claim.value,
                        claim.path,
                        claim.line,
                        "no `def` for this name anywhere in the tree",
                    )
                )
            else:
                warnings.append(claim)
        elif claim.kind == "module_path":
            if not _path_exists(claim.value, known_paths):
                errors.append(
                    Claim(
                        claim.kind,
                        claim.value,
                        claim.path,
                        claim.line,
                        "no such file in the tree",
                    )
                )
            else:
                warnings.append(claim)
        elif claim.kind == "path_line":
            _, _, rest = claim.detail.partition("path=")
            cited, _, tail = rest.partition(" line=")
            resolved = _resolve_path(cited, known_paths)
            if resolved is None:
                errors.append(
                    Claim(
                        claim.kind,
                        claim.value,
                        claim.path,
                        claim.line,
                        f"no such file: {cited}",
                    )
                )
            elif int(tail) > known_paths[resolved]:
                errors.append(
                    Claim(
                        claim.kind,
                        claim.value,
                        claim.path,
                        claim.line,
                        f"line is past the end of {resolved} "
                        f"({known_paths[resolved]} lines)",
                    )
                )
            else:
                warnings.append(claim)
        else:
            warnings.append(claim)
    return errors, warnings


def _resolve_path(candidate: str, known_paths: dict[str, int]) -> str | None:
    """Resolve a cited module path against the index, or return ``None``.

    Prose cites both ``backend/orders/views.py`` and bare ``views.py``; a bare
    name resolves only when it is unambiguous, so a citation that could mean
    two different files is reported rather than guessed at.
    """
    if candidate in known_paths:
        return candidate
    tail = candidate.rsplit("/", 1)[-1]
    matches = [p for p in known_paths if p.rsplit("/", 1)[-1] == tail]
    return matches[0] if len(matches) == 1 else None


def _path_exists(candidate: str, known_paths: dict[str, int]) -> bool:
    """Whether a cited module path names exactly one file in the tree."""
    return _resolve_path(candidate, known_paths) is not None


def check_byte_integrity(blob: bytes, baseline: dict) -> dict:
    """Compare one committed blob against its recorded byte facts.

    Returns a dict with ``ok`` plus the measured values, so a failure says
    what moved rather than merely that something did.
    """
    lines = blob.split(b"\n")
    if blob.endswith(b"\n"):
        lines = lines[:-1]

    control: list[dict] = []
    for index, raw in enumerate(lines, start=1):
        # Every control byte is recorded, not the first on the line: the real
        # changes.md carries three on line 675, and stopping at the first would
        # baseline that line as one byte and hide the next corruption there.
        for column, byte in enumerate(raw, start=1):
            if byte < 0x20 and byte != 0x09:
                control.append(
                    {"line": index, "column": column, "byte": f"0x{byte:02x}"}
                )

    cr = blob.count(b"\r")
    crlf = blob.count(b"\r\n")
    measured = {
        "bytes": len(blob),
        "lines": len(lines),
        "control_bytes": len(control),
        "control_detail": control,
        "cr": cr,
        "crlf_pairs": crlf,
        "trailing_lf": blob.endswith(b"\n"),
    }

    problems = []
    if (
        "control_bytes" in baseline
        and measured["control_bytes"] != baseline["control_bytes"]
    ):
        problems.append(
            f"control bytes {measured['control_bytes']} != recorded "
            f"{baseline['control_bytes']}"
        )
    if "cr" in baseline and measured["cr"] != baseline["cr"]:
        problems.append(f"CR {measured['cr']} != recorded {baseline['cr']}")
    if "crlf_pairs" in baseline and measured["crlf_pairs"] != baseline["crlf_pairs"]:
        problems.append(
            f"CRLF pairs {measured['crlf_pairs']} != recorded "
            f"{baseline['crlf_pairs']}"
        )
    # The changelog grows on every commit, so equality would be wrong and
    # useless. A SHRINK is the signature of a write that ate content, which is
    # exactly the corruption this check exists for.
    if "lines" in baseline and measured["lines"] < baseline["lines"]:
        problems.append(
            f"{measured['lines']} lines is fewer than the recorded "
            f"{baseline['lines']}: content was lost"
        )
    if baseline.get("trailing_lf") and not measured["trailing_lf"]:
        problems.append("trailing newline is gone")
    measured["ok"] = not problems
    measured["problems"] = problems
    return measured


# --------------------------------------------------------------------------
# Shell. Git and filesystem live here and nowhere else.
# --------------------------------------------------------------------------


def _git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def added_lines(base: str, head: str, doc_prefixes: tuple[str, ...]) -> dict:
    """Map each changed doc file to its added line numbers.

    ``--unified=0`` is essential: with context, unchanged prose is reported as
    added and the output drowns.
    """
    raw = _git("diff", "--unified=0", f"{base}...{head}", "--", *doc_prefixes)
    files: dict[str, set[int]] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line.startswith("+++ "):
            # A deleted file's post-image is /dev/null; it has no added lines to
            # read and `git show head:/dev/null` would abort the whole scan.
            candidate = line[6:]
            if candidate == "/dev/null":
                current = None
                continue
            current = candidate
            files.setdefault(current, set())
        elif line.startswith("@@") and current:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if not match:
                continue
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            files[current].update(range(start, start + count))
    return files


def build_index() -> tuple[set[str], dict[str, int]]:
    """Collect every ``def test_*`` name and every tracked file's line count."""
    tests: set[str] = set()
    paths: dict[str, int] = {}

    for source_root in (
        "backend",
        "frontend/src",
        "frontend/tests",
        "tests",
        "scripts",
    ):
        root = REPO_ROOT / source_root
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            paths[rel] = len(text.splitlines())
            if path.suffix == ".py":
                tests.update(RE_DEF_TEST.findall(text))
            elif path.suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
                tests.update(RE_TEST_NAME.findall(text))
    return tests, paths


def load_baseline() -> dict:
    if BASELINE_PATH.exists():
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {}


def write_baseline() -> dict:
    """Record current byte facts for the tracked documentation blobs."""
    baseline: dict[str, dict] = {}
    for rel in BYTE_INTEGRITY_DOCS:
        blob = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
        if blob.returncode != 0:
            continue
        measured = check_byte_integrity(blob.stdout, {})
        baseline[rel] = {
            key: measured[key]
            for key in ("control_bytes", "cr", "crlf_pairs", "lines", "trailing_lf")
        }
    BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return baseline


def run(base: str, head: str) -> Report:
    known_tests, known_paths = build_index()
    files = added_lines(base, head, DEFAULT_DOCS)

    claims: list[Claim] = []
    for path, numbers in files.items():
        if not numbers:
            continue
        blob = _git("show", f"{head}:{path}")
        blob_lines = blob.splitlines()
        for number in sorted(numbers):
            if 1 <= number <= len(blob_lines):
                claims.extend(extract_claims(blob_lines[number - 1], path, number))

    errors, warnings = verify_claims(claims, known_tests, known_paths)

    baseline = load_baseline()
    integrity: list[dict] = []
    for rel, recorded in baseline.items():
        blob = subprocess.run(
            ["git", "show", f"{head}:{rel}"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
        if blob.returncode != 0:
            continue
        result = check_byte_integrity(blob.stdout, recorded)
        result["path"] = rel
        integrity.append(result)

    report = Report(
        base=base,
        head=head,
        files_scanned=sorted(files),
        claims=claims,
        errors=errors,
        warnings=warnings,
        byte_integrity=integrity,
    )
    if any(not entry["ok"] for entry in integrity):
        for entry in integrity:
            if entry["ok"]:
                continue
            report.errors.append(
                Claim(
                    "byte_integrity",
                    entry["path"],
                    entry["path"],
                    0,
                    "; ".join(entry["problems"]),
                )
            )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="origin/spec-comp",
        help="base ref for the diff (default: origin/spec-comp)",
    )
    parser.add_argument("--head", default="HEAD", help="head ref (default: HEAD)")
    parser.add_argument(
        "--json", action="store_true", help="emit the full report as JSON on stdout"
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record current byte facts and exit. Never runs "
        "automatically: a self-updating baseline records "
        "whatever the prose claimed, which is the defect "
        "this script exists to catch.",
    )
    args = parser.parse_args(argv)

    if args.update_baseline:
        recorded = write_baseline()
        print(json.dumps(recorded, indent=2, sort_keys=True))
        return 0

    try:
        report = run(args.base, args.head)
    except (RuntimeError, OSError) as error:
        print(f"doc_claims: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(report.to_json())
    else:
        print(
            f"doc_claims: {len(report.claims)} claims in "
            f"{len(report.files_scanned)} file(s) — "
            f"{len(report.errors)} error(s), {len(report.warnings)} to verify"
        )
        for claim in report.errors:
            print(
                f"  ERROR  {claim.path}:{claim.line} [{claim.kind}] "
                f"{claim.value} — {claim.detail}"
            )
        for entry in report.byte_integrity:
            state = "ok" if entry["ok"] else "CHANGED"
            print(
                f"  bytes  {entry['path']}: {state} "
                f"({entry['control_bytes']} control, CR {entry['cr']}, "
                f"LF {entry['trailing_lf']})"
            )
        if report.warnings:
            print(f"  {len(report.warnings)} claim(s) need a human or an auditor.")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

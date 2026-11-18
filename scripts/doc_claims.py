"""Scan documentation diffs for mechanically checkable claims.

Three classes are hard failures: a quoted ``test_*`` name with no matching
``def``, a quoted module path that does not exist, and a ``file.py:123``
reference whose file is missing or too short. Everything else (counts,
universal quantifiers, self-counts) is reported for a human.

Counter-examples are declared with a ``<!-- doc-claims:absent ... -->``
directive (at most one per line, naming a real witness); the restructure map
resolves historical cites whose target moved, and resolves rather than
suppresses. The diff base must move with the promotion: if the base is an
ancestor of HEAD the claim half scans nothing, which is reported loudly.

Exit codes: 0 clean, 1 errors present, 2 usage or environment failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "scripts" / "doc_claims_baseline.json"
RESTRUCTURE_MAP_PATH = REPO_ROOT / "scripts" / "doc_claims_restructure_map.json"

DEFAULT_DOCS = ("backend/docs/",)
BYTE_INTEGRITY_DOCS = ("backend/docs/changes.md",)

# Where the diff base comes from, first hit winning. See resolve_base().
BASE_ENV_VAR = "DOC_CLAIMS_BASE"
PR_BASE_ENV_VAR = "GITHUB_BASE_REF"
CI_ENV_VAR = "CI"
FALLBACK_BASE = "origin/spec-comp"

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

# One character after the prefix is enough: a cited name is a name whatever
# its length, and a floor here would silently drop short names.
RE_TEST_NAME = re.compile(_BOUNDARY + r"(test_[A-Za-z0-9_]+)")
# The tail after `.py` is `\w*(?:\.\w+)*` so a longer filename
# (`views.py.bak`, `views.pyx`) is matched whole and fails as missing; `\b`
# would drop the tail and launder it into the real `.py` beside it.
_PATH_TOKEN = r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.py\w*(?:\.\w+)*"
RE_MODULE_PATH = re.compile(_BOUNDARY + f"({_PATH_TOKEN})")
RE_PATH_LINE = re.compile(_BOUNDARY + f"({_PATH_TOKEN}):(?:L)?(\\d+)")
RE_DEF_TEST = re.compile(_BOUNDARY + r"def\s+(test_[A-Za-z0-9_]+)")

# A number welded to something countable: a bare digit is not a claim, "7
# tests" is.
RE_COUNT = re.compile(
    r"(?<![\w.])(\d+)\s+"
    r"(tests?|fixtures?|failures?|errors?|migrations?|statements?|stmts?|"
    r"occurrences?|places?|lines?|columns?|models?|endpoints?|rows?|"
    r"suites?|methods?|classes?|paths?|files?|fields?|capabilities)\b",
    re.IGNORECASE,
)
RE_MEASURED = re.compile(r"\bmeasured\s+(\d+)", re.IGNORECASE)
# The BUG-7 sentence spells the numeral ("exactly ONE place"), so spellings
# are admitted inside this narrow frame only, where "one" cannot become a
# count claim.
RE_NUMBER = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
RE_EXACTLY_PLACES = re.compile(
    r"\bexactly\s+" + RE_NUMBER + r"\s+(?:place|occurrence|instance)s?\b",
    re.IGNORECASE,
)

# Narrow on purpose: bare "all" and "none" would bury the real findings.
RE_UNIVERSAL = re.compile(
    r"\b(always|never|only|sole|solely|exclusively|impossible|cannot|"
    r"can not|must not|no code path|every single|without exception)\b",
    re.IGNORECASE,
)
RE_SELF_REF = re.compile(
    r"\b(changes\.md|this file|this row|this section|this commit)\b",
    re.IGNORECASE,
)

# Grammar, all on ONE source line, inside an HTML comment:
#
#   <!-- doc-claims:absent <test> (renamed-to <test> | misspelling-of <test>
#                              | retired) ; reason: <text> -->
#   <!-- doc-claims:absent <path> (corrupted-fragment-of <path>) ; reason: <text> -->
#
# At most ONE directive per line, naming ONE target and -- for every kind but
# `retired` -- a real witness: a `def` for a test, a tracked file whose path ends
# with the cited fragment for a path. The reason must clear MIN_REASON_CHARS and
# name a commit sha or task id; a malformed or over-quota directive honours
# nothing and fails closed.
MIN_REASON_CHARS = 24

# The checkable half of a reason: a token that LOOKS like a ledger reference.
# Both branches require a digit inside the token (without it any hyphenated
# English word or a-f run passed). This is a shape test, not proof of
# traceability; the review remains the control.
RE_REASON_REFERENCE = re.compile(
    r"(?:\b(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b"
    r"|\b(?=[A-Za-z0-9-]*[0-9])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+\b)"
)
RE_COUNTER_EXAMPLE = re.compile(
    r"<!--\s*doc-claims:absent\s+"
    r"(?P<target>test_[A-Za-z0-9_]+|[A-Za-z0-9_][A-Za-z0-9_./-]*\.py)\s+"
    r"\((?:renamed-to\s+(?P<renamed_to>test_[A-Za-z0-9_]+)"
    r"|misspelling-of\s+(?P<misspelling_of>test_[A-Za-z0-9_]+)"
    r"|corrupted-fragment-of\s+(?P<fragment_of>[A-Za-z0-9_][A-Za-z0-9_./-]*\.py)"
    r"|retired)\)"
    r"\s*;\s*reason:\s*(?P<reason>[^\n]+?)\s*-->"
)
# A path target gets ONLY the kind with a checkable witness.
COUNTER_EXAMPLE_KINDS = {
    "test": frozenset({"renamed-to", "misspelling-of", "retired"}),
    "path": frozenset({"corrupted-fragment-of"}),
}
OVER_QUOTA = (
    "more than one directive on this line; at most one is accepted, so this "
    "line is read as carrying none"
)
RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_directive_comments(line: str) -> str:
    """Remove the marker-carrying directives from the prose being scanned.

    A malformed directive is stripped too, which keeps failing closed: it
    suppresses nothing, it just stops annotating itself."""
    return RE_HTML_COMMENT.sub(
        lambda match: "" if "doc-claims:" in match.group(0) else match.group(0), line
    )


# Decidable claims are hard failures; ``byte_integrity`` is decided here too.
ERROR_KINDS = frozenset({"test_name", "module_path", "path_line", "byte_integrity"})


@dataclass(frozen=True)
class CounterExample:
    """One author's explicit statement that a quoted target is reported ABSENT.

    A ``retired`` directive has no witness; every other kind must name one
    that exists in the tree, or ``verify_claims`` refuses the directive."""

    target: str
    target_kind: str
    kind: str
    witness: str
    reason: str


def _directive_kind(match: re.Match) -> tuple[str, str, str]:
    if match.group("renamed_to"):
        return "renamed-to", match.group("renamed_to")
    if match.group("misspelling_of"):
        return "misspelling-of", match.group("misspelling_of")
    if match.group("fragment_of"):
        return "corrupted-fragment-of", match.group("fragment_of")
    return "retired", ""


def parse_counter_examples(
    line: str,
) -> tuple[dict[str, CounterExample], dict[str, str]]:
    """Read the counter-example directives out of ONE source line.

    Returns ``(accepted, refused)`` keyed by target; every refusal carries
    its reason so the error a directive failed to prevent says why."""
    accepted: dict[str, CounterExample] = {}
    refused: dict[str, str] = {}
    candidates: list[CounterExample] = []
    for match in RE_COUNTER_EXAMPLE.finditer(line):
        target = match.group("target")
        kind, witness = _directive_kind(match)
        reason = match.group("reason").strip()
        target_kind = "path" if target.endswith(".py") else "test"
        if len(reason) < MIN_REASON_CHARS:
            refused[target] = f"reason is under {MIN_REASON_CHARS} characters"
            continue
        if not RE_REASON_REFERENCE.search(reason):
            refused[target] = "reason names no commit sha or task id"
            continue
        if kind not in COUNTER_EXAMPLE_KINDS[target_kind]:
            refused[target] = (
                f"kind {kind!r} is not accepted on a {target_kind} target "
                f"(accepted: {', '.join(sorted(COUNTER_EXAMPLE_KINDS[target_kind]))})"
            )
            continue
        candidates.append(
            CounterExample(
                target=target,
                target_kind=target_kind,
                kind=kind,
                witness=witness,
                reason=reason,
            )
        )

    if len(candidates) > 1:
        # The cap: refuse ALL rather than keep the first, because the script
        # does not get to decide what the author meant.
        for candidate in candidates:
            refused[candidate.target] = OVER_QUOTA
        return {}, refused

    for candidate in candidates:
        accepted[candidate.target] = candidate
    return accepted, refused


@dataclass(frozen=True)
class Claim:
    """One extracted claim; ``value`` is the matched text a human checks.

    ``detail`` is the machine half, ``note`` the human half, kept apart so an
    annotation is never parsed back as part of a citation."""

    kind: str
    value: str
    path: str
    line: int
    detail: str = ""
    note: str = ""
    directive: CounterExample | None = None
    refusal: str = ""

    @property
    def severity(self) -> str:
        """Whether this claim failed, or is a warning for a human to read.

        A claim keeps its directive only when it was accepted; a refused one
        is dropped onto a fresh Claim and reads as an error again."""
        if self.directive is not None:
            return "warn"
        return "error" if self.kind in ERROR_KINDS else "warn"


@dataclass
class Report:

    base: str
    head: str
    base_source: str = ""
    files_scanned: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    errors: list[Claim] = field(default_factory=list)
    warnings: list[Claim] = field(default_factory=list)
    byte_integrity: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


# Pure core: no filesystem, no subprocess -- this is what the unit tests hit.


def _directive_note(directive: CounterExample | None) -> str:
    """The one-line human note for an accepted directive, or ``""``."""
    if directive is None:
        return ""
    return f"cited as a counter-example ({directive.kind}): {directive.reason}"


def _is_dotted_module_segment(text: str, span: tuple[int, int]) -> bool:
    """True when ``span`` is a NON-FINAL segment of a dotted identifier.

    A module cited dotted carries the ``test_`` stem as a segment with no
    ``def``; the final segment is the method and is deliberately not exempt.
    Measured on the identifier text either side, never on the whole line."""
    start, end = span
    before = start - 1
    while before >= 0 and (text[before].isalnum() or text[before] == "_"):
        before -= 1
    if before >= 0 and text[before] == ".":
        pass
    else:
        return False
    after = end
    while after < len(text) and (text[after].isalnum() or text[after] == "_"):
        after += 1
    # A non-final segment is followed by a dot and at least one word character.
    return (
        after < len(text)
        and text[after] == "."
        and after + 1 < len(text)
        and (text[after + 1].isalnum() or text[after + 1] == "_")
    )


def _inside_any_span(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    """Whether ``span`` lies wholly inside any one of ``spans``.

    Position on the line decides "was this token part of a cited path", not
    string equality; containment rather than overlap keeps a token that
    reaches across a path its own claim."""
    start, end = span
    return any(low <= start and end <= high for low, high in spans)


def extract_claims(text: str, path: str, first_line: int = 1) -> list[Claim]:
    """Pull every claim out of one blob of added documentation text.

    ``first_line`` is where ``text`` starts in the file, so findings point
    somewhere a human can navigate."""
    claims: list[Claim] = []
    for offset, raw in enumerate(text.splitlines()):
        line_no = first_line + offset
        line = raw.strip()
        if not line:
            continue
        accepted, refused = parse_counter_examples(line)
        prose = _strip_directive_comments(line)

        # A `file.py:123` cite is one claim; record it so the module pass skips it.
        cited_with_line: set[str] = set()
        # The SPAN of every path token, so the test-name pass cannot read a
        # `test_` that is part of a filename. `path:line` cites share the span.
        path_spans: list[tuple[int, int]] = []
        for match in RE_PATH_LINE.finditer(prose):
            cited = match.group(1)
            cited_with_line.add(cited)
            claims.append(
                Claim(
                    kind="path_line",
                    value=match.group(0),
                    path=path,
                    line=line_no,
                    detail=f"path={match.group(1)} line={match.group(2)}",
                    note=_directive_note(accepted.get(cited)),
                    directive=accepted.get(cited),
                    refusal=refused.get(cited, ""),
                )
            )

        for match in RE_MODULE_PATH.finditer(prose):
            path_spans.append(match.span(1))
            if match.group(1) in cited_with_line:
                continue
            cited = match.group(1)
            claims.append(
                Claim(
                    "module_path",
                    cited,
                    path,
                    line_no,
                    note=_directive_note(accepted.get(cited)),
                    directive=accepted.get(cited),
                    refusal=refused.get(cited, ""),
                )
            )

        for match in RE_TEST_NAME.finditer(prose):
            # A `test_` INSIDE a cited path is the module's stem, not a method;
            # the path claim is still verified in exchange.
            if _inside_any_span(match.span(), path_spans):
                continue
            # Dotted ids carry the module stem as a non-final segment, which
            # has no `def`; only a non-final segment is exempt, because the
            # final segment is the method the gate exists to check.
            if _is_dotted_module_segment(prose, match.span()):
                continue
            name = match.group(1)
            # Only an explicit directive on THIS line separates the two readings.
            directive = accepted.get(name)
            claims.append(
                Claim(
                    "test_name",
                    name,
                    path,
                    line_no,
                    note=_directive_note(directive),
                    directive=directive,
                    refusal=refused.get(name, ""),
                )
            )

        # A figure claimed by a narrower pattern is not claimed twice.
        covered: list[tuple[int, int]] = []
        for regex in (RE_MEASURED, RE_EXACTLY_PLACES):
            for match in regex.finditer(prose):
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
        for match in RE_COUNT.finditer(prose):
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

        universal = RE_UNIVERSAL.search(prose)
        if universal:
            claims.append(Claim("universal", universal.group(0), path, line_no))

        # A count a file makes about itself cannot survive being written.
        counts_here = any(c.kind == "count" for c in claims if c.line == line_no)
        if counts_here and RE_SELF_REF.search(prose):
            claims.append(
                Claim(
                    kind="self_count",
                    value=prose[:120],
                    path=path,
                    line=line_no,
                    detail="a file counting its own occurrences is wrong "
                    "the moment it cites one (SPEC-1-B07d BUG-7)",
                )
            )
    return claims


def _map_entry_for(
    token: str, claim_path: str, path_map: dict[str, dict]
) -> dict | None:
    """The map entry allowed to resolve THIS cite, or ``None``.

    Exact token match AND the citing file must appear in ``cites_in``: an
    entry written for one file's historical rows must not green the same
    token in another doc. Fail-closed: no recorded scope resolves nothing."""
    entry = path_map.get(token)
    if entry is None:
        return None
    if claim_path in entry.get("cites_in", ()):
        return entry
    return None


def verify_claims(
    claims: Iterable[Claim],
    known_tests: set[str],
    known_paths: dict[str, int],
    restructure: dict | None = None,
) -> tuple[list[Claim], list[Claim]]:
    """Split claims into hard errors and unverified warnings.

    ``known_paths`` maps a repo-relative posix path to its line count. A map
    entry scoped to the citing file decides: it preempts the direct lookup,
    any directive and the numeric line check, and downgrades a path_line's
    line half to a warning saying the line was not verified."""
    errors: list[Claim] = []
    warnings: list[Claim] = []
    restructure = restructure or {}
    path_map: dict[str, dict] = restructure.get("paths", {})
    test_map: dict[str, dict] = restructure.get("tests", {})
    tracked = restructure.get("tracked")
    for claim in claims:
        # Computed once so the error text cannot describe a different failure.
        failure = (
            _directive_failure(claim.directive, known_tests, known_paths)
            if claim.directive is not None
            else ""
        )
        if claim.kind == "test_name":
            if claim.value in known_tests:
                warnings.append(claim)
            elif claim.value in test_map:
                # The map requires the SUCCESSOR to exist: deleting the
                # replacement fails the old cite again.
                entry = test_map[claim.value]
                successor = entry["successor"]
                if successor in known_tests:
                    warnings.append(
                        Claim(
                            claim.kind,
                            claim.value,
                            claim.path,
                            claim.line,
                            note=(
                                f"restructure map: renamed, the shipped pin is "
                                f"{successor} -- {entry['reason']}"
                            ),
                        )
                    )
                else:
                    errors.append(
                        Claim(
                            claim.kind,
                            claim.value,
                            claim.path,
                            claim.line,
                            detail=_absent_detail(
                                claim,
                                f"restructure-map successor {successor} does "
                                "not exist either",
                                failure,
                            ),
                        )
                    )
            elif claim.directive is not None and not failure:
                # The author stated this name is reported ABSENT, and the
                # witness check passed.
                warnings.append(claim)
            else:
                errors.append(
                    Claim(
                        claim.kind,
                        claim.value,
                        claim.path,
                        claim.line,
                        _absent_detail(
                            claim,
                            "no `def` for this name anywhere in the tree",
                            failure,
                        ),
                    )
                )
        elif claim.kind == "module_path":
            entry = _map_entry_for(claim.value, claim.path, path_map)
            if entry is not None:
                ok, note = restructure_check(entry, known_paths, tracked)
                if ok:
                    warnings.append(
                        Claim(
                            claim.kind,
                            claim.value,
                            claim.path,
                            claim.line,
                            note=note,
                        )
                    )
                else:
                    errors.append(
                        Claim(
                            claim.kind,
                            claim.value,
                            claim.path,
                            claim.line,
                            detail=note,
                        )
                    )
            elif _path_exists(claim.value, known_paths):
                warnings.append(claim)
            elif claim.directive is not None and not failure:
                warnings.append(claim)
            else:
                errors.append(
                    Claim(
                        claim.kind,
                        claim.value,
                        claim.path,
                        claim.line,
                        _absent_detail(claim, "no such file in the tree", failure),
                    )
                )
        elif claim.kind == "path_line":
            _, _, rest = claim.detail.partition("path=")
            cited, _, tail = rest.partition(" line=")
            entry = _map_entry_for(cited, claim.path, path_map)
            if entry is not None:
                # The map preempts the line check: a moved location cannot vouch
                # for remembered line numbers, so the warning says so.
                ok, note = restructure_check(entry, known_paths, tracked)
                if ok:
                    warnings.append(
                        Claim(
                            claim.kind,
                            claim.value,
                            claim.path,
                            claim.line,
                            note=(
                                f"{note}; cited line {tail} NOT verified "
                                "under a map resolution"
                            ),
                        )
                    )
                else:
                    errors.append(
                        Claim(
                            claim.kind,
                            claim.value,
                            claim.path,
                            claim.line,
                            detail=note,
                        )
                    )
                continue
            resolved = _resolve_path(cited, known_paths)
            if resolved is None:
                if claim.directive is not None and not failure:
                    warnings.append(claim)
                else:
                    errors.append(
                        Claim(
                            claim.kind,
                            claim.value,
                            claim.path,
                            claim.line,
                            _absent_detail(claim, f"no such file: {cited}", failure),
                        )
                    )
            elif int(tail) > known_paths[resolved]:
                # No directive reaches this branch: the file resolved, so the
                # cite is not a counter-example and a wrong line is just wrong.
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


def _directive_failure(
    directive: CounterExample, known_tests: set[str], known_paths: dict[str, int]
) -> str:
    """Why this directive is NOT acceptable, or ``""`` when it is.

    A named witness must be real and be the intact form of THIS target: a
    ``def`` for a test, or a tracked path that ENDS WITH the cited fragment
    (a byte-eaten write truncates the FRONT of the name, so a slash-prefixed
    match would reject the very corruption this kind exists for). ``retired``
    names no witness and is the weakest kind; the review is the control."""
    if directive.kind == "retired":
        return ""
    if directive.target_kind == "test":
        if directive.witness in known_tests:
            return ""
        return (
            f"the directive names {directive.witness} as the surviving name, "
            f"which does not exist either"
        )
    resolved = _resolve_path(directive.witness, known_paths)
    if resolved is None:
        return (
            f"the directive names {directive.witness} as the intact file, "
            f"which is not in the tree either"
        )
    if not resolved.endswith(directive.target):
        return (
            f"the directive names {directive.witness}, which resolves to "
            f"{resolved} and does not end with {directive.target}"
        )
    return ""


def _absent_detail(claim: Claim, base: str, failure: str = "") -> str:
    if failure:
        return f"{base}, and {failure}"
    if claim.refusal:
        return (
            f"{base} (a counter-example directive was present and refused: "
            f"{claim.refusal})"
        )
    return base


def _resolve_path(candidate: str, known_paths: dict[str, int]) -> str | None:
    """Resolve a cited module path against the index, or return ``None``.

    Three passes: exact path, suffix match on a PATH BOUNDARY, then bare
    basename. The ``/`` is load-bearing -- ``orders/views.py`` is a path,
    ``ders/views.py`` is not -- and a basename alone is ambiguous across apps."""
    normalised = candidate[2:] if candidate.startswith("./") else candidate
    if normalised in known_paths:
        return normalised
    for predicate in (_ends_with_path, _same_basename):
        matches = [p for p in known_paths if predicate(p, normalised)]
        if len(matches) == 1:
            return matches[0]
    return None


def _ends_with_path(tracked: str, cited: str) -> bool:
    return tracked.endswith("/" + cited)


def _same_basename(tracked: str, cited: str) -> bool:
    return tracked.rsplit("/", 1)[-1] == cited.rsplit("/", 1)[-1]


def _path_exists(candidate: str, known_paths: dict[str, int]) -> bool:
    return _resolve_path(candidate, known_paths) is not None


# The restructure map: historical cites whose target moved, pinned in data.
# It resolves, it never suppresses: every pass prints a RESTRUCTURED line with
# the entry's reason, an entry whose target is gone is an error, and cites_in
# scopes an entry to the doc files it was written for.

# Exactly one of these three keys per path entry; unknown keys are rejected
# so a typo cannot park an entry in silent no-op limbo. ``cites_in`` is
# required on every shape.
_MAP_ENTRY_KEYS = {
    "targets": frozenset({"reason", "targets", "mode", "cites_in"}),
    "untracked": frozenset({"reason", "untracked", "cites_in"}),
    "external": frozenset({"reason", "external", "cites_in"}),
}
_MAP_MODES = frozenset({"all", "any"})


def _map_error(where: str, problem: str) -> RuntimeError:
    location = f" {where}" if where else ""
    return RuntimeError(f"restructure map{location}: {problem}")


def _validate_reason(where: str, entry: dict) -> str:
    """The entry's reason, or raise: an unexplained pass is a defect.
    Same floor as the directives; the reason is what the log line prints."""
    reason = entry.get("reason")
    if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON_CHARS:
        raise _map_error(
            where, f"reason must be a sentence of at least {MIN_REASON_CHARS} chars"
        )
    return reason


def _validate_path_list(where: str, key: str, value: object) -> None:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise _map_error(where, f"{key} must be a non-empty list of paths")


def _validate_path_entry(where: str, entry: object) -> None:
    if not isinstance(entry, dict):
        raise _map_error(where, "entry must be an object")
    kinds = [key for key in _MAP_ENTRY_KEYS if key in entry]
    if len(kinds) != 1:
        raise _map_error(
            where, "needs exactly one of targets, untracked or external"
        )
    allowed = _MAP_ENTRY_KEYS[kinds[0]]
    unknown = set(entry) - allowed
    if unknown:
        raise _map_error(where, f"unknown keys: {sorted(unknown)}")
    _validate_reason(where, entry)
    if kinds[0] == "targets":
        _validate_path_list(where, "targets", entry["targets"])
        mode = entry.get("mode", "all")
        if mode not in _MAP_MODES:
            raise _map_error(
                where, f"mode must be one of {sorted(_MAP_MODES)}, got {mode!r}"
            )
    elif kinds[0] == "untracked":
        _validate_path_list(where, "untracked", entry["untracked"])
    elif entry["external"] is not True:
        raise _map_error(where, "external must be true")
    # A cites_in outside the scanned doc prefixes can never hold a claim, so
    # it is a load error rather than a silent no-op.
    _validate_path_list(where, "cites_in", entry.get("cites_in"))
    for cited_doc in entry["cites_in"]:
        if not cited_doc.startswith(DEFAULT_DOCS):
            raise _map_error(
                where,
                f"cites_in {cited_doc!r} is outside the scanned doc prefixes "
                f"{tuple(DEFAULT_DOCS)} and can never hold a claim",
            )


def _validate_test_entry(where: str, entry: object) -> None:
    if not isinstance(entry, dict):
        raise _map_error(where, "entry must be an object")
    unknown = set(entry) - {"reason", "successor"}
    if unknown:
        raise _map_error(where, f"unknown keys: {sorted(unknown)}")
    _validate_reason(where, entry)
    successor = entry.get("successor")
    if not isinstance(successor, str) or not successor:
        raise _map_error(where, "successor must be a non-empty test name")


def validate_restructure_map(data: object) -> dict:
    """Shape-check a parsed map, or raise. Existence is NOT checked here.

    A rotted target fails the claim that named it, per claim; a structurally
    broken map aborts, because a scanner without its map lies by omission."""
    if not isinstance(data, dict):
        raise _map_error("", "top level must be an object")
    unknown = set(data) - {"_readme", "paths", "tests"}
    if unknown:
        raise _map_error("", f"unknown top-level keys: {sorted(unknown)}")
    paths = data.get("paths")
    tests = data.get("tests")
    if not isinstance(paths, dict) or not isinstance(tests, dict):
        raise _map_error("", "'paths' and 'tests' objects are both required")
    for cited, entry in paths.items():
        if not isinstance(cited, str) or not cited:
            raise _map_error("paths", f"cite keys must be non-empty strings: {cited!r}")
        _validate_path_entry(f"paths[{cited!r}]", entry)
    for name, entry in tests.items():
        if not isinstance(name, str) or not name.startswith("test_"):
            raise _map_error("tests", f"keys must be test names: {name!r}")
        _validate_test_entry(f"tests[{name!r}]", entry)
    return {"paths": dict(paths), "tests": dict(tests)}


def load_restructure_map(path: Path = RESTRUCTURE_MAP_PATH) -> dict:
    """Read and shape-validate the map file; missing or unreadable raises."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise _map_error(str(path), f"cannot read: {error}") from error
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise _map_error(str(path), f"invalid JSON: {error}") from error
    return validate_restructure_map(data)


def _target_present(target: str, known_paths: dict[str, int]) -> bool:
    """Whether one map target exists in the index.

    A target ending in ``/`` is a package: present while any indexed file
    lives under it, so the cite fails the day the package does."""
    if target.endswith("/"):
        return any(path.startswith(target) for path in known_paths)
    return target in known_paths


def restructure_check(
    entry: dict,
    known_paths: dict[str, int],
    tracked: frozenset[str] | None,
) -> tuple[bool, str]:
    """``(holds, note)`` for one map entry against THIS tree.

    The note always names the entry's reason and is used unchanged as the
    error detail. An untracked entry without ``tracked`` answers False, never
    passing on absent evidence."""
    reason = entry["reason"]
    if "external" in entry:
        return True, (
            "restructure map: external path, outside this repository and not "
            f"machine-checkable from the tree -- {reason}"
        )
    if "untracked" in entry:
        if tracked is None:
            return False, (
                "restructure map: cannot verify this must-stay-untracked entry "
                f"without the tracked file list -- {reason}"
            )
        ghosts = [item for item in entry["untracked"] if item in tracked]
        if ghosts:
            return False, (
                "restructure map: must stay out of the tree but is tracked: "
                f"{', '.join(ghosts)} -- {reason}"
            )
        return True, f"restructure map: untracked as recorded -- {reason}"
    targets = entry["targets"]
    missing = [item for item in targets if not _target_present(item, known_paths)]
    if entry.get("mode", "all") == "any":
        holds = len(missing) < len(targets)
    else:
        holds = not missing
    if holds:
        return True, f"restructure map: resolves to {', '.join(targets)} -- {reason}"
    return False, (
        f"restructure map target(s) missing: {', '.join(missing)} -- {reason}"
    )


def check_byte_integrity(blob: bytes, baseline: dict) -> dict:
    """Compare one committed blob against its recorded byte facts.

    Returns ``ok`` plus the measured values, so a failure says what moved."""
    lines = blob.split(b"\n")
    if blob.endswith(b"\n"):
        lines = lines[:-1]

    control: list[dict] = []
    for index, raw in enumerate(lines, start=1):
        # Every control byte is recorded, not the first on the line: stopping at
        # the first would hide the next corruption on the same line.
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
    # The changelog grows on every commit, so only a SHRINK is a failure: it
    # is the signature of a write that ate content.
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


# Shell: git and the filesystem live here and nowhere else.


def resolve_base(
    explicit: str | None = None, env: dict[str, str] | None = None
) -> tuple[str, str]:
    """Pick the diff base, and say where the choice came from.

    First hit wins: ``--base``, ``$DOC_CLAIMS_BASE``, ``origin/$GITHUB_BASE_REF``
    when ``$CI`` is set, then ``origin/spec-comp``. The base must MOVE WITH
    THE PROMOTION: once a train is promoted the default is an ancestor of
    HEAD, the three-dot diff is empty, and the claim half scans nothing."""
    environ = os.environ if env is None else env
    if explicit:
        return explicit, "--base"
    from_env = environ.get(BASE_ENV_VAR, "").strip()
    if from_env:
        return from_env, f"${BASE_ENV_VAR}"
    pr_base = environ.get(PR_BASE_ENV_VAR, "").strip()
    if environ.get(CI_ENV_VAR, "").strip() and pr_base:
        return f"origin/{pr_base}", f"origin/${PR_BASE_ENV_VAR} (CI)"
    return FALLBACK_BASE, "default"


def empty_scan_notice(files_scanned: Iterable[str], base: str, head: str) -> str:
    """The message for a claim half that found nothing to look at, else ``""``.
    A gate that scanned zero lines must not look like a gate that passed."""
    if list(files_scanned):
        return ""
    return (
        f"claim half scanned 0 file(s) against base {base!r} (head {head!r}). "
        f"The base is an ancestor of the head, so there is no ADDED "
        f"documentation to check and every claim in this run passed by not "
        f"being looked at. Byte integrity still ran. To widen the base, set "
        f"${BASE_ENV_VAR} to the last promoted sha or the PR base branch "
        f"before this step."
    )


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

    ``--unified=0`` is essential: with context, unchanged prose counts as
    added and the output drowns."""
    raw = _git("diff", "--unified=0", f"{base}...{head}", "--", *doc_prefixes)
    files: dict[str, set[int]] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line.startswith("+++ "):
            # A deleted file's post-image is /dev/null; it has no added lines.
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


def run(base: str, head: str, base_source: str = "") -> Report:
    known_tests, known_paths = build_index()
    restructure = load_restructure_map()
    # The HEAD tree, for the map's must-stay-untracked entries: a source-root
    # walk cannot see a root-level scratch file, but `ls-tree` sees the tree.
    restructure["tracked"] = frozenset(
        _git("ls-tree", "-r", "--name-only", head).splitlines()
    )
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

    errors, warnings = verify_claims(
        claims, known_tests, known_paths, restructure
    )

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
        base_source=base_source,
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
        default=None,
        help="base ref for the diff. Unset, the base is resolved in this "
        f"order: --base, ${BASE_ENV_VAR}, origin/${PR_BASE_ENV_VAR} when "
        f"${CI_ENV_VAR} is set, then {FALLBACK_BASE}. The base must move "
        "WITH THE PROMOTION: once a train is promoted the default is an "
        "ancestor of HEAD, the three-dot diff is empty, and the claim half "
        f"scans nothing. CI should set ${BASE_ENV_VAR} to the PR base sha "
        f"(${{{{ github.event.pull_request.base.sha }}}}) or to the branch "
        f"name ({FALLBACK_BASE} is an ancestor of HEAD in a push context).",
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

    base, base_source = resolve_base(args.base)
    try:
        report = run(base, args.head, base_source)
    except (RuntimeError, OSError) as error:
        print(f"doc_claims: {error}", file=sys.stderr)
        return 2

    notice = empty_scan_notice(report.files_scanned, base, args.head)
    if notice:
        print(notice, file=sys.stderr)

    if args.json:
        print(report.to_json())
    else:
        print(
            f"doc_claims: {len(report.claims)} claims in "
            f"{len(report.files_scanned)} file(s) — "
            f"{len(report.errors)} error(s), {len(report.warnings)} to verify"
        )
        print(f"  base   {report.base} (from {report.base_source})")
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
        # Every honoured counter-example and map resolution is printed: a pass
        # that leaves no trace in the log is not reviewable.
        for claim in report.warnings:
            if claim.directive is not None:
                print(
                    f"  ABSENT {claim.path}:{claim.line} [{claim.kind}] "
                    f"{claim.value} — {claim.note}"
                )
            elif claim.note.startswith("restructure map:"):
                print(
                    f"  RESTRUCTURED {claim.path}:{claim.line} [{claim.kind}] "
                    f"{claim.value} — {claim.note}"
                )
        if report.warnings:
            print(f"  {len(report.warnings)} claim(s) need a human or an auditor.")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

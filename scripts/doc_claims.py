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

One of those three cannot tell a CLAIM from a COUNTER-EXAMPLE. Prose that
quotes a retired test name, or a misspelling, or a path that a byte-eaten
write left as a fragment, in order to REPORT that it does not exist, is the
same sentence shape as prose that asserts it does -- and this repository is
full of the former, in its own ledger. A gate that is red on correct prose
gets deleted or made ``continue-on-error``, which is worse than having no
gate, so the author states the counter-example explicitly and this script
reads the statement rather than guessing at the sentence::

    the file now reads `ests_returns.py`
    <!-- doc-claims:absent ests_returns.py (corrupted-fragment-of
         tests_returns.py) ; reason: c873b27 ate the leading byte -->

That directive silences at most ONE claim, on at most ONE line: two or more
directives on a line and the line is read as carrying none. A named witness
has to be real -- a ``def`` for a test name, or for a path a tracked file
whose own path ends with the cited fragment, since that fragment is the
intact name with its front truncated. ``retired`` names no witness at all and
is therefore the weakest kind, held only by the review. The reason has to
clear a length floor AND contain a token that looks like a ledger reference;
that second rule is a shape test that raises the cost of filler, not a proof
that anyone can follow the trail, and the review is the control there too. It
is not a suppression list, not a file exemption, and not a regex escape
hatch, and it cannot be produced by editing the sentence: "there is not a
test named ``test_x``" is still an error. The full grammar, and every rule
that keeps it narrow, are on ``parse_counter_examples``.

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

THE BASE MUST MOVE WITH THE PROMOTION. After a train is promoted,
``origin/spec-comp`` is an ANCESTOR of the branch tip, so ``A...B`` is
legitimately empty, the claim half scans nothing, and the step is green
because it looked at nothing (BUG-5, TOOL-01). A base is therefore resolved,
first hit winning:

  1. ``--base REF``
  2. ``$DOC_CLAIMS_BASE``       <- the hook the CI workflow should set
  3. ``origin/$GITHUB_BASE_REF`` when ``$CI`` is set
  4. ``origin/spec-comp``       (the local default, unchanged)

``git diff A...B`` is already the merge-base diff, so naming the PR base branch
is sufficient; no merge-base call is needed. The run also prints the base it
resolved and where it came from, and says so loudly on stderr when the claim
half scanned zero files, so "green" can never again mean "empty".

Usage::

    python scripts/doc_claims.py --base origin/spec-comp
    python scripts/doc_claims.py --base origin/spec-comp --json > claims.json
    python scripts/doc_claims.py --update-baseline

    # in CI, with DOC_CLAIMS_BASE set by the workflow:
    DOC_CLAIMS_BASE="${{ github.event.pull_request.base.sha }}" \\
        python scripts/doc_claims.py

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

DEFAULT_DOCS = ("backend/docs/",)
BYTE_INTEGRITY_DOCS = ("backend/docs/changes.md",)

# Where the diff base comes from, first hit winning. See resolve_base().
BASE_ENV_VAR = "DOC_CLAIMS_BASE"
PR_BASE_ENV_VAR = "GITHUB_BASE_REF"
CI_ENV_VAR = "CI"
FALLBACK_BASE = "origin/spec-comp"

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

# --------------------------------------------------------------------------
# The counter-example directive (BUG-4, then BUG-A/B/C, TOOL-01 c1 and c2).
#
# Grammar, all on ONE source line, inside an HTML comment so it never becomes
# part of the document's voice:
#
#   <!-- doc-claims:absent <test> (renamed-to <test>
#                              | misspelling-of <test>
#                              | retired) ; reason: <text> -->
#   <!-- doc-claims:absent <path> (corrupted-fragment-of <path>)
#                              ; reason: <text> -->
#
# Narrowness, which is the whole point. Each bullet is what the CODE does:
#
#   * AT MOST ONE directive is accepted per line, so at most ONE claim on a
#     line can be silenced by this mechanism. Two or more well-formed
#     directives on one line means NONE is accepted: the script will not pick
#     one, because picking one is a guess, and a guess is what this file
#     refuses to do everywhere else (BUG-A). One line rather than one per
#     document, because a per-document cap would depend on which lines the
#     diff happened to touch -- a cap that changes with the base ref is not a
#     cap, it is a lottery.
#   * it names ONE target, and only that target, so a sibling claim on the same
#     sentence is still checked.
#   * `renamed-to` and `misspelling-of` MUST name a witness, and the witness
#     must have a real `def` in the tree; `corrupted-fragment-of` MUST name a
#     witness that resolves to a tracked file whose path ENDS WITH the cited
#     fragment -- resolving alone would prove only that SOME file exists, and
#     would silence any absent path at all.
#   * a kind is only accepted on a target it can mean: a path target takes
#     `corrupted-fragment-of` and nothing else, so `retired` -- the one kind
#     with no witness to check -- cannot be spent on a path.
#   * the reason must clear MIN_REASON_CHARS, which is a LENGTH FLOOR and buys
#     exactly one thing: that the reason is a fragment rather than a bare
#     token. It is not evidence of anything. What makes a reason auditable is
#     RE_REASON_REFERENCE: it must name a commit sha or a task id, so the
#     statement points at a ledger row a reviewer can open (BUG-B).
#   * a directive NEVER excuses a line past the end of a file that resolved.
#     The file existing is proof that the citation is not a counter-example,
#     so only the missing-file half of a `file.py:123` claim is reachable
#     here at all.
#   * a MALFORMED or over-quota directive honours nothing, and says so in the
#     error it leaves behind. Failing closed is the only safe direction: a
#     typo leaves the gate red, with the reason, and the author finds out.
#
# It is deliberately not satisfiable by rewriting the sentence. "there is not a
# test named test_x" contains no directive and is still a hard error, which is
# the test that pins the guarantee.
# --------------------------------------------------------------------------
MIN_REASON_CHARS = 24

# The checkable half of a reason: a token that LOOKS like a ledger reference.
# Both branches require a DIGIT inside the token, which is the whole fix
# (BUG-B): without it any hyphenated English word passes, and this repository's
# prose is saturated with them, so the rule accepted `well-known`,
# `counter-example`, `e-mail`, `read-only`, `up-to-date` and `so-so`. The sha
# branch needs it for the same reason -- `defaced`, `effaced`, and a bare run of
# a-f letters all matched `[0-9a-f]{7,40}` and named nothing at all.
#
# What this is NOT: proof that the reason is traceable. It is a SHAPE test that
# raises the cost of filler, and the review remains the control. Filler that
# still survives it is any hyphenated token containing a digit -- `a-1`,
# `utf-8`, `sha-1` -- which is recorded as a known residual rather than papered
# over, because the alternative (an uppercase-led token) would reject id forms
# this repository has not written yet and is a decision for the next cycle.
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
# Which kinds each kind of target can carry. A path target gets ONLY the kind
# with a checkable witness, so extending the directive to paths (BUG-C) added
# no new unwitnessed escape.
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
    """Remove the machine-readable directives from the prose being scanned.

    A directive is metadata, not prose: left in place, the name it names and
    the witness it cites would each be extracted as claims in their own right
    and the line would fail on its own annotation. Only comments carrying the
    marker are removed, so a genuine claim cannot be parked in a comment and
    hidden -- and a MALFORMED directive is stripped too, which keeps failing
    closed: it suppresses nothing, it just stops annotating itself.
    """
    return RE_HTML_COMMENT.sub(
        lambda match: "" if "doc-claims:" in match.group(0) else match.group(0), line
    )


# Decidable -> hard failure. Not decidable -> report only.
# ``byte_integrity`` is decided by this script, so a failure there is an error.
ERROR_KINDS = frozenset({"test_name", "module_path", "path_line", "byte_integrity"})


@dataclass(frozen=True)
class CounterExample:
    """One author's explicit statement that a quoted target is reported ABSENT.

    ``target_kind`` is ``test`` or ``path`` and decides which kinds may appear
    here at all. ``witness`` is the surviving name (tests) or the intact file
    (paths); it is empty only for ``retired``, the one kind with nothing to
    check against. ``verify_claims`` refuses a directive whose witness does not
    exist, so a directive can never swap one absent target for another.
    """

    target: str
    target_kind: str
    kind: str
    witness: str
    reason: str


def _directive_kind(match: re.Match) -> tuple[str, str, str]:
    """``(kind, witness)`` for one grammar match, or ``("", "")`` if unknown."""
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

    Returns ``(accepted, refused)``, both keyed by target. Narrow by
    construction, and every refusal carries its reason so the error the
    directive failed to prevent says WHY it was not honoured -- a gate that
    goes red without saying why is a gate the author cannot act on.

    Refused: an over-quota line, a malformed target/kind pairing, a reason
    under the length floor, and a reason that names no ledger reference. A
    target is only ever in one of the two mappings.
    """
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
        # The cap. Refusing ALL of them rather than keeping the first is
        # deliberate: the script does not get to decide which of two
        # statements on one line the author meant.
        for candidate in candidates:
            refused[candidate.target] = OVER_QUOTA
        return {}, refused

    for candidate in candidates:
        accepted[candidate.target] = candidate
    return accepted, refused


@dataclass(frozen=True)
class Claim:
    """One extracted claim. ``value`` is the matched text a human checks.

    ``detail`` is the machine half (a ``path_line`` claim parses its own cited
    path out of it), ``note`` is the human half (why a counter-example
    directive was accepted, for the run log). Keeping them apart is what stops
    an annotation from being parsed back as part of a citation.
    """

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

        A claim only KEEPS its directive when the directive was accepted, so
        ``directive is not None`` is exactly "this absent-name citation was
        stated by its author and is not a claim that the name exists". A
        refused directive is dropped onto a fresh Claim by ``verify_claims``
        and reads as an error again.
        """
        if self.directive is not None:
            return "warn"
        return "error" if self.kind in ERROR_KINDS else "warn"


@dataclass
class Report:
    """Scan result. Serialised to JSON verbatim."""

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


# --------------------------------------------------------------------------
# Pure core. No filesystem, no subprocess -- this is what the unit tests hit.
# --------------------------------------------------------------------------


def _directive_note(directive: CounterExample | None) -> str:
    """The one-line human note for an accepted directive, or ``""``.

    Written for all three kinds by one function so the log cannot show a
    counter-example for a path and stay mute for a test.
    """
    if directive is None:
        return ""
    return f"cited as a counter-example ({directive.kind}): {directive.reason}"


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
        accepted, refused = parse_counter_examples(line)
        # Directives are metadata; claims are read from the prose around them.
        prose = _strip_directive_comments(line)

        # A path cited as `file.py:123` is one claim, not two. Record which
        # names were consumed as path:line so the bare-module pass skips them.
        cited_with_line: set[str] = set()
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
            name = match.group(1)
            # A quoted name is one claim whether it is asserted or reported
            # absent; only an explicit directive on THIS line tells the two
            # apart, and only for the one target the directive spells out.
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

        # A figure already claimed by a narrower pattern is not claimed twice:
        # "exactly 2 places" is one figure, not two, and a doubled figure makes
        # a reviewer count the wrong number of things to check.
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
        # Computed once, and handed to the error text, so the message cannot
        # describe a different failure from the one that actually happened.
        failure = (
            _directive_failure(claim.directive, known_tests, known_paths)
            if claim.directive is not None
            else ""
        )
        if claim.kind == "test_name":
            if claim.value in known_tests:
                warnings.append(claim)
            elif claim.directive is not None and not failure:
                # The author stated this name is being reported ABSENT and the
                # statement survives the one check a machine can make on it.
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
            if _path_exists(claim.value, known_paths):
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
                # No directive reaches this branch, deliberately. The file
                # resolved, which is proof the citation is not a
                # counter-example: a wrong line number is a wrong number, and
                # letting prose declare it absent is how a stale reference gets
                # to live forever.
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

    Returning the reason rather than a bool is what keeps the error honest: a
    single "not in the tree either" for every path failure would be a lie the
    moment the witness resolves and merely is not this fragment's intact form.

    A named witness must be REAL, and it must be the intact form of THIS
    target. A test witness must have a ``def``; a path witness must resolve to
    a tracked file whose own path ENDS WITH the cited fragment. Resolving alone
    would prove only that SOME file exists, so `corrupted-fragment-of
    orders/views.py` would otherwise silence any absent path at all, and a
    directive could swap one absent target for another absent one.

    SUFFIX, not "ends with /<fragment>". A byte-eaten write truncates the
    FRONT of a name, so the intact form is the fragment with something
    prepended: `ests_returns.py` is the intact `tests_returns.py`, and
    requiring a slash before the fragment would reject the very corruption
    case this whole kind exists for. A suffix match still cannot cut into an
    unrelated file, because `orders/views.py` does not end with
    `accounts/whatever.py` or with `whatever.py`.

    ``retired`` names no witness, has nothing to fail, and is the weakest kind
    by construction; the review is the control there.
    """
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
    """Why an absent-target claim is an error, including any failed directive."""
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

    Three passes, most specific first: the exact path, then a suffix match on a
    PATH BOUNDARY, then the bare basename.

    The middle pass is BUG-3 (TOOL-01). Prose cites app-relative paths
    constantly -- ``orders/views.py`` -- and without it the only fallback is a
    basename match, which is ambiguous across the four apps that each own a
    views.py. The result was that a correct citation was reported as an error,
    and 94 of the 121 errors on the wide base were that. The ``/`` in the
    suffix is load-bearing: ``orders/views.py`` is a path, ``ders/views.py`` is
    not, and only the boundary tells them apart.
    """
    normalised = candidate[2:] if candidate.startswith("./") else candidate
    if normalised in known_paths:
        return normalised
    for predicate in (_ends_with_path, _same_basename):
        matches = [p for p in known_paths if predicate(p, normalised)]
        if len(matches) == 1:
            return matches[0]
    return None


def _ends_with_path(tracked: str, cited: str) -> bool:
    """Whether a tracked path ENDS WITH the cited path, at a boundary."""
    return tracked.endswith("/" + cited)


def _same_basename(tracked: str, cited: str) -> bool:
    """Whether a tracked path has the same final segment as the cited one."""
    return tracked.rsplit("/", 1)[-1] == cited.rsplit("/", 1)[-1]


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


def resolve_base(
    explicit: str | None = None, env: dict[str, str] | None = None
) -> tuple[str, str]:
    """Pick the diff base, and say where the choice came from.

    First hit wins:

    1. ``--base REF``
    2. ``$DOC_CLAIMS_BASE`` -- the hook the CI workflow is expected to set
    3. ``origin/$GITHUB_BASE_REF`` when ``$CI`` is set
    4. ``origin/spec-comp`` -- the local default, unchanged

    The base has to MOVE WITH THE PROMOTION or the claim half checks nothing.
    Once a train is promoted ``origin/spec-comp`` is an ancestor of the branch
    tip, ``A...B`` is legitimately empty, and the step goes green having read
    zero lines of prose (BUG-5, TOOL-01). ``git diff A...B`` is already the
    merge-base diff, so naming the PR base branch is enough; no merge-base call
    is needed here.
    """
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

    Silence here is the defect: a gate that scanned zero lines reports zero
    errors and is indistinguishable from a gate that passed. This says so on
    stderr, every run, so "green" cannot quietly mean "empty" again.
    """
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


def run(base: str, head: str, base_source: str = "") -> Report:
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
        # Every honoured counter-example is printed, never silently dropped: a
        # directive that leaves no trace in the log is not reviewable, and
        # "reviewable" is the entire control on the weakest kind.
        for claim in report.warnings:
            if claim.directive is not None:
                print(
                    f"  ABSENT {claim.path}:{claim.line} [{claim.kind}] "
                    f"{claim.value} — {claim.note}"
                )
        if report.warnings:
            print(f"  {len(report.warnings)} claim(s) need a human or an auditor.")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

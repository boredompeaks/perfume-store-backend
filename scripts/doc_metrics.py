"""Measure comment, docstring, logic and documentation volume.

Why this exists
---------------
#80's finding asserted a 45% comment/docstring ratio over 12,870 logic lines,
4,435 comment lines and 6,063 docstring lines. The three figures appear
nowhere in the tree and no command produced them, so the claim could not be
checked -- and a policy pinned to an unmeasured number is itself the defect,
not the ratio.

This script is the command. It pins the definition below, measures the tree,
and compares the result with ``scripts/doc_metrics_baseline.json``.

The definition (``doc-metrics-v1``)
-----------------------------------
* **Universe.** Files git lists for this checkout (``git ls-files``), read from
  the git INDEX (``git cat-file blob :path``), not the working tree.
  ``core.autocrlf`` can put CRLF in a checkout while the commit stores LF, and
  a byte ratio that moves with the local line endings is not a measurement;
  the index is what the next commit contains, and after a commit it equals
  ``HEAD``. A staged file is measured, an unstaged working-tree edit is not.
* **App Python.** ``backend/**/*.py`` excluding the ``migrations`` package
  (Django generates it; nobody wrote it for a reader) and test modules. A test
  module is a file under a ``tests`` path component, or whose stem is
  ``tests``, starts with ``test_``, or starts with ``tests_``.
* **Docs.** ``backend/docs/**/*.md`` bytes, EXCLUDING the append-only
  ``backend/docs/changes.md`` ledger. The ledger gets one row per change by
  policy and cannot be cut, so counting it would make the ratio rise on every
  commit and turn the gate into noise; its size is reported (the ``ledger``
  scope) but not ratcheted.
* **Every physical line is classified exactly once**, first match winning:
    1. blank -- nothing but whitespace, wherever it sits. A blank line inside
       a docstring is still blank: it is separator, not prose the author
       wrote, and counting it as a docstring line would make the count depend
       on how airy a docstring happens to be.
    2. docstring -- inside the string literal that is the first statement of a
       module, class, function or async function. Found with ``ast``, not a
       triple-quote regex, so a triple-quoted constant assigned to a name is
       logic and a ``#`` inside any string is text, not a comment.
    3. comment -- the line's first non-whitespace character begins a ``#``
       token. A line whose first character is code is logic even when a
       trailing comment sits on it (the comment is visible in review; the line
       is still logic).
    4. logic -- everything else, including continuation lines and multi-line
       non-docstring strings.
* **Bytes are raw file sizes**, and the documentation ratio is docs bytes /
  app Python bytes. A line count cannot compare a 20-line doc with a 20-line
  module.
* **Density is reported two ways**, because #80's "45% ... on 12,870 logic
  lines" can be read as either and the difference matters:
    * ``comment_docstring_per_logic = (comment + docstring) / logic``
    * ``comment_docstring_share = (comment + docstring) / (logic + comment +
      docstring)`` -- the share of non-blank-classified lines.
  Comparisons cross-multiply the integer counts, so no float rounding decides
  a pass. The floats in the artifact are printed for humans.

Usage::

    python scripts/doc_metrics.py measure          # human-readable table
    python scripts/doc_metrics.py measure --json   # canonical artifact
    python scripts/doc_metrics.py check            # 1 on regression
    python scripts/doc_metrics.py baseline         # record the current state
    python scripts/doc_metrics.py baseline --accept-increase
    git add scripts/doc_metrics_baseline.json      # check reads the indexed copy

``baseline`` refuses to record a ratio ABOVE the committed one unless
``--accept-increase`` is passed: the baseline is a one-way ratchet so a routine
refresh cannot quietly bless a regression, and an accepted increase is an
explicit, reviewable line in the diff (its reason belongs in the changelog
row, as ``conventions.md`` says). The baseline is read from the git index, like
the measurement, so stage it before ``check`` will see an update.

Exit codes: 0 clean, 1 regression or refused increase, 2 usage, environment or
unparsable-source failure.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import subprocess
import sys
import tokenize
from dataclasses import dataclass, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "scripts" / "doc_metrics_baseline.json"

SCHEMA_VERSION = 1
DEFINITION = "doc-metrics-v1"

APP_PYTHON_PREFIX = "backend/"
DOCS_PREFIX = "backend/docs/"
LEDGER_PATH = "backend/docs/changes.md"

# Git reads repository location from the environment before it looks at the
# worktree. A hook (or a caller) that exports GIT_INDEX_FILE would make
# ``git ls-files`` list THAT index -- empty or partial -- so the measurement
# would silently describe a different tree. The measurement owns its repo and
# its index, so these variables are stripped from every git subprocess.
GIT_LOCATION_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)

BASELINE_NOTE = (
    "Hand-maintained: scripts/doc_metrics.py baseline writes this file only "
    "when an operator runs it. A baseline that refreshed itself would record "
    "whatever the last run measured, which is the defect the check exists to "
    "catch. The integer counts are the source of truth; the metrics block is "
    "derived and printed for humans. Record a higher ratio only via "
    "--accept-increase, and state the reason in the changelog row."
)


class MeasurementError(Exception):
    """A tree that cannot be measured honestly. Exit 2, never a silent skip."""


# --------------------------------------------------------------------------
# Pure core: classification and ratios. No git, no filesystem, no network.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeCounts:
    """Line classes for one measurement scope."""

    files: int = 0
    total_lines: int = 0
    logic_lines: int = 0
    comment_lines: int = 0
    docstring_lines: int = 0
    blank_lines: int = 0
    bytes: int = 0

    def plus(self, other: "ScopeCounts") -> "ScopeCounts":
        return ScopeCounts(
            files=self.files + other.files,
            total_lines=self.total_lines + other.total_lines,
            logic_lines=self.logic_lines + other.logic_lines,
            comment_lines=self.comment_lines + other.comment_lines,
            docstring_lines=self.docstring_lines + other.docstring_lines,
            blank_lines=self.blank_lines + other.blank_lines,
            bytes=self.bytes + other.bytes,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "files": self.files,
            "total_lines": self.total_lines,
            "logic_lines": self.logic_lines,
            "comment_lines": self.comment_lines,
            "docstring_lines": self.docstring_lines,
            "blank_lines": self.blank_lines,
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class Measurement:
    """The scopes the artifact records.

    ``ledger_markdown`` is reported beside the three ratcheted scopes because
    it is documentation the policy cannot ask anyone to cut.
    """

    app_python: ScopeCounts
    test_python: ScopeCounts
    docs_markdown: ScopeCounts
    ledger_markdown: ScopeCounts

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {
            "app_python": self.app_python.as_dict(),
            "test_python": self.test_python.as_dict(),
            "docs_markdown": self.docs_markdown.as_dict(),
            "ledger_markdown": self.ledger_markdown.as_dict(),
        }


@dataclass(frozen=True)
class Metric:
    """One comparable ratio, kept as integers so comparison is exact."""

    name: str
    numerator: int
    denominator: int

    def value(self) -> float:
        if self.denominator == 0:
            raise MeasurementError(f"{self.name}: denominator is zero")
        return self.numerator / self.denominator


def app_metrics(counts: ScopeCounts, docs_bytes: int) -> tuple[Metric, ...]:
    """The three ratios the baseline ratchets, in reporting order."""
    comment_docstring = counts.comment_lines + counts.docstring_lines
    non_blank = counts.logic_lines + comment_docstring
    return (
        Metric("comment_docstring_per_logic", comment_docstring, counts.logic_lines),
        Metric("comment_docstring_share", comment_docstring, non_blank),
        Metric("docs_to_app_python_bytes", docs_bytes, counts.bytes),
    )


def normalise_newlines(source: str) -> str:
    """Translate the three newline spellings Python accepts to ``\\n``.

    ``ast`` and ``tokenize`` agree on line numbers only for the text they are
    both given, and a CR-only source is split differently by a default
    ``StringIO`` reader than by ``split_physical_lines``. Normalising once is
    what keeps the classifier's line numbers and the tokenizer's rows aligned.
    """
    return source.replace("\r\n", "\n").replace("\r", "\n")


def split_physical_lines(source: str) -> list[str]:
    """Split into the physical lines the Python parser numbers.

    ``str.splitlines`` would also split on form feed and Unicode line
    separators, which the tokenizer does NOT number as new lines; that would
    shift every line number after such a byte and misclassify docstrings. Only
    the three newline spellings Python accepts are normalised.
    """
    lines = normalise_newlines(source).split("\n")
    if lines and lines[-1] == "":
        # A trailing newline terminates the last line, it does not start one.
        # Keeping the phantom element would add one blank line per file.
        lines.pop()
    return lines


def _docstring_line_numbers(tree: ast.AST) -> set[int]:
    """Line numbers covered by a real module/class/function docstring."""
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        end = value.end_lineno if value.end_lineno is not None else value.lineno
        covered.update(range(value.lineno, end + 1))
    return covered


def _comment_token_starts(source: str) -> set[tuple[int, int]]:
    """(row, column) of every real COMMENT token, so strings cannot fake one."""
    return {
        token.start
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }


def classify_source(source: str) -> ScopeCounts:
    """Classify every physical line of one Python source.

    Raises ``SyntaxError`` (from ``ast``) rather than guessing at an
    unparsable file: a line that cannot be classified is not measured.
    """
    # One normalised text feeds the splitter, the parser and the tokenizer, so
    # their line numbers cannot disagree about where a line starts.
    normalised = normalise_newlines(source)
    lines = split_physical_lines(normalised)
    tree = ast.parse(normalised)
    docstrings = _docstring_line_numbers(tree)
    comments = _comment_token_starts(normalised)

    logic = comment = docstring = blank = 0
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            blank += 1
        elif number in docstrings:
            docstring += 1
        else:
            # tokenize columns and str offsets both count characters, so the
            # first non-whitespace offset identifies the token that opens the
            # line. A line starting with code has no COMMENT token there.
            opening_column = len(line) - len(line.lstrip())
            if (number, opening_column) in comments:
                comment += 1
            else:
                logic += 1
    return ScopeCounts(
        files=1,
        total_lines=len(lines),
        logic_lines=logic,
        comment_lines=comment,
        docstring_lines=docstring,
        blank_lines=blank,
    )


def is_test_module(relpath: str) -> bool:
    """Whether a tracked Python path is a test module by the pinned rule."""
    path = Path(relpath)
    if "tests" in path.parts:
        return True
    stem = path.stem
    return stem == "tests" or stem.startswith("test_") or stem.startswith("tests_")


def is_migration(relpath: str) -> bool:
    return "migrations" in Path(relpath).parts


def is_app_python(relpath: str) -> bool:
    return (
        relpath.startswith(APP_PYTHON_PREFIX)
        and relpath.endswith(".py")
        and not is_migration(relpath)
        and not is_test_module(relpath)
    )


def is_test_python(relpath: str) -> bool:
    return (
        relpath.startswith(APP_PYTHON_PREFIX)
        and relpath.endswith(".py")
        and not is_migration(relpath)
        and is_test_module(relpath)
    )


def is_ledger_markdown(relpath: str) -> bool:
    return relpath == LEDGER_PATH


def is_doc_markdown(relpath: str) -> bool:
    return (
        relpath.startswith(DOCS_PREFIX)
        and relpath.endswith(".md")
        and not is_ledger_markdown(relpath)
    )


def measurement_payload(measurement: Measurement) -> dict[str, object]:
    """Canonical artifact. Deterministic: no timestamps, no commit id."""
    metrics = app_metrics(
        measurement.app_python, measurement.docs_markdown.bytes
    )
    return {
        "schema": SCHEMA_VERSION,
        "definition": DEFINITION,
        "note": BASELINE_NOTE,
        "scopes": measurement.as_dict(),
        "metrics": {metric.name: round(metric.value(), 6) for metric in metrics},
    }


def compare_to_baseline(
    measurement: Measurement, baseline: dict[str, object]
) -> list[tuple[str, float, float]]:
    """Return ``(metric, baseline_value, current_value)`` for each regression.

    Exact integer cross-multiplication, not float comparison: a tie at the
    baseline's own ratio can round to a false regression if compared as floats.
    """
    old_scopes = baseline.get("scopes")
    if not isinstance(old_scopes, dict):
        raise MeasurementError("baseline is missing a 'scopes' object")
    old_app = _scope_from_payload(old_scopes, "app_python")
    old_docs = _scope_from_payload(old_scopes, "docs_markdown")

    old_metrics = {
        metric.name: metric
        for metric in app_metrics(old_app, old_docs.bytes)
    }
    current_metrics = app_metrics(
        measurement.app_python, measurement.docs_markdown.bytes
    )
    regressions: list[tuple[str, float, float]] = []
    for current in current_metrics:
        # A zero denominator makes every comparison ``0 > 0`` false, so an
        # empty app scope would read as a pass in plain ``check`` and as an
        # error in ``check --json`` (which derives the ratios). Reject it here,
        # before either mode can disagree.
        if current.denominator == 0:
            raise MeasurementError(
                f"{current.name}: current denominator is zero; nothing to compare"
            )
        base = old_metrics[current.name]
        if base.denominator == 0:
            raise MeasurementError(
                f"baseline {current.name} has a zero denominator; re-record it"
            )
        if current.numerator * base.denominator > base.numerator * current.denominator:
            regressions.append(
                (current.name, base.value(), current.value())
            )
    return regressions


def _scope_from_payload(scopes: dict[str, object], name: str) -> ScopeCounts:
    raw = scopes.get(name)
    if not isinstance(raw, dict):
        raise MeasurementError(f"baseline is missing scope {name!r}")
    fields = {}
    for field_name in (
        "files",
        "total_lines",
        "logic_lines",
        "comment_lines",
        "docstring_lines",
        "blank_lines",
        "bytes",
    ):
        value = raw.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise MeasurementError(
                f"baseline {name}.{field_name} is not an integer"
            )
        if value < 0:
            # A negative count is corrupt data, not a measurement. Rejecting
            # it here matters beyond tidiness: a negative denominator makes a
            # regression comparison ``current * base_den > base_num * ...``
            # false, so a corrupt baseline would certify a regression-free run.
            raise MeasurementError(
                f"baseline {name}.{field_name} is negative ({value})"
            )
        fields[field_name] = value
    return ScopeCounts(**fields)


def validate_baseline(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise MeasurementError("baseline root is not a JSON object")
    if payload.get("schema") != SCHEMA_VERSION:
        raise MeasurementError(
            f"baseline schema {payload.get('schema')!r} != {SCHEMA_VERSION}"
        )
    if payload.get("definition") != DEFINITION:
        raise MeasurementError(
            f"baseline definition {payload.get('definition')!r} != {DEFINITION}"
        )
    scopes = payload.get("scopes")
    if not isinstance(scopes, dict):
        raise MeasurementError("baseline is missing a 'scopes' object")
    # Validate every scope once here so a corrupt file fails at load, before a
    # comparison can half-run against it.
    for name in ("app_python", "test_python", "docs_markdown", "ledger_markdown"):
        _scope_from_payload(scopes, name)
    return payload


# --------------------------------------------------------------------------
# Shell: git and the filesystem live here and nowhere else.
# --------------------------------------------------------------------------


def git_environment() -> dict[str, str]:
    """The process environment with git's repository-location keys removed.

    A hook can export ``GIT_INDEX_FILE`` (pre-commit does) or ``GIT_DIR``, and
    git honours those before ``-C``: a measurement run from a hook would read
    the hook's temporary index. The tool measures the repository it lives in,
    so those inherited pointers are deliberately dropped; unrelated variables
    (PATH, locale, config) are preserved.
    """
    environment = dict(os.environ)
    for name in GIT_LOCATION_ENV_VARS:
        environment.pop(name, None)
    return environment


def tracked_files(repo_root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        env=git_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise MeasurementError(f"git ls-files failed: {detail or 'no detail'}")
    names = completed.stdout.decode("utf-8").split("\0")
    return sorted(name for name in names if name)


def index_blob(repo_root: Path, relpath: str) -> bytes:
    """Read one tracked file's staged content, never the working tree.

    ``git cat-file blob :path`` reads the index, whose blobs git normalised on
    ``add`` (so a CRLF checkout still measures the LF content the commit
    stores). The index is also what the next commit will contain, which lets a
    baseline be recorded from a staged tree; after the commit it equals HEAD.
    """
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "blob", f":{relpath}"],
        capture_output=True,
        env=git_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise MeasurementError(
            f"{relpath}: unreadable from the git index ({detail or 'no detail'})"
        )
    return completed.stdout


def index_file_or_none(repo_root: Path, relpath: str) -> bytes | None:
    """The staged bytes of ``relpath``, or ``None`` when it is not in the index."""
    listed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", relpath],
        capture_output=True,
        env=git_environment(),
    )
    if listed.returncode != 0:
        detail = listed.stderr.decode("utf-8", "replace").strip()
        raise MeasurementError(
            f"{relpath}: cannot read the git index ({detail or 'no detail'})"
        )
    if not listed.stdout.strip(b"\0"):
        return None
    return index_blob(repo_root, relpath)


def measure(repo_root: Path = REPO_ROOT) -> Measurement:
    scopes = {
        "app_python": ScopeCounts(),
        "test_python": ScopeCounts(),
        "docs_markdown": ScopeCounts(),
        "ledger_markdown": ScopeCounts(),
    }
    for relpath in tracked_files(repo_root):
        scope = None
        if is_app_python(relpath):
            scope = "app_python"
        elif is_test_python(relpath):
            scope = "test_python"
        elif is_ledger_markdown(relpath):
            scope = "ledger_markdown"
        elif is_doc_markdown(relpath):
            scope = "docs_markdown"
        if scope is None:
            continue

        data = index_blob(repo_root, relpath)

        if scope in ("docs_markdown", "ledger_markdown"):
            file_counts = ScopeCounts(
                files=1,
                total_lines=len(split_physical_lines(data.decode("utf-8", "replace"))),
                bytes=len(data),
            )
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MeasurementError(
                    f"{relpath}: not valid UTF-8 ({exc})"
                ) from exc
            try:
                file_counts = classify_source(text)
            except SyntaxError as exc:
                raise MeasurementError(
                    f"{relpath}: cannot be parsed ({exc.msg}, line {exc.lineno})"
                ) from exc
            file_counts = replace(file_counts, bytes=len(data))
        scopes[scope] = scopes[scope].plus(file_counts)

    return Measurement(
        app_python=scopes["app_python"],
        test_python=scopes["test_python"],
        docs_markdown=scopes["docs_markdown"],
        ledger_markdown=scopes["ledger_markdown"],
    )


def _parse_baseline(raw: bytes, source: str) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MeasurementError(f"{source} is not valid UTF-8: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MeasurementError(f"{source} is not valid JSON: {exc}") from exc
    return validate_baseline(payload)


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, object]:
    """Load the baseline from the working tree, for explicit ``--path`` use."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise MeasurementError(
            f"no baseline at {path}; run 'python scripts/doc_metrics.py baseline'"
        ) from exc
    except OSError as exc:
        raise MeasurementError(f"cannot read baseline {path}: {exc}") from exc
    return _parse_baseline(raw, f"baseline {path}")


def _relative_to_repo(repo_root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def load_index_baseline(
    repo_root: Path = REPO_ROOT, path: Path = BASELINE_PATH
) -> dict[str, object] | None:
    """The baseline the next commit would carry, or ``None`` if not staged.

    The measurement reads the index, so the baseline it is compared against
    must come from the same snapshot. Reading the working-tree file would let
    an unstaged edit (or a deletion) describe a baseline that will not be
    committed -- and ``record_baseline`` would then miss the very increase it
    exists to refuse.
    """
    relpath = _relative_to_repo(repo_root, path)
    if relpath is None:
        raise MeasurementError(
            f"baseline {path} is outside {repo_root}; use an in-repository path"
        )
    raw = index_file_or_none(repo_root, relpath)
    if raw is None:
        return None
    return _parse_baseline(raw, f"baseline {relpath} in the git index")


def record_baseline(
    measurement: Measurement,
    path: Path = BASELINE_PATH,
    accept_increase: bool = False,
    repo_root: Path = REPO_ROOT,
) -> tuple[bool, list[tuple[str, float, float]]]:
    """Write the baseline, unless that would record a higher ratio.

    Returns ``(written, regressions)``. A refusal writes nothing: a baseline
    that can be refreshed past a regression without an explicit flag is not a
    ratchet, it is a snapshot of whatever ran last.

    The previous baseline is read from the INDEX when ``path`` is inside the
    repository, because that is the snapshot the measurement describes; an
    unstaged working-tree deletion or edit cannot erase the comparison. After
    writing, stage the file: ``check`` also reads the indexed copy.
    """
    if _relative_to_repo(repo_root, path) is not None:
        previous = load_index_baseline(repo_root, path)
    else:
        previous = load_baseline(path) if path.exists() else None
    regressions = (
        compare_to_baseline(measurement, previous) if previous is not None else []
    )
    if regressions and not accept_increase:
        return False, regressions
    payload = measurement_payload(measurement)
    try:
        path.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise MeasurementError(f"cannot write baseline {path}: {exc}") from exc
    return True, regressions


# --------------------------------------------------------------------------
# Reporting.
# --------------------------------------------------------------------------


def _percent(part: int, whole: int) -> str:
    return f"{(part / whole) * 100:.1f}%" if whole else "n/a"


def format_measurement(measurement: Measurement) -> str:
    app = measurement.app_python
    docs = measurement.docs_markdown
    ledger = measurement.ledger_markdown
    tests = measurement.test_python
    comment_docstring = app.comment_lines + app.docstring_lines
    non_blank = app.logic_lines + comment_docstring
    per_logic = (
        f"{comment_docstring / app.logic_lines:.4f}" if app.logic_lines else "n/a"
    )
    docs_ratio = f"{docs.bytes / app.bytes:.4f}" if app.bytes else "n/a"
    lines = [
        f"doc_metrics: definition {DEFINITION} (from the git index)",
        "app Python (backend/**, migrations and test modules excluded)",
        f"  files {app.files:,}  bytes {app.bytes:,}",
        (
            f"  total {app.total_lines:,} = logic {app.logic_lines:,}"
            f" + comment {app.comment_lines:,}"
            f" + docstring {app.docstring_lines:,}"
            f" + blank {app.blank_lines:,}"
        ),
        f"  comment+docstring per logic line {per_logic}",
        (
            "  comment+docstring share of non-blank "
            f"{_percent(comment_docstring, non_blank)}"
        ),
        "docs (backend/docs/**/*.md, excluding the append-only ledger)",
        f"  files {docs.files:,}  lines {docs.total_lines:,}  bytes {docs.bytes:,}",
        f"  docs:app Python bytes {docs_ratio}",
        (
            "ledger (backend/docs/changes.md, append-only; reported, not "
            f"ratcheted)  lines {ledger.total_lines:,}  bytes {ledger.bytes:,}"
        ),
        "test Python (reported, excluded from the headline)",
        f"  files {tests.files:,}  logic {tests.logic_lines:,}"
        f"  comment {tests.comment_lines:,}"
        f"  docstring {tests.docstring_lines:,}  blank {tests.blank_lines:,}",
    ]
    return "\n".join(lines)


def _format_regressions(
    regressions: list[tuple[str, float, float]]
) -> str:
    width = max(len(name) for name, _, _ in regressions)
    rows = [
        f"  {name:<{width}}  baseline {base:.6f} -> now {current:.6f}"
        for name, base, current in regressions
    ]
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure comment, docstring, logic and documentation volume."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    measure_parser = subparsers.add_parser(
        "measure", help="print the current measurement"
    )
    measure_parser.add_argument(
        "--json", action="store_true", help="print the canonical JSON artifact"
    )

    check_parser = subparsers.add_parser(
        "check", help="fail when a ratio rose above the committed baseline"
    )
    check_parser.add_argument(
        "--json", action="store_true", help="print machine-readable results"
    )

    baseline_parser = subparsers.add_parser(
        "baseline", help="record the current measurement in the baseline file"
    )
    baseline_parser.add_argument(
        "--accept-increase",
        action="store_true",
        help="record ratios above the committed baseline (needs a written reason)",
    )
    baseline_parser.add_argument(
        "--path",
        type=Path,
        default=BASELINE_PATH,
        help="baseline file to write (default: scripts/doc_metrics_baseline.json)",
    )

    args = parser.parse_args(argv)

    try:
        measurement = measure()

        if args.command == "measure":
            if args.json:
                payload = measurement_payload(measurement)
                print(json.dumps(payload, sort_keys=True, indent=2))
            else:
                print(format_measurement(measurement))
            return 0

        if args.command == "check":
            baseline = load_index_baseline()
            if baseline is None:
                raise MeasurementError(
                    f"no baseline at {BASELINE_PATH.relative_to(REPO_ROOT)} in the "
                    "git index; run 'python scripts/doc_metrics.py baseline' and "
                    "stage the file"
                )
            regressions = compare_to_baseline(measurement, baseline)
            if args.json:
                print(
                    json.dumps(
                        {
                            "measurement": measurement_payload(measurement),
                            "baseline_definition": baseline["definition"],
                            "regressions": [
                                {
                                    "metric": name,
                                    "baseline": base,
                                    "current": current,
                                }
                                for name, base, current in regressions
                            ],
                        },
                        sort_keys=True,
                        indent=2,
                    )
                )
            else:
                print(format_measurement(measurement))
                if regressions:
                    print(
                        "doc_metrics: REGRESSION against "
                        f"{BASELINE_PATH.relative_to(REPO_ROOT)}"
                    )
                    print(_format_regressions(regressions))
                    print(
                        "doc_metrics: cut comment/docstring/doc bytes, or record "
                        "an accepted increase in the same commit."
                    )
                else:
                    print(
                        "doc_metrics: all ratios at or below the committed "
                        f"baseline ({baseline['definition']})."
                    )
            return 1 if regressions else 0

        # args.command == "baseline"
        written, regressions = record_baseline(
            measurement, args.path, args.accept_increase
        )
        if not written:
            print(
                "doc_metrics: refusing to record a baseline ABOVE the committed "
                "one:"
            )
            print(_format_regressions(regressions))
            print(
                "doc_metrics: improve the ratio, or pass --accept-increase and "
                "state the reason in the changelog row."
            )
            return 1
        print(f"doc_metrics: wrote {args.path}")
        if regressions:
            print("doc_metrics: ACCEPTED INCREASE (explicitly, via --accept-increase):")
            print(_format_regressions(regressions))
        return 0

    except MeasurementError as exc:
        print(f"doc_metrics: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

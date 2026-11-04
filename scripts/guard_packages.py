"""Fail when a directory holding Python files is missing its ``__init__.py``.

A worktree wipe removed 28 ``__init__.py`` markers and, in a worse incident,
every tracked file. Python's implicit namespace packages (PEP 420) kept the
imports working: an app directory without a marker is still importable, so no
error is raised, but Django's test discovery stops descending into it and the
runner exits 0 on the tests that remain. A missing package marker is therefore
a silent loss, and silence is what this guard turns into a non-zero exit.

Read-only: it inspects the tree and prints what is missing, nothing else.

Exit codes: 0 = every Python-bearing directory is a package, 1 = at least one
is not (each is printed on its own line), 2 = the root could not be inspected.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Named precisely because these trees carry .py files they never intend to be
# packages: a virtualenv's site-packages, byte-code caches, git internals and
# installed JavaScript dependencies.
EXCLUDED_DIRS = frozenset({"venv", "__pycache__", ".git", "node_modules"})

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "backend"


def find_missing(root: Path) -> list[Path]:
    """Return the Python-bearing directories under ``root`` with no marker."""
    missing: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDED_DIRS)
        current = Path(dirpath)
        if current == root:
            # The Django project root runs manage.py as a script; it is not a
            # package and adding a marker here would change import semantics.
            continue
        has_python = any(name.endswith(".py") for name in filenames)
        if has_python and not (current / "__init__.py").is_file():
            missing.append(current)
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assert every directory with .py files has __init__.py."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=str(DEFAULT_ROOT),
        help="tree to walk (default: the repo's backend/ directory)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"guard_packages: root is not a directory: {root}", file=sys.stderr)
        return 2

    missing = find_missing(root)
    for path in missing:
        print(path.as_posix())
    if missing:
        print(
            f"guard_packages: {len(missing)} directory(ies) with .py files "
            "lack __init__.py; discovery will skip them without an error",
            file=sys.stderr,
        )
        return 1

    print(f"guard_packages: clean ({root.as_posix()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

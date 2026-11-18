"""Back up the configured database (pg_dump or the sqlite online backup API) and prune older dumps.

Reads the source only, writes into BACKUP_DIR, never echoes the connection identity, and exits non-zero on a failed dump.
"""

import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management import BaseCommand, CommandError

# Fixed-width UTC stamps sort lexicographically, which is what makes "keep the
# newest N" a name sort rather than a filesystem-metadata sort.
PREFIX = "perfume"
POSTGRES_SUFFIX = ".dump"
SQLITE_SUFFIX = ".sqlite3"
PARTIAL_SUFFIX = ".partial"
DEFAULT_KEEP = 7

# Both stamped shapes are spelled out rather than wildcarded, so files that
# merely borrow the prefix are never retention candidates.
STAMP_GLOB = f"{PREFIX}-????????T??????Z"
STAMP_GLOBS = (STAMP_GLOB, f"{STAMP_GLOB}-[0-9]*")

# The first bytes of a file the named engine writes; retention reads only
# these to decide whether a candidate is one of ours.
SIGNATURES = {
    POSTGRES_SUFFIX: b"PGDMP",
    SQLITE_SUFFIX: b"SQLite format 3\x00",
}

PG_CLIENT_HINT = "postgresql-client (pg_dump from a newer major also works)"

POSTGRES_ENGINE = "django.db.backends.postgresql"
SQLITE_ENGINE = "django.db.backends.sqlite3"

# Third-party pg_dump errors can quote the connection, its parameter dump and
# the resolved address - none of which is readable from settings - so they are
# masked by shape as well as by value; over-masking is the safe direction.
# IPv4 and compressed/full IPv6 only, so a clock time ("02:30:00") survives.
PARAMETERS_LINE = re.compile(r"(?im)^.*connection parameters:.*$")
ADDRESS = re.compile(
    r"(?<![\w.])(?:"
    r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}"
    r"|[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4})*::(?:[0-9A-Fa-f]{0,4}:)*[0-9A-Fa-f]{0,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r")(?![\w.])"
)


def env_int(name, default):
    """Read an integer env knob, falling back to the default when it is absent or malformed."""
    raw = os.getenv(name)
    if raw is None or not raw.strip().isdigit():
        return default
    return int(raw.strip())


def redact(text, secrets):
    """Replace every secret in ``text`` so pg_dump's error output cannot leak the connection."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***redacted***")
    return text


def scrub(text, secrets):
    """Redact secrets and connection-shaped strings in third-party pg_dump text."""
    text = redact(text, secrets)
    text = PARAMETERS_LINE.sub("Connection parameters: ***redacted***", text)
    return ADDRESS.sub("***redacted***", text)


def utc_stamp(moment=None):
    """A fixed-width UTC timestamp for the dump filename; fixed width keeps retention's name sort correct."""
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def backup_name(stamp, suffix):
    return f"{PREFIX}-{stamp}{suffix}"


def unique_path(directory, name):
    """``directory/name``, disambiguated with ``-1``, ``-2`` when taken, so dumps in the same second never overwrite each other."""
    candidate = Path(directory) / name
    index = 1
    while candidate.exists():
        candidate = Path(directory) / f"{Path(name).stem}-{index}{Path(name).suffix}"
        index += 1
    return candidate


def validate_keep(keep):
    """Return ``keep`` as an int, refusing a count that would delete every backup."""
    keep = int(keep)
    if keep < 1:
        raise CommandError(
            f"backup_db refuses a retention count of {keep}: keeping zero (or "
            f"fewer) dumps would delete every backup the moment it finished "
            f"making one. Pass --keep N with N >= 1, or set BACKUP_RETENTION "
            f"to a positive number."
        )
    return keep


def is_own_dump(path):
    """True when ``path``'s content carries the engine's dump signature, so retention never deletes a file it did not write."""
    signature = SIGNATURES.get(Path(path).suffix)
    if signature is None:
        return False
    try:
        with open(path, "rb") as handle:
            return handle.read(len(signature)) == signature
    except OSError:
        return False


def prune(directory, keep):
    """Delete this command's older dumps, newest first, keeping ``keep``; returns the names removed."""
    keep = validate_keep(keep)
    directory = Path(directory)
    candidates = []
    for suffix in (POSTGRES_SUFFIX, SQLITE_SUFFIX):
        for pattern in STAMP_GLOBS:
            candidates.extend(
                path
                for path in directory.glob(f"{pattern}{suffix}")
                if is_own_dump(path)
            )
    # Newest first; same-second ties are ordered by their disambiguator, which
    # does not matter because retention counts files, not seconds.
    newest = sorted(candidates, key=lambda path: path.name, reverse=True)
    removed = []
    for stale in newest[keep:]:
        stale.unlink()
        removed.append(stale.name)
    return removed


def pg_dump_binary():
    return shutil.which("pg_dump")


class Command(BaseCommand):
    help = (
        "Back the configured database up with pg_dump (PostgreSQL) or the "
        "sqlite online backup API, then apply a retention policy. Reads the "
        "database only, writes into BACKUP_DIR, never echoes the connection "
        "identity, and exits non-zero on a failed dump."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            help="Connection alias to back up (default: default).",
        )
        parser.add_argument(
            "--output-dir",
            help=(
                "Directory to write the dump into. Defaults to the BACKUP_DIR "
                "environment key, which is required."
            ),
        )
        parser.add_argument(
            "--keep",
            type=int,
            help=(
                "How many dumps to keep, newest first (default: the "
                "BACKUP_RETENTION environment key, else 7)."
            ),
        )
        parser.add_argument(
            "--prune-only",
            action="store_true",
            help=(
                "Apply the retention policy without dumping. The way an "
                "operator prunes when the dump itself came from a different "
                "pg_dump binary (for example the postgres image's own)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the plan and exit without reading or writing.",
        )

    def handle(self, *args, **options):
        alias = options["database"]
        config = self._connection(alias)
        keep = options["keep"]
        if keep is None:
            keep = env_int("BACKUP_RETENTION", DEFAULT_KEEP)
        directory, engine = self._plan(alias, config, options)
        # Refuse an impossible retention count before any filesystem work.
        keep = validate_keep(keep)

        if options["dry_run"]:
            self.stdout.write(
                f"DRY RUN: would back up connection '{alias}' "
                f"({self._engine_label(engine)}) into '{directory}', keeping "
                f"the newest {keep} dump(s), then prune the rest"
                + (
                    "; --prune-only, so no dump would be taken."
                    if options["prune_only"]
                    else "."
                )
                + " Nothing was read or written."
            )
            return

        if options["prune_only"]:
            if not directory.is_dir():
                raise CommandError(
                    f"backup_db cannot prune: '{directory}' is not a "
                    f"directory, so there is nothing to prune. Point "
                    f"--output-dir (or BACKUP_DIR) at the directory the dumps "
                    f"are written to."
                )
            removed = prune(directory, keep)
            self.stdout.write(
                f"Pruned {len(removed)} dump(s) from '{directory}', keeping "
                f"the newest {keep}."
            )
            return

        directory.mkdir(parents=True, exist_ok=True)
        destination = self._dump(alias, config, engine, directory)
        size = destination.stat().st_size
        removed = prune(directory, keep)
        self.stdout.write(
            f"Backup written: {destination.name} ({size} bytes, "
            f"{self._engine_label(engine)} connection '{alias}') in "
            f"'{directory}'."
        )
        if removed:
            self.stdout.write(
                f"Pruned {len(removed)} older dump(s), keeping the newest "
                f"{keep}: {', '.join(removed)}"
            )
        self.stdout.write(
            self.style.SUCCESS(
                "Backup complete. To restore, follow "
                "docs/runbook-incident-recovery.md section 3a - a dump is "
                "only useful once it has been restored into a FRESH database "
                "and verified with manage.py restore_drill."
            )
        )

    def _connection(self, alias):
        """The configured connection entry for ``alias``, read from `settings.DATABASES` so it cannot disagree with the parser the app connects with."""
        databases = getattr(settings, "DATABASES", {}) or {}
        if alias not in databases:
            raise CommandError(
                f"backup_db cannot back up unknown connection alias "
                f"'{alias}'. Configured aliases: "
                + (", ".join(sorted(databases)) or "none")
                + "."
            )
        return databases[alias] or {}

    def _plan(self, alias, config, options):
        """Resolve (backup directory, engine), refusing an unsafe target before anything is written; `--dry-run` is subject to it too."""
        engine = config.get("ENGINE") or ""
        if engine not in (POSTGRES_ENGINE, SQLITE_ENGINE):
            raise CommandError(
                f"backup_db cannot back up connection '{alias}': unsupported "
                f"database engine {engine!r}. It knows PostgreSQL (pg_dump) and "
                f"sqlite (the online backup API)."
            )
        raw = (options.get("output_dir") or os.getenv("BACKUP_DIR") or "").strip()
        if not raw:
            raise CommandError(
                "backup_db refuses to run without a backup directory: set "
                "BACKUP_DIR (see backend/.env.example) or pass "
                "--output-dir. A dump written to a container's writable "
                "layer is lost when the container is replaced, which is the "
                "failure this command exists to prevent."
            )
        directory = Path(raw).expanduser()
        if directory.exists() and not directory.is_dir():
            raise CommandError(
                f"backup_db refuses to write into '{directory}': it exists "
                f"and is not a directory."
            )
        return directory, engine

    @staticmethod
    def _engine_label(engine):
        return "postgres" if engine == POSTGRES_ENGINE else "sqlite"

    @staticmethod
    def _secrets(config):
        """The whole postgres connection identity (URL, password, user, host, port, name), which must never reach a terminal or a log."""
        values = [os.environ.get("DATABASE_URL") or ""]
        values.extend(
            str(config.get(key) or "")
            for key in ("PASSWORD", "USER", "HOST", "PORT", "NAME")
        )
        return [value for value in values if value]

    def _dump(self, alias, config, engine, directory):
        """Take one dump and move it into place. Returns the final path."""
        if engine == POSTGRES_ENGINE:
            return self._dump_postgres(alias, config, directory)
        return self._dump_sqlite(alias, config, directory)

    def _dump_postgres(self, alias, config, directory):
        binary = pg_dump_binary()
        if not binary:
            raise CommandError(
                f"backup_db cannot run: pg_dump is not on PATH. Install the "
                f"PostgreSQL client ({PG_CLIENT_HINT}) on the host or image "
                f"that runs this command, or take the dump with the postgres "
                f"image's own pg_dump and apply the retention policy with "
                f"`manage.py backup_db --prune-only` (see "
                f"docs/deploy-runbook.md, 'Backups')."
            )
        destination = unique_path(directory, backup_name(utc_stamp(), POSTGRES_SUFFIX))
        secrets = self._secrets(config)
        partial = Path(f"{destination}{PARTIAL_SUFFIX}")
        try:
            with open(partial, "wb") as handle:
                # stdout is the file, so the dump never passes through this
                # process's memory or its own output buffer.
                completed = subprocess.run(
                    self._pg_argv(binary),
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    env=self._pg_environment(config),
                    check=False,
                )
            if completed.returncode != 0:
                raise CommandError(
                    f"backup_db FAILED for connection '{alias}': pg_dump exited "
                    f"{completed.returncode} and no dump was kept. "
                    f"{self._pg_error(completed, secrets)}"
                )
            os.replace(partial, destination)
        except OSError as exc:
            raise CommandError(
                f"backup_db could not write the dump for connection '{alias}' "
                f"into '{directory}': {scrub(str(exc), secrets)}"
            ) from exc
        finally:
            if partial.exists():
                # A partial dump must never look like a backup or become a
                # retention candidate (the glob does not match `.partial`).
                partial.unlink()
        return destination

    @staticmethod
    def _pg_argv(binary):
        """``pg_dump`` flags only: the connection travels in libpq ``PG*`` env vars, so the process table cannot expose it; ``--no-password`` fails loudly instead of prompting."""
        return [
            binary,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--no-password",
        ]

    @staticmethod
    def _pg_environment(config):
        """The child environment: the parent's, plus the libpq connection."""
        environment = dict(os.environ)
        options = config.get("OPTIONS") or {}
        for variable, value in (
            ("PGHOST", config.get("HOST")),
            ("PGPORT", config.get("PORT")),
            ("PGUSER", config.get("USER")),
            ("PGPASSWORD", config.get("PASSWORD")),
            ("PGDATABASE", config.get("NAME")),
            # The URL's query params reach OPTIONS, so a remote managed
            # database is dumped over the same TLS it is served with.
            ("PGSSLMODE", options.get("sslmode")),
        ):
            if value:
                environment[variable] = str(value)
        return environment

    @staticmethod
    def _pg_error(completed, secrets):
        """A redacted, bounded excerpt of `pg_dump`'s own error output."""
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
        detail = scrub(detail, secrets)
        if not detail:
            return "pg_dump reported no diagnostic output."
        return "pg_dump said:\n" + "\n".join(detail.splitlines()[-5:])

    def _dump_sqlite(self, alias, config, directory):
        name = str(config.get("NAME") or "")
        if not name or ":memory:" in name or "mode=memory" in name:
            raise CommandError(
                f"backup_db cannot back up connection '{alias}': it names an "
                f"in-memory sqlite database, which has nothing on disk to "
                f"back up. Point --database at the deployment's file-backed "
                f"database."
            )
        source = Path(name)
        if not source.is_file():
            raise CommandError(
                f"backup_db cannot back up connection '{alias}': '{source}' is "
                f"not a file. A dump of a database that is not there is a "
                f"backup of nothing."
            )
        destination = unique_path(directory, backup_name(utc_stamp(), SQLITE_SUFFIX))
        partial = Path(f"{destination}{PARTIAL_SUFFIX}")
        try:
            # mode=ro guarantees this command cannot modify the database it
            # copies, whatever happens below.
            uri = f"{source.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as reader:
                with closing(sqlite3.connect(partial)) as writer:
                    # The online backup API copies page by page, so the copy
                    # is consistent even while the app is writing.
                    reader.backup(writer)
            os.replace(partial, destination)
        except (sqlite3.Error, OSError) as exc:
            raise CommandError(
                f"backup_db FAILED for connection '{alias}': {exc}"
            ) from exc
        finally:
            if partial.exists():
                partial.unlink()
        return destination

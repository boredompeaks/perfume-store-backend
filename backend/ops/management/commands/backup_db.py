"""SPEC-22-02 [R-22.9]: the backup mechanism, with retention.

`restore_drill` rehearses the restore; it is not a backup. Until this command
existed the repository had no backup capability at all - no dump, no retention
policy and no schedule - while the incident runbook deferred the whole story
to the deployment section. This is that story, in code, so an operator can take
a real backup and keep a known number of them.

Two engines, because this project ships both:

* **PostgreSQL** - `pg_dump` against the configured `DATABASE_URL`. The
  connection identity (host, port, user, password, database, sslmode) travels
  in the CHILD's environment as the libpq `PG*` variables, never in argv:
  argv is world-readable through `ps`, and a backup job is exactly the kind of
  thing an operator runs while pasting output into a ticket. `--no-password`
  means a wrong or missing password fails the dump immediately instead of
  blocking a cron entry on a prompt nobody will ever answer.
* **sqlite** (the local/dev database) - the stdlib online backup API, which
  copies page by page and is therefore consistent even while the app is
  writing. A file copy of a live sqlite database is not.

Safety, structural rather than advisory:

* the source is only ever READ. The sqlite database is opened `mode=ro`, and
  the postgres path is a client-side dump. Nothing here can write to, migrate
  or drop the database it backs up;
* the dump is written to a `.partial` file and moved into place only after it
  succeeds, so a failed or interrupted backup can never be mistaken for a good
  one - and the partial file is removed on the failure path;
* a backup directory is REQUIRED (`BACKUP_DIR`, or `--output-dir`). A dump
  written into a container's writable layer is lost when the container is
  replaced, which is precisely the failure this command exists to prevent, so
  an unconfigured run is refused rather than guessed at;
* the dump always lands on a FRESH filename (`unique_path`), so a backup can
  never overwrite an existing dump - or the database, if the operator's
  database happens to be named like one;
* **no output ever contains the database connection.** Messages name the
  alias, the engine and the file. Everything captured from `pg_dump` is
  redacted against the whole connection identity - the URL, the password, the
  host, the port, the user and the database name - and then masked by shape,
  because libpq also prints its own parameter dump and the ADDRESS IT RESOLVED,
  which are in no setting this command can read;
* a failed dump exits non-zero (`CommandError`) - a backup that failed must be
  loud, or the cadence silently produces nothing;
* retention deletes only files this command wrote - the stamped filename
  scheme (`STAMP_GLOBS`) AND the engine's own file signature (`is_own_dump`)
  must both line up - newest first, keeping `--keep` (default
  `BACKUP_RETENTION`, else 7). A file an operator parked in the backup
  directory, including a hand-taken dump that borrowed the prefix, is never a
  deletion candidate.

See `docs/deploy-runbook.md` ("Backups") for the schedule, the volume and
off-host requirements, and `docs/runbook-incident-recovery.md` for restoring.
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

# The filename scheme this command owns: `<PREFIX>-<UTC stamp>[-<n>]<suffix>`.
# Fixed-width UTC stamps sort lexicographically, which is what makes "keep the
# newest N" a name sort rather than a filesystem-metadata sort. Retention
# matches ONLY this scheme (`STAMP_GLOBS`) and only files whose bytes are a dump
# of ours (`is_own_dump`), so pruning can never delete something else.
PREFIX = "perfume"
POSTGRES_SUFFIX = ".dump"
SQLITE_SUFFIX = ".sqlite3"
PARTIAL_SUFFIX = ".partial"
DEFAULT_KEEP = 7

# Retention's name test, in one place: the stamped shape
# `<PREFIX>-<8 digits>T<6 digits>Z`, plus the `-<n>` disambiguator `unique_path`
# gives a second dump taken inside the same second. Both shapes are spelled out
# rather than followed by a wildcard - a trailing `*` would let any junk after
# the stamp match, which is exactly the prefix-only rule this replaces. So
# `perfume-latest.dump` and `perfume-2026.dump`, which borrow the prefix, are
# never even candidates.
STAMP_GLOB = f"{PREFIX}-????????T??????Z"
STAMP_GLOBS = (STAMP_GLOB, f"{STAMP_GLOB}-[0-9]*")

# The first bytes of a file the named engine writes. Retention reads only these
# (never the whole dump) to decide whether a candidate is one of ours:
# `pg_dump --format=custom` and the sqlite file header.
SIGNATURES = {
    POSTGRES_SUFFIX: b"PGDMP",
    SQLITE_SUFFIX: b"SQLite format 3\x00",
}

# Named in the "pg_dump is not installed" refusal. It is a hint, not a pin:
# pg_dump from a newer major can read an older server, so an operator is not
# boxed into one version.
PG_CLIENT_HINT = "postgresql-client (pg_dump from a newer major also works)"

POSTGRES_ENGINE = "django.db.backends.postgresql"
SQLITE_ENGINE = "django.db.backends.sqlite3"

# Third-party error text carries the shape of a connection that no configured
# value can match, so it is masked by shape as well as by value:
#
# * libpq prints its `key=value` parameter dump on failure - the whole
#   connection in one line;
# * libpq reports the ADDRESS IT RESOLVED (`connection to server at "db"
#   (10.42.0.7)`), which is discovered at connect time and is in no setting this
#   command can read. An internal address is deployment topology: not a
#   credential, but not something a cron mail should hand out either.
#
# IPv4, the compressed IPv6 form and the full 8-group IPv6 form only. A clock
# time ("02:30:00") has two colons and no "::", so it survives; an over-masked
# excerpt is the safe direction of failure.
PARAMETERS_LINE = re.compile(r"(?im)^.*connection parameters:.*$")
ADDRESS = re.compile(
    r"(?<![\w.])(?:"
    r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}"
    r"|[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4})*::(?:[0-9A-Fa-f]{0,4}:)*[0-9A-Fa-f]{0,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r")(?![\w.])"
)


def env_int(name, default):
    """Read an integer env knob, ignoring anything that is not one.

    Same fail-safe shape as the settings-level knobs: an absent or malformed
    value falls back to the documented default rather than crashing a backup
    at 3am.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip().isdigit():
        return default
    return int(raw.strip())


def redact(text, secrets):
    """Replace every secret in ``text`` with a placeholder.

    `pg_dump` error text is third-party output that has been known to quote
    the connection it was given; printing it verbatim would turn a backup
    failure into a credential leak in a cron mail or a CI log.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***redacted***")
    return text


def scrub(text, secrets):
    """Redact the secrets AND the shape of a connection in third-party text.

    `redact` can only hide what this command was configured with, and a
    `pg_dump` failure quotes more than that: libpq dumps its whole parameter
    list and names the address it resolved. Those are masked by shape, so a
    deployment's internal topology cannot ride out of a failed backup into
    somebody's inbox either.
    """
    text = redact(text, secrets)
    text = PARAMETERS_LINE.sub("Connection parameters: ***redacted***", text)
    return ADDRESS.sub("***redacted***", text)


def utc_stamp(moment=None):
    """A fixed-width UTC timestamp for the dump filename.

    Fixed width matters: retention sorts these names to find the newest dumps,
    and a variable-width timestamp would sort wrongly.
    """
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def backup_name(stamp, suffix):
    return f"{PREFIX}-{stamp}{suffix}"


def unique_path(directory, name):
    """``directory/name``, disambiguated with ``-1``, ``-2`` if it is taken.

    Two backups inside the same second must both survive: a cron entry that
    fires on a boundary (and a manual run right behind it) must not overwrite
    the dump it just made.
    """
    candidate = Path(directory) / name
    index = 1
    while candidate.exists():
        candidate = Path(directory) / f"{Path(name).stem}-{index}{Path(name).suffix}"
        index += 1
    return candidate


def validate_keep(keep):
    """Return ``keep`` as an int, refusing a count that would delete backups.

    Kept zero (or fewer) would delete every backup the moment it finished
    making one, which is a silent way to end up with no backups at all.
    """
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
    """True when ``path``'s CONTENT is a dump of the kind this command writes.

    The name is `prune`'s business - it only ever hands this function files from
    the stamped glob (`STAMP_GLOB`) - and content is what makes the claim true.
    Retention is a deletion loop: a file an operator parked in the backup
    directory keeps its name, but a hand-taken `pg_dump` (plain SQL, by
    default), a note, or somebody else's copy does not carry the engine's
    signature, so it is not a candidate and is never deleted.

    Checking content rather than trusting the name alone is also what keeps the
    documented `--prune-only` route working: a dump the postgres image's own
    pg_dump wrote is identical by name to ours, and by signature it is exactly
    the artefact retention is meant to keep.
    """
    signature = SIGNATURES.get(Path(path).suffix)
    if signature is None:
        return False
    try:
        with open(path, "rb") as handle:
            return handle.read(len(signature)) == signature
    except OSError:
        # A file retention cannot read is a file retention will not delete.
        return False


def prune(directory, keep):
    """Delete this command's older dumps, newest first, keeping ``keep``.

    Returns the names removed. Only files this command wrote are considered -
    the stamped name scheme (`STAMP_GLOBS`) AND the engine's own file signature
    (`is_own_dump`) must both line up - so a file an operator parked in the
    backup directory is never a deletion candidate.
    """
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
    # Newest first. Two dumps stamped in the same second are ordered by their
    # disambiguating suffix, which is arbitrary between them and does not
    # matter: retention counts files, not seconds.
    newest = sorted(candidates, key=lambda path: path.name, reverse=True)
    removed = []
    for stale in newest[keep:]:
        stale.unlink()
        removed.append(stale.name)
    return removed


def pg_dump_binary():
    """The ``pg_dump`` executable on PATH, or None when it is not installed."""
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
        # Refuse an impossible retention count before any filesystem work, so
        # a rejected run cannot have half-applied anything.
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

    # -- plan -------------------------------------------------------------

    def _connection(self, alias):
        """The configured connection entry for ``alias``.

        Read straight off `settings.DATABASES`, which is the single parser
        that already fail-closes a production `DATABASE_URL` (SPEC-22-03).
        Re-parsing the URL here would be a second parser that could disagree
        with the one the app actually connects with.
        """
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
        """Resolve (backup directory, engine), refusing an unsafe target.

        Every refusal happens before a single byte is written and before any
        subprocess starts, and `--dry-run` is subject to it too: a dry run is
        a report, not an exemption.
        """
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

    # -- dumping ----------------------------------------------------------

    @staticmethod
    def _secrets(config):
        """Every string that must never reach a terminal, a log or a ticket.

        The whole postgres CONNECTION IDENTITY, not just the password: a
        `pg_dump` failure quotes `user "..."` and `dbname=...` right next to the
        connection error, and an operator pasting that into a ticket hands out
        the deployment's database coordinates. Called only on the postgres path
        - the sqlite connection has no identity to hide (its NAME is a file
        path the operator needs in order to act on the error).
        """
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
                # stdout IS the file: the dump never passes through this
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
                # A partial dump must never look like a backup, and must never
                # become a retention candidate (the glob does not match
                # `.partial`).
                partial.unlink()
        return destination

    @staticmethod
    def _pg_argv(binary):
        """`pg_dump` flags only - no connection identity.

        The host, port, user and database travel in the child's environment
        (the libpq `PG*` variables) instead, so the process table cannot be
        used to read the deployment's database coordinates. `--no-password`
        makes a missing or wrong password a fast, loud failure rather than an
        unattended prompt that hangs the scheduler forever.
        """
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
            # The URL's query params reach OPTIONS (SPEC-22-01), so a remote
            # managed database is dumped over the same TLS it is served with.
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
            # mode=ro is the structural guarantee that this command cannot
            # modify the database it is copying, whatever happens below.
            uri = f"{source.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as reader:
                with closing(sqlite3.connect(partial)) as writer:
                    # The online backup API copies page by page, so the copy is
                    # consistent even while the app is writing.
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

"""SPEC-21-3 [R-21.2.12]: a hermetic database-restore rehearsal.

There was no DB-restore or incident-recovery drill, so "can we get the data
back" was an untested assumption. This command rehearses the recovery path
end to end and reports pass/fail through its exit code, so CI or an operator
can run it as a gate.

Safety is structural, not advisory:

* the drill REFUSES to run unless the connection it would read from is a
  throwaway database (in-memory, or a file under the system temp directory),
  or is not the database this deployment is configured to use, or is the test
  database the test runner derives from that configuration. Anything else
  raises CommandError before a single query is issued, so the drill can never
  drop, migrate or overwrite a real database;
* every write lands in a temp directory that the drill creates and then
  deletes. The restore target is a fresh sqlite file with its own connection
  alias -- not an alias an operator pointed at;
* the source database is only ever READ (``dumpdata``). No DROP, no
  ``flush``, no migrate against the source under any flag;
* ``--dry-run`` reports the plan and exits without touching anything.

The scenario is the real one: snapshot the database with ``dumpdata``,
restore it into an empty database that was built by running the migrations,
then verify the restore by comparing per-model row counts. That catches the
failure that matters after a restore -- a snapshot that loads but is missing
rows, because a restore was partial or the backup was truncated.
"""

import os
import tempfile
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import BaseCommand, CommandError, call_command
from django.db import connections
from django.db.backends.base.creation import TEST_DATABASE_PREFIX
from django.db.utils import ConnectionDoesNotExist, DatabaseError, load_backend

# The scratch connection the restore lands in. Deliberately not a name an
# operator would configure: the drill owns it and removes it again.
RESTORE_ALIAS = "restore_drill_scratch"

# The names this process STARTED with, read at import -- before anything can
# rewrite them. Django's test runner rewrites BOTH settings.DATABASES[alias]
# ["NAME"] and connection.settings_dict["NAME"] to the database it creates, so
# by the time a command runs the connection's name and the deployment's
# configured name are the same string. That makes "a database other than the
# configured one" carry no information, and the drill refused the very test
# database it had been pointed at. Captured before the rewrite is what the
# fourth shape below is decided from.
CONFIGURED_AT_IMPORT = {
    alias: {
        "NAME": str((config or {}).get("NAME") or ""),
        "TEST_NAME": str(((config or {}).get("TEST") or {}).get("NAME") or ""),
    }
    for alias, config in settings.DATABASES.items()
}


def _within(path, root):
    """True when ``path`` is ``root`` or lives under it.

    Pure string work on absolute paths, so the rule can be exercised without
    creating anything.
    """
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    return path == root or path.startswith(root + os.sep)


def is_rehearsal_safe(name, configured, start_name="", start_test_name=""):
    """Whether the drill may read from a database called ``name``.

    ``configured`` is the name this deployment is configured with as this
    process sees it NOW. ``start_name`` and ``start_test_name`` are that same
    connection's name and ``TEST["NAME"]`` as they were when this module was
    imported, which is before the test runner rewrites the live one. Both are
    needed because after the rewrite ``configured`` names the test database too,
    and the two being equal is exactly the case that must not be read as "the
    configured production database".

    Four shapes pass:
      * an in-memory database (the test runner's default),
      * a file inside the system temp directory (a throwaway an operator or
        CI created on purpose),
      * any name that is not the configured production database,
      * the test database the test runner derives from what this process started
        with: ``TEST["NAME"]`` where one is configured, otherwise the start name
        under the runner's own prefix. The shape is matched exactly, and with
        nothing captured -- an alias this process never saw, or a command module
        imported after the rewrite -- it cannot be satisfied at all, so the drill
        refuses instead of assuming a capture it never had.
    """
    if not name:
        return False
    if name == ":memory:" or "mode=memory" in name:
        return True
    if _within(name, tempfile.gettempdir()):
        return True
    if name != configured:
        return True
    if start_name and name == f"{TEST_DATABASE_PREFIX}{start_name}":
        return True
    return bool(start_test_name) and name == start_test_name


def _row_counts(alias):
    """Objects per model label on ``alias`` -- the restore's verification
    measure. Read through the app registry so a missing table surfaces as a
    failed comparison rather than a silently skipped model."""
    counts = {}
    for model in apps.get_models():
        counts[model._meta.label] = model.objects.using(alias).count()
    return counts


class Command(BaseCommand):
    help = (
        "Rehearse a database restore against a throwaway copy and verify it "
        "(hermetic: refuses to read a real database, never writes outside a "
        "temp directory it created)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            choices=tuple(connections),
            help="Connection alias to snapshot (default: default).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the plan and exit without reading or writing.",
        )
        parser.add_argument(
            "--simulate-failure",
            action="store_true",
            help=(
                "Corrupt the restored copy after loading it, so the "
                "verification fails and the drill exits non-zero. This is how "
                "the failure path is exercised in CI."
            ),
        )

    def handle(self, *args, **options):
        alias = options["database"]
        configured = str((settings.DATABASES.get(alias) or {}).get("NAME") or "")
        origin = CONFIGURED_AT_IMPORT.get(alias) or {}
        # Resolving the connection builds its wrapper; it opens nothing, so the
        # safety decision costs no query and applies to every mode below --
        # including --dry-run, which is a report, not an exemption.
        name = str(connections[alias].settings_dict.get("NAME") or "")

        if not is_rehearsal_safe(
            name, configured, origin.get("NAME", ""), origin.get("TEST_NAME", "")
        ):
            raise CommandError(
                f"restore_drill refuses to run: connection '{alias}' points at "
                f"'{name}', which is neither an in-memory database, a file "
                f"under the system temp directory, nor a database other than "
                f"the configured '{configured}', nor the test database derived "
                f"from it. The drill never reads a real database; point "
                f"--database at a test database."
            )

        if options["dry_run"]:
            self.stdout.write(
                f"DRY RUN: would dump connection '{alias}' ('{name}') to a "
                f"temp fixture, migrate an empty throwaway database, load "
                f"the dump into it, and compare per-model row counts. Nothing "
                f"was read or written."
            )
            return

        workdir = Path(tempfile.mkdtemp(prefix="restore-drill-"))
        try:
            snapshot = workdir / "snapshot.json"
            before = self._snapshot(alias, snapshot)
            after = self._restore(snapshot, workdir / "restored.sqlite3")
            if options["simulate_failure"]:
                self._corrupt_restored_copy()
                # the counts are re-read, not reused: the corruption has to
                # be visible to the comparison for the drill to fail
                after = _row_counts(RESTORE_ALIAS)
            self._verify(before, after, snapshot)
        except DatabaseError as exc:
            # An unmigrated or unreachable database is an operator mistake,
            # not a bug: say so plainly instead of dumping a traceback, and
            # still exit non-zero.
            raise CommandError(
                f"restore_drill could not run against connection '{alias}' "
                f"('{name}'): {exc}"
            ) from exc
        finally:
            self._discard_scratch_connection()
            self._remove_workdir(workdir)

    # -- steps -----------------------------------------------------------

    def _snapshot(self, alias, snapshot):
        """Dump the source into a fixture file (read-only on the source)."""
        before = _row_counts(alias)
        with snapshot.open("w", encoding="utf-8") as handle:
            call_command("dumpdata", database=alias, stdout=handle, verbosity=0)
        self.stdout.write(
            f"Snapshot: {sum(before.values())} objects across "
            f"{len(before)} models -> {snapshot.name}"
        )
        return before

    def _restore(self, snapshot, target):
        """Build an empty database from the migrations, then load the dump
        into it on a scratch connection the drill owns."""
        backend = load_backend("django.db.backends.sqlite3")
        # Registered on the connection handler's thread-local store only,
        # deliberately NOT in settings.DATABASES: the alias must not outlive
        # the command, and nothing else in the process may resolve it.
        connections[RESTORE_ALIAS] = backend.DatabaseWrapper(
            {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": str(target),
                "ATOMIC_REQUESTS": False,
                "AUTOCOMMIT": True,
                "CONN_MAX_AGE": 0,
                "CONN_HEALTH_CHECKS": False,
                "OPTIONS": {},
                "TIME_ZONE": None,
                "USER": "",
                "PASSWORD": "",
                "HOST": "",
                "PORT": "",
                "TEST": {},
            },
            RESTORE_ALIAS,
        )
        # The counts below are read through the same alias, so the scratch
        # connection deliberately outlives this method;
        # _discard_scratch_connection closes and unregisters it once
        # verification is done (handle()'s finally).
        call_command(
            "migrate",
            database=RESTORE_ALIAS,
            verbosity=0,
            interactive=False,
        )
        call_command("loaddata", str(snapshot), database=RESTORE_ALIAS, verbosity=0)
        after = _row_counts(RESTORE_ALIAS)
        self.stdout.write(
            f"Restore: {sum(after.values())} objects loaded into {target.name}"
        )
        return after

    def _corrupt_restored_copy(self):
        """Simulate the damage a real incident produces: the dump loads, but
        the restored copy is missing rows. Removing one restored row is enough
        for the count comparison to fail, which is exactly the signal an
        operator needs from this drill."""
        removed = None
        for model in apps.get_models():
            row = model.objects.using(RESTORE_ALIAS).order_by("pk").first()
            if row is not None:
                model.objects.using(RESTORE_ALIAS).filter(pk=row.pk).delete()
                removed = model._meta.label
                break
        # An empty restored copy has no row to drop, and comparing two empty
        # counts is a truthful pass -- so that case is reported plainly
        # instead of being dressed up as a failure.
        self.stdout.write(
            f"Simulated failure: removed one {removed} row from the restored copy"
            if removed
            else (
                "Simulated failure: the restored copy holds no rows, so there "
                "was nothing to remove; the comparison below is the real result"
            )
        )

    def _verify(self, before, after, snapshot):
        """Compare the snapshot against the restored copy model by model."""
        labels = sorted(set(before) | set(after))
        short = [
            f"{label}: snapshot {before.get(label, 0)} -> restored "
            f"{after.get(label, 0)}"
            for label in labels
            if before.get(label, 0) != after.get(label, 0)
        ]
        if short:
            raise CommandError(
                f"Restore verification FAILED for {len(short)} model(s): "
                + "; ".join(short)
                + f". The dump in {snapshot.name} did not round-trip."
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Restore drill PASSED: {sum(before.values())} objects across "
                f"{len(before)} models round-tripped and verified."
            )
        )

    # -- teardown --------------------------------------------------------

    def _discard_scratch_connection(self):
        """Close and forget the scratch alias, so the drill leaves the
        process with exactly the connections it started with."""
        try:
            connections[RESTORE_ALIAS].close()
        except ConnectionDoesNotExist:
            return
        del connections[RESTORE_ALIAS]

    def _remove_workdir(self, workdir):
        """Delete everything the drill wrote, including the restored sqlite
        file and its -wal/-shm siblings."""
        for leftover in sorted(workdir.glob("*")):
            leftover.unlink()
        workdir.rmdir()

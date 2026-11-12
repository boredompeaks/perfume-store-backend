"""A hermetic database-restore rehearsal: snapshot, restore into a freshly migrated database, and verify per-model row counts.

Refuses to read a real database (only in-memory, temp-dir, non-configured, or test databases) and exits non-zero on failure.
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

# The scratch connection the restore lands in; the drill owns it and removes
# it again.
RESTORE_ALIAS = "restore_drill_scratch"

# Captured at import, before Django's test runner rewrites both
# settings.DATABASES[alias]["NAME"] and connection.settings_dict["NAME"] to the
# test database it creates -- which would make "other than the configured one"
# carry no information.
CONFIGURED_AT_IMPORT = {
    alias: {
        "NAME": str((config or {}).get("NAME") or ""),
        "TEST_NAME": str(((config or {}).get("TEST") or {}).get("NAME") or ""),
    }
    for alias, config in settings.DATABASES.items()
}


def _within(path, root):
    """True when ``path`` is ``root`` or lives under it."""
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    return path == root or path.startswith(root + os.sep)


def is_rehearsal_safe(name, configured, start_name="", start_test_name=""):
    """Whether the drill may read from a database called ``name``: in-memory, under the system temp dir, not the configured database, or the test database the runner derives from the names captured at import."""
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
    """Objects per model label on ``alias``, read through the app registry so a missing table surfaces as a failed comparison."""
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
        # Resolving the connection opens nothing, so the safety decision
        # costs no query and applies to --dry-run too.
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
                # the counts are re-read, not reused, so the corruption is
                # visible to the comparison
                after = _row_counts(RESTORE_ALIAS)
            self._verify(before, after, snapshot)
        except DatabaseError as exc:
            # An unmigrated or unreachable database is an operator mistake,
            # not a bug: say so plainly and still exit non-zero.
            raise CommandError(
                f"restore_drill could not run against connection '{alias}' "
                f"('{name}'): {exc}"
            ) from exc
        finally:
            self._discard_scratch_connection()
            self._remove_workdir(workdir)

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
        """Build an empty database from the migrations, then load the dump into it on a scratch connection the drill owns."""
        backend = load_backend("django.db.backends.sqlite3")
        # Registered only on the connection handler's thread-local store,
        # deliberately not in settings.DATABASES, so the alias cannot outlive
        # the command or be resolved elsewhere.
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
        # The counts are read through the same alias, so the scratch
        # connection outlives this method; handle()'s finally closes it.
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
        """Simulate the damage a real incident produces: the dump loads, but the restored copy is missing rows."""
        removed = None
        for model in apps.get_models():
            row = model.objects.using(RESTORE_ALIAS).order_by("pk").first()
            if row is not None:
                model.objects.using(RESTORE_ALIAS).filter(pk=row.pk).delete()
                removed = model._meta.label
                break
        # An empty copy has no row to drop, and comparing two empty counts is
        # a truthful pass, so it is reported plainly rather than as a failure.
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

    def _discard_scratch_connection(self):
        """Close and forget the scratch alias so the process ends with the connections it started with."""
        try:
            connections[RESTORE_ALIAS].close()
        except ConnectionDoesNotExist:
            return
        del connections[RESTORE_ALIAS]

    def _remove_workdir(self, workdir):
        """Delete everything the drill wrote, including the sqlite file and its -wal/-shm siblings."""
        for leftover in sorted(workdir.glob("*")):
            leftover.unlink()
        workdir.rmdir()

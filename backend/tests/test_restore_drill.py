"""SPEC-21-3 [R-21.2.12]: the restore drill's hermetic-safety pins.

Three properties matter more than the drill's happy path, so they are pinned
first: it refuses to read a real database, ``--dry-run`` writes nothing, and
it exits non-zero when the restore does not round-trip. The passing run is
pinned too, otherwise "exits non-zero" could be satisfied by a command that
always fails.

The refusals run through the real path -- the command reads its own
connection configuration, decides, and stops. No safety check is patched out,
and the "production" database is a path that is never created, so the refusal
tests cannot touch the filesystem at all.
"""

import io
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection, connections
from django.db.utils import ConnectionDoesNotExist, load_backend
from django.test import SimpleTestCase, override_settings, tag

from common.testing import ApiTestCase
from ops.management.commands.restore_drill import (
    RESTORE_ALIAS,
    _within,
    is_rehearsal_safe,
)

# An absolute path that looks exactly like a deployment's database file and
# that this test suite never creates.
PRODUCTION_DB = os.path.abspath(
    os.sep + os.path.join("srv", "perfume-store", "db.sqlite3")
)


def _sqlite_settings(alias, name):
    """A complete sqlite connection entry under ``alias``, so the override is
    a real connection configuration rather than a partial one."""
    return {
        alias: {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(name),
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
        }
    }


class DrillHygieneAssertionsMixin:
    """After any run the scratch alias must be unresolvable again and the
    drill's temp directory gone: the drill is a rehearsal, never a second
    restore waiting to happen."""

    def setUp(self):
        super().setUp()
        self.workdirs_before = self.drill_workdirs()

    def drill_workdirs(self):
        return set(Path(tempfile.gettempdir()).glob("restore-drill-*"))

    def assert_scratch_unregistered(self):
        self.assertNotIn(RESTORE_ALIAS, connections.databases)
        with self.assertRaises(ConnectionDoesNotExist):
            connections[RESTORE_ALIAS]

    def assert_no_drill_workdirs_left(self):
        self.assertEqual(self.drill_workdirs(), self.workdirs_before)


@tag("ops")
class RestoreDrillSafetyTests(DrillHygieneAssertionsMixin, SimpleTestCase):
    """The drill must be structurally incapable of touching real data."""

    @contextmanager
    def alias_configured_as(self, alias, db_path):
        """Run the block with ``alias`` configured to point at ``db_path``.

        The connection handler caches its settings, so ``override_settings``
        alone never reaches it; adding the entry to ``connections.databases``
        is the very object the handler reads, which is how a second
        configured database looks in production.
        """
        entry = _sqlite_settings(alias, db_path)[alias]
        connections.databases[alias] = entry
        try:
            with override_settings(DATABASES={**settings.DATABASES, alias: entry}):
                yield
        finally:
            connections.databases.pop(alias, None)
            try:
                connections[alias].close()
                del connections[alias]
            except ConnectionDoesNotExist:
                pass

    def production_configured_as(self, alias):
        """The production case: the alias' configured file is a real
        deployment database path that this suite never creates."""
        return self.alias_configured_as(alias, PRODUCTION_DB)

    def test_refuses_a_configured_production_database(self):
        """The load-bearing refusal: an alias whose configured file is a real
        database is the production case, and the drill stops before a single
        query -- so the file is never even opened, let alone written."""
        alias = "restore_drill_prod"

        with self.production_configured_as(alias):
            with self.assertRaises(CommandError) as caught:
                call_command("restore_drill", database=alias)

        message = str(caught.exception)
        self.assertIn("refuses to run", message)
        self.assertIn(PRODUCTION_DB, message)
        self.assertIn("never reads a real database", message)
        # nothing was created and no scratch connection was left behind
        self.assertFalse(os.path.exists(PRODUCTION_DB))
        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_refusal_leaves_no_scratch_connection_behind(self):
        """A refused run must not register the scratch alias either, so a
        refusal can never be the start of an accidental restore."""
        with self.production_configured_as("restore_drill_prod2"):
            with self.assertRaises(CommandError):
                call_command("restore_drill", database="restore_drill_prod2")

        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_dry_run_writes_nothing_and_opens_no_connection(self):
        """--dry-run is the operator's "just tell me what it would do" path:
        exit 0, nothing written, nothing read and no restore attempted."""
        out = io.StringIO()

        call_command("restore_drill", "--database", "default", "--dry-run", stdout=out)

        output = out.getvalue()
        self.assertIn("DRY RUN", output)
        self.assertIn("Nothing was read or written", output)
        self.assertNotIn("PASSED", output)
        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_dry_run_is_not_a_bypass_of_the_production_refusal(self):
        """Dry-run is not an override switch: a real database is refused
        with or without the flag, because the refusal is a property of the
        target, not of what the command intends to do."""
        alias = "restore_drill_prod3"

        with self.production_configured_as(alias):
            with self.assertRaises(CommandError):
                call_command("restore_drill", database=alias, dry_run=True)

        self.assertFalse(os.path.exists(PRODUCTION_DB))

    def test_unusable_database_is_a_readable_refusal_not_a_traceback(self):
        """An operator pointing the drill at a database with no schema gets a
        sentence and a non-zero exit, never a stack trace -- and the drill
        still tears down after itself, even though it never got as far as
        registering the scratch connection."""
        workdir = Path(mkdtemp(prefix="schema-less-"))
        self.addCleanup(shutil.rmtree, workdir, True)
        schemaless = workdir / "empty.sqlite3"
        schemaless.write_bytes(b"")
        self.addCleanup(schemaless.unlink, True)
        alias = "restore_drill_schemaless"
        entry = _sqlite_settings(alias, schemaless)[alias]
        connections[alias] = load_backend(entry["ENGINE"]).DatabaseWrapper(entry, alias)

        try:
            with self.assertRaises(CommandError) as caught:
                call_command("restore_drill", database=alias)
        finally:
            connections[alias].close()
            del connections[alias]

        message = str(caught.exception)
        self.assertIn("could not run against connection", message)
        self.assertIn(str(schemaless), message)
        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()


@tag("ops")
class RestoreDrillSafetyRuleTests(SimpleTestCase):
    """The decision behind those refusals, pinned directly so the rule cannot
    be widened without a test noticing. The names below are absolute, so the
    assertions do not depend on where the repository happens to be checked
    out."""

    def test_in_memory_is_safe(self):
        self.assertTrue(is_rehearsal_safe(":memory:", PRODUCTION_DB))

    def test_django_test_database_uri_is_safe(self):
        """The name the test runner actually uses for sqlite."""
        self.assertTrue(
            is_rehearsal_safe(
                "file:memorydb_default?mode=memory&cache=shared", PRODUCTION_DB
            )
        )

    def test_file_inside_the_temp_directory_is_safe(self):
        self.assertTrue(
            is_rehearsal_safe(
                str(Path(tempfile.gettempdir()) / "throwaway.sqlite3"),
                PRODUCTION_DB,
            )
        )

    def test_a_name_other_than_the_configured_one_is_safe(self):
        """A connection pointing at a database this deployment does not use
        is the test database; reading it cannot harm production."""
        self.assertTrue(is_rehearsal_safe(PRODUCTION_DB + "-test", PRODUCTION_DB))

    def test_the_configured_database_is_not_safe(self):
        self.assertFalse(is_rehearsal_safe(PRODUCTION_DB, PRODUCTION_DB))

    def test_an_unset_name_is_not_safe(self):
        """Fail closed: an empty name identifies nothing, so it is refused
        rather than waved through."""
        self.assertFalse(is_rehearsal_safe("", ""))

    def test_a_sibling_of_the_temp_directory_is_not_inside_it(self):
        """The temp containment is the directory itself, not a path that
        merely starts with the same characters."""
        temp_root = Path(tempfile.gettempdir())
        self.assertTrue(_within(str(temp_root / "throwaway.sqlite3"), temp_root))
        self.assertFalse(_within(str(temp_root.with_suffix(".sqlite3")), temp_root))


@tag("ops")
class RestoreDrillRunTests(DrillHygieneAssertionsMixin, ApiTestCase):
    """The rehearsal itself: snapshot -> migrate -> load -> verify, run
    against the throwaway database the test runner created."""

    def run_drill(self, **kwargs):
        """One full drill run against the test database, capturing output."""
        out = io.StringIO()
        call_command("restore_drill", "--database", "default", stdout=out, **kwargs)
        return out.getvalue()

    def test_drill_round_trips_the_test_database_and_cleans_up(self):
        """The whole rehearsal in one pass, because each run really does
        build and tear down a database: a restore that round-trips is
        reported PASSED, the source is untouched, the scratch connection is
        unregistered and the temp directory is gone."""
        self.make_user("drill-buyer")
        source_before = User.objects.count()

        output = self.run_drill()

        self.assertIn("Snapshot:", output)
        self.assertIn("Restore:", output)
        self.assertIn("Restore drill PASSED", output)
        self.assertIn("round-tripped and verified", output)
        # the source is read-only: same rows, same connection, still usable
        self.assertEqual(User.objects.count(), source_before)
        self.assertTrue(User.objects.filter(username="drill-buyer").exists())
        self.assertIn("auth_user", connection.introspection.table_names())
        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_failed_restore_simulation_exits_non_zero(self):
        """The signal an operator needs from CI: a restore that does not
        round-trip is a failure (a non-zero exit), not a warning."""
        with self.assertRaises(CommandError) as caught:
            self.run_drill(simulate_failure=True)

        message = str(caught.exception)
        self.assertIn("Restore verification FAILED", message)
        self.assertIn("did not round-trip", message)
        # the failure names the model whose rows went missing, with both
        # counts, so the operator knows what to look at
        self.assertRegex(message, r"\w+\.\w+: snapshot \d+ -> restored \d+")

    def test_failing_drill_cleans_up_exactly_like_a_passing_one(self):
        """A red CI run must not leave a scratch database or a scratch
        connection behind either -- that is how drills rot."""
        with self.assertRaises(CommandError):
            self.run_drill(simulate_failure=True)

        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_drill_verifies_a_populated_database(self):
        """The verification is a real comparison, not a constant: the fresh
        test database already holds the migration-created rows (permissions,
        content types), so a PASSED line means real rows were dumped and
        reloaded rather than an empty-file no-op."""
        self.make_user("drill-buyer")

        output = self.run_drill()

        snapshot_line = next(
            line for line in output.splitlines() if line.startswith("Snapshot:")
        )
        self.assertNotIn(" 0 objects", snapshot_line)
        self.assertIn("Restore drill PASSED", output)

    def test_drill_works_with_no_fixture_data_at_all(self):
        """A brand-new deployment's empty database is a legitimate thing to
        restore; the drill must pass on it rather than inventing a failure."""
        from orders.models import Order

        Order.objects.all().delete()

        output = self.run_drill()

        self.assertIn("Restore drill PASSED", output)

    def test_drill_removes_its_own_workdir_even_when_it_fails(self):
        """Cleanup runs in a finally, so the restored sqlite file and the
        dump are removed on the failure path too."""
        workdir_before = self.workdirs_before

        with self.assertRaises(CommandError):
            self.run_drill(simulate_failure=True)

        self.assertEqual(self.drill_workdirs(), workdir_before)
        self.assertNotEqual(workdir_before, set())


@tag("ops")
class RestoreDrillWorkdirHygieneTests(SimpleTestCase):
    """The drill deletes what it wrote, and the helper it uses to do that is
    what makes that true for sqlite's journal siblings too."""

    def test_remove_workdir_deletes_every_file_it_contains(self):
        from ops.management.commands.restore_drill import Command

        workdir = Path(mkdtemp(prefix="restore-drill-cleanup-"))
        self.addCleanup(shutil.rmtree, workdir, True)
        for name in ("snapshot.json", "restored.sqlite3", "restored.sqlite3-journal"):
            (workdir / name).write_text("x", encoding="utf-8")

        Command()._remove_workdir(workdir)

        self.assertFalse(workdir.exists())

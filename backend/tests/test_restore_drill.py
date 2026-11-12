import io
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection, connections
from django.db.utils import ConnectionDoesNotExist, load_backend
from django.test import SimpleTestCase, override_settings, tag

from common.testing import ApiTestCase
from ops.management.commands import restore_drill as drill
from ops.management.commands.restore_drill import (
    RESTORE_ALIAS,
    Command,
    _within,
    is_rehearsal_safe,
)

PRODUCTION_DB = os.path.abspath(
    os.sep + os.path.join("srv", "perfume-store", "db.sqlite3")
)


def _sqlite_settings(alias, name):
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


@contextmanager
def without_temp_containment():
    """The runner rewrites a PostgreSQL name to a BARE database name, which the containment check resolves against the working directory; switching that shape off isolates the rule under test."""
    with patch.object(drill, "_within", return_value=False):
        yield


class DrillHygieneAssertionsMixin:
    """Both halves read THIS test's own workdir scope: the shared system temp root is mutable state a concurrent suite also writes into."""

    def setUp(self):
        super().setUp()
        # The prefix deliberately avoids "restore-drill-", so no other process
        # globs this directory as a drill workdir.
        self.workdir_scope = Path(mkdtemp(prefix="drill-hygiene-scope-"))
        self.addCleanup(shutil.rmtree, self.workdir_scope, True)
        real_mkdtemp = tempfile.mkdtemp

        def scoped_mkdtemp(prefix=None, suffix=None, dir=None):
            # Only `restore-drill-*` mkdtemp calls are relocated; every other
            # mkdtemp keeps its default destination.
            if dir is None and (prefix or "").startswith("restore-drill-"):
                dir = str(self.workdir_scope)
            return real_mkdtemp(prefix=prefix, suffix=suffix, dir=dir)

        patcher = patch.object(tempfile, "mkdtemp", side_effect=scoped_mkdtemp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workdirs_before = self.drill_workdirs()

    def drill_workdirs(self):
        return set(Path(self.workdir_scope).glob("restore-drill-*"))

    def assert_scratch_unregistered(self):
        self.assertNotIn(RESTORE_ALIAS, connections.databases)
        with self.assertRaises(ConnectionDoesNotExist):
            connections[RESTORE_ALIAS]

    def assert_no_drill_workdirs_left(self):
        self.assertEqual(self.drill_workdirs(), self.workdirs_before)


@tag("ops")
class RestoreDrillSafetyTests(DrillHygieneAssertionsMixin, SimpleTestCase):
    @contextmanager
    def alias_configured_as(self, alias, db_path):
        """The connection handler caches its settings, so override_settings alone never reaches it; adding to connections.databases is the object it reads."""
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
        return self.alias_configured_as(alias, PRODUCTION_DB)

    def test_refuses_a_configured_production_database(self):
        alias = "restore_drill_prod"

        with self.production_configured_as(alias):
            with self.assertRaises(CommandError) as caught:
                call_command("restore_drill", database=alias)

        message = str(caught.exception)
        self.assertIn("refuses to run", message)
        self.assertIn(PRODUCTION_DB, message)
        self.assertIn("never reads a real database", message)
        self.assertFalse(os.path.exists(PRODUCTION_DB))
        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_refusal_leaves_no_scratch_connection_behind(self):
        with self.production_configured_as("restore_drill_prod2"):
            with self.assertRaises(CommandError):
                call_command("restore_drill", database="restore_drill_prod2")

        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_dry_run_writes_nothing_and_opens_no_connection(self):
        out = io.StringIO()

        call_command("restore_drill", "--database", "default", "--dry-run", stdout=out)

        output = out.getvalue()
        self.assertIn("DRY RUN", output)
        self.assertIn("Nothing was read or written", output)
        self.assertNotIn("PASSED", output)
        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_dry_run_is_not_a_bypass_of_the_production_refusal(self):
        alias = "restore_drill_prod3"

        with self.production_configured_as(alias):
            with self.assertRaises(CommandError):
                call_command("restore_drill", database=alias, dry_run=True)

        self.assertFalse(os.path.exists(PRODUCTION_DB))

    def test_unusable_database_is_a_readable_refusal_not_a_traceback(self):
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

    def test_the_derived_test_database_is_accepted(self):
        alias = "restore_drill_derived"
        captured = {alias: {"NAME": PRODUCTION_DB, "TEST_NAME": ""}}
        out = io.StringIO()

        with (
            patch.dict(drill.CONFIGURED_AT_IMPORT, captured, clear=False),
            without_temp_containment(),
            self.alias_configured_as(alias, f"test_{PRODUCTION_DB}"),
        ):
            call_command("restore_drill", database=alias, dry_run=True, stdout=out)

        self.assertIn("DRY RUN", out.getvalue())
        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_an_uncaptured_alias_is_still_refused(self):
        alias = "restore_drill_uncaptured"

        with (
            without_temp_containment(),
            self.alias_configured_as(alias, f"test_{PRODUCTION_DB}"),
        ):
            with self.assertRaises(CommandError) as caught:
                call_command("restore_drill", database=alias, dry_run=True)

        self.assertIn("refuses to run", str(caught.exception))
        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()


@tag("ops")
class RestoreDrillSafetyRuleTests(SimpleTestCase):
    def test_in_memory_is_safe(self):
        self.assertTrue(is_rehearsal_safe(":memory:", PRODUCTION_DB))

    def test_django_test_database_uri_is_safe(self):
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
        self.assertTrue(is_rehearsal_safe(PRODUCTION_DB + "-test", PRODUCTION_DB))

    def test_the_test_database_the_runner_derived_is_safe(self):
        with without_temp_containment():
            self.assertTrue(
                is_rehearsal_safe(
                    f"test_{PRODUCTION_DB}", f"test_{PRODUCTION_DB}", PRODUCTION_DB
                )
            )

    def test_the_configured_test_database_name_is_safe(self):
        named = f"{PRODUCTION_DB}-drill"
        with without_temp_containment():
            self.assertTrue(is_rehearsal_safe(named, named, PRODUCTION_DB, named))

    def test_the_configured_database_is_not_safe_even_with_a_capture(self):
        self.assertFalse(is_rehearsal_safe(PRODUCTION_DB, PRODUCTION_DB, PRODUCTION_DB))

    def test_no_capture_fails_closed(self):
        with without_temp_containment():
            self.assertFalse(
                is_rehearsal_safe(f"test_{PRODUCTION_DB}", f"test_{PRODUCTION_DB}")
            )

    def test_the_derived_shape_is_a_whole_name_not_a_prefix(self):
        longer = f"test_{PRODUCTION_DB}-backup"
        with without_temp_containment():
            self.assertFalse(is_rehearsal_safe(longer, longer, PRODUCTION_DB))

    def test_the_configured_database_is_not_safe(self):
        self.assertFalse(is_rehearsal_safe(PRODUCTION_DB, PRODUCTION_DB))

    def test_an_unset_name_is_not_safe(self):
        self.assertFalse(is_rehearsal_safe("", ""))

    def test_a_sibling_of_the_temp_directory_is_not_inside_it(self):
        temp_root = Path(tempfile.gettempdir())
        self.assertTrue(_within(str(temp_root / "throwaway.sqlite3"), temp_root))
        self.assertFalse(_within(str(temp_root.with_suffix(".sqlite3")), temp_root))


@tag("ops")
class RestoreDrillRunTests(DrillHygieneAssertionsMixin, ApiTestCase):
    def run_drill(self, **kwargs):
        out = io.StringIO()
        call_command("restore_drill", "--database", "default", stdout=out, **kwargs)
        return out.getvalue()

    def test_drill_round_trips_the_test_database_and_cleans_up(self):
        self.make_user("drill-buyer")
        source_before = User.objects.count()

        output = self.run_drill()

        self.assertIn("Snapshot:", output)
        self.assertIn("Restore:", output)
        self.assertIn("Restore drill PASSED", output)
        self.assertIn("round-tripped and verified", output)
        self.assertEqual(User.objects.count(), source_before)
        self.assertTrue(User.objects.filter(username="drill-buyer").exists())
        self.assertIn("auth_user", connection.introspection.table_names())
        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_failed_restore_simulation_exits_non_zero(self):
        with self.assertRaises(CommandError) as caught:
            self.run_drill(simulate_failure=True)

        message = str(caught.exception)
        self.assertIn("Restore verification FAILED", message)
        self.assertIn("did not round-trip", message)
        self.assertRegex(message, r"\w+\.\w+: snapshot \d+ -> restored \d+")

    def test_failing_drill_cleans_up_exactly_like_a_passing_one(self):
        with self.assertRaises(CommandError):
            self.run_drill(simulate_failure=True)

        self.assert_scratch_unregistered()
        self.assert_no_drill_workdirs_left()

    def test_drill_verifies_a_populated_database(self):
        self.make_user("drill-buyer")

        output = self.run_drill()

        snapshot_line = next(
            line for line in output.splitlines() if line.startswith("Snapshot:")
        )
        self.assertNotIn(" 0 objects", snapshot_line)
        self.assertIn("Restore drill PASSED", output)

    def test_drill_works_with_no_fixture_data_at_all(self):
        from orders.models import Order

        Order.objects.all().delete()

        output = self.run_drill()

        self.assertIn("Restore drill PASSED", output)

    def test_drill_removes_its_own_workdir_even_when_it_fails(self):
        seen = {}
        original = Command._remove_workdir

        def spy(command, workdir):
            path = Path(workdir)
            seen["path"] = path
            seen["existed"] = path.is_dir()
            seen["contents"] = sorted(entry.name for entry in path.glob("*"))
            return original(command, workdir)

        with patch.object(Command, "_remove_workdir", spy):
            with self.assertRaises(CommandError):
                self.run_drill(simulate_failure=True)

        self.assertIn("path", seen, "the failed run never tore its workdir down")
        self.assertTrue(seen["existed"], "the workdir was already gone at teardown")
        self.assertIn("snapshot.json", seen["contents"])
        self.assertIn("restored.sqlite3", seen["contents"])
        self.assertFalse(
            seen["path"].exists(),
            f"a failed drill left its workdir behind: {seen['path']}",
        )


@tag("ops")
class RestoreDrillWorkdirHygieneTests(SimpleTestCase):
    def test_remove_workdir_deletes_every_file_it_contains(self):
        workdir = Path(mkdtemp(prefix="restore-drill-cleanup-"))
        self.addCleanup(shutil.rmtree, workdir, True)
        for name in ("snapshot.json", "restored.sqlite3", "restored.sqlite3-journal"):
            (workdir / name).write_text("x", encoding="utf-8")

        Command()._remove_workdir(workdir)

        self.assertFalse(workdir.exists())

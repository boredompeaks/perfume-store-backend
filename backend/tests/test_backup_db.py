import io
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, tag

from ops.management.commands import backup_db as backup_module
from ops.management.commands.backup_db import (
    DEFAULT_KEEP,
    PREFIX,
    SIGNATURES,
    backup_name,
    is_own_dump,
    pg_dump_binary,
    prune,
    redact,
    scrub,
    utc_stamp,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# A deliberately self-describing, low-entropy probe credential, assembled into the URL at runtime: no password-shaped literal is committed and no complete DSN is written out.
PROBE_PASSWORD = "probe-fixture-only"
PROBE_USER = "probe-user"
PROBE_HOST = "db.internal"
PROBE_DB = "probe_store"
PROBE_URL = (
    f"postgres://{PROBE_USER}:{PROBE_PASSWORD}@{PROBE_HOST}:5432/{PROBE_DB}"
    "?sslmode=require"
)
DUMP_BODY = b"PGDMP-fake-dump"

UNSET = object()

POSTGRES_ENTRY = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": PROBE_DB,
    "USER": PROBE_USER,
    "PASSWORD": PROBE_PASSWORD,
    "HOST": PROBE_HOST,
    "PORT": "5432",
    "OPTIONS": {"sslmode": "require"},
}


def sqlite_entry(name):
    return {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(name),
        "USER": "",
        "PASSWORD": "",
        "HOST": "",
        "PORT": "",
        "OPTIONS": {},
    }


class BackupTestCase(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.backup_dir = Path(mkdtemp(prefix="backup-db-test-"))
        self.addCleanup(self._remove_tree, self.backup_dir)
        self.pg_calls = []
        env = patch.dict(os.environ, {"DATABASE_URL": PROBE_URL})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def _remove_tree(root):
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        root.rmdir()

    @contextmanager
    def using_databases(self, **aliases):
        with patch.dict(backup_module.settings.DATABASES, aliases, clear=True):
            yield

    @contextmanager
    def pg_dump(self, returncode=0, stderr=b"", binary="/usr/bin/pg_dump"):
        calls = self.pg_calls

        def fake_run(argv, stdout=None, stderr_=None, env=None, check=False, **kwargs):
            calls.append({"argv": argv, "env": env, "check": check, "stdout": stdout})
            stdout.write(DUMP_BODY)
            return type("Completed", (), {"returncode": returncode, "stderr": stderr})()

        with (
            patch.object(backup_module.subprocess, "run", fake_run),
            patch.object(backup_module, "pg_dump_binary", return_value=binary),
        ):
            yield

    def run_command(self, **kwargs):
        if "output_dir" not in kwargs:
            kwargs["output_dir"] = str(self.backup_dir)
        elif kwargs["output_dir"] is UNSET:
            kwargs.pop("output_dir")
        out = io.StringIO()
        call_command("backup_db", stdout=out, **kwargs)
        return out.getvalue()

    def dumps(self):
        return sorted(
            path.name
            for path in self.backup_dir.iterdir()
            if path.is_file()
            and path.name.startswith(f"{PREFIX}-")
            and path.suffix in (".dump", ".sqlite3")
        )

    def write_dump(self, stamp, suffix=".dump", content=None):
        path = self.backup_dir / backup_name(stamp, suffix)
        path.write_bytes(SIGNATURES[suffix] if content is None else content)
        return path

    def write_foreign(self, name, content=b"an operator's own file"):
        path = self.backup_dir / name
        path.write_bytes(content)
        return path

    def make_database(self, name="live.sqlite3"):
        source = self.backup_dir / name
        # closing(), not `with`: sqlite3's context manager is transactional and leaves the connection (and the file handle) open.
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("CREATE TABLE scent (id integer primary key, name text)")
            connection.execute("INSERT INTO scent (name) VALUES ('oud')")
            connection.execute("INSERT INTO scent (name) VALUES ('rose')")
            connection.commit()
        return source


@tag("ops")
class PostgresBackupTests(BackupTestCase):
    def test_a_successful_dump_lands_a_file_and_reports_it(self):
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            output = self.run_command()

        self.assertIn("Backup written", output)
        self.assertIn(f"{len(DUMP_BODY)} bytes", output)
        self.assertIn("postgres connection 'default'", output)
        self.assertEqual(len(self.dumps()), 1)
        written = self.backup_dir / self.dumps()[0]
        self.assertEqual(written.suffix, ".dump")
        self.assertEqual(written.read_bytes(), DUMP_BODY)

    def test_the_dump_goes_straight_to_a_file_not_through_the_command_output(self):
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            output = self.run_command()

        self.assertNotIn(DUMP_BODY.decode(), output)
        self.assertTrue(self.pg_calls[-1]["stdout"].name.endswith(".dump.partial"))

    def test_a_successful_run_leaves_no_partial_file_behind(self):
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            self.run_command()

        self.assertEqual(
            [name for name in self.dumps() if name.endswith(".partial")], []
        )

    def test_the_command_output_never_contains_the_url_or_the_password(self):
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            output = self.run_command()

        for secret in (PROBE_URL, PROBE_PASSWORD, PROBE_USER):
            self.assertNotIn(secret, output)

    def test_pg_dump_argv_carries_no_connection_identity(self):
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            self.run_command()

        argv = " ".join(self.pg_calls[-1]["argv"])
        for secret in (PROBE_PASSWORD, PROBE_USER, PROBE_HOST, PROBE_DB):
            self.assertNotIn(secret, argv)
        self.assertIn("--no-password", argv)

    def test_the_connection_reaches_pg_dump_through_the_environment(self):
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            self.run_command()

        env = self.pg_calls[-1]["env"]
        self.assertEqual(env["PGPASSWORD"], PROBE_PASSWORD)
        self.assertEqual(env["PGUSER"], PROBE_USER)
        self.assertEqual(env["PGHOST"], PROBE_HOST)
        self.assertEqual(env["PGPORT"], "5432")
        self.assertEqual(env["PGDATABASE"], PROBE_DB)
        self.assertEqual(env["PGSSLMODE"], "require")

    def test_a_failed_dumps_error_text_cannot_leak_the_password(self):
        with (
            self.using_databases(default=POSTGRES_ENTRY),
            self.pg_dump(
                returncode=1,
                stderr=f"pg_dump: error: connection to {PROBE_URL} failed".encode(),
            ),
        ):
            with self.assertRaises(CommandError) as caught:
                self.run_command()

        message = str(caught.exception)
        self.assertNotIn(PROBE_PASSWORD, message)
        self.assertNotIn(PROBE_URL, message)
        self.assertIn("***redacted***", message)
        self.assertIn("pg_dump exited 1", message)

    def test_a_failed_dumps_error_text_cannot_leak_the_connection_identity(self):
        stderr = (
            f'pg_dump: error: connection to server at "{PROBE_HOST}" '
            f"(10.42.0.7), port 5432 failed: FATAL:  password authentication "
            f'failed for user "{PROBE_USER}"\n'
            f"\tConnection parameters: host={PROBE_HOST} port=5432 "
            f"user={PROBE_USER} dbname={PROBE_DB} sslmode=require\n"
        ).encode()

        with (
            self.using_databases(default=POSTGRES_ENTRY),
            self.pg_dump(returncode=1, stderr=stderr),
        ):
            with self.assertRaises(CommandError) as caught:
                self.run_command()

        message = str(caught.exception)
        for identity in (
            PROBE_URL,
            PROBE_PASSWORD,
            PROBE_USER,
            PROBE_HOST,
            PROBE_DB,
            "5432",
            "10.42.0.7",
        ):
            with self.subTest(identity=identity):
                self.assertNotIn(identity, message)
        self.assertIn("pg_dump exited 1", message)
        self.assertIn("password authentication failed", message)
        self.assertIn("***redacted***", message)

    def test_a_failure_with_no_diagnostic_output_still_says_something(self):
        with (
            self.using_databases(default=POSTGRES_ENTRY),
            self.pg_dump(returncode=2, stderr=b"   "),
        ):
            with self.assertRaises(CommandError) as caught:
                self.run_command()

        message = str(caught.exception)
        self.assertIn("no diagnostic output", message)
        self.assertNotIn(PROBE_PASSWORD, message)

    def test_a_failed_dump_exits_non_zero_and_keeps_no_dump(self):
        with (
            self.using_databases(default=POSTGRES_ENTRY),
            self.pg_dump(returncode=1, stderr=b"pg_dump: error: boom"),
        ):
            with self.assertRaises(CommandError) as caught:
                self.run_command()

        message = str(caught.exception)
        self.assertIn("backup_db FAILED for connection 'default'", message)
        self.assertIn("no dump was kept", message)
        self.assertEqual(self.dumps(), [])

    def test_a_missing_pg_dump_binary_is_a_named_refusal_not_a_traceback(self):
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump(binary=None):
            with self.assertRaises(CommandError) as caught:
                self.run_command()

        message = str(caught.exception)
        self.assertIn("pg_dump is not on PATH", message)
        self.assertIn("--prune-only", message)

    def test_an_unwritable_backup_directory_is_a_readable_refusal(self):
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            with patch.object(
                backup_module.subprocess,
                "run",
                side_effect=PermissionError(13, "Permission denied"),
            ):
                with self.assertRaises(CommandError) as caught:
                    self.run_command()

        message = str(caught.exception)
        self.assertIn("could not write the dump", message)
        self.assertIn("Permission denied", message)


@tag("ops")
class SqliteBackupTests(BackupTestCase):
    def test_a_sqlite_backup_copies_the_rows_and_leaves_the_source_untouched(self):
        source = self.make_database()
        before = source.read_bytes()

        with self.using_databases(default=sqlite_entry(source)):
            output = self.run_command()

        self.assertIn("Backup complete", output)
        dump = self.backup_dir / self.dumps()[0]
        with closing(sqlite3.connect(dump)) as restored:
            rows = restored.execute("SELECT name FROM scent ORDER BY id").fetchall()
        self.assertEqual(rows, [("oud",), ("rose",)])
        self.assertEqual(source.read_bytes(), before)

    def test_a_sqlite_backup_leaves_no_partial_file_behind(self):
        with self.using_databases(default=sqlite_entry(self.make_database())):
            self.run_command()

        self.assertEqual(
            [name for name in self.dumps() if name.endswith(".partial")], []
        )

    def test_two_backups_in_the_same_second_do_not_overwrite_each_other(self):
        source = self.make_database()

        with self.using_databases(default=sqlite_entry(source)):
            self.run_command()
            self.run_command()

        self.assertEqual(len(self.dumps()), 2)

    def test_it_refuses_to_write_the_backup_over_an_existing_file(self):
        stamp = "20260101T000000Z"
        source = self.make_database(backup_name(stamp, ".sqlite3"))
        before = source.read_bytes()

        with (
            self.using_databases(default=sqlite_entry(source)),
            patch.object(backup_module, "utc_stamp", return_value=stamp),
        ):
            self.run_command()

        self.assertEqual(
            self.dumps(),
            [
                f"{PREFIX}-{stamp}-1.sqlite3",
                backup_name(stamp, ".sqlite3"),
            ],
        )
        self.assertEqual(source.read_bytes(), before)

    def test_a_corrupt_database_file_fails_loudly_and_leaves_no_partial(self):
        corrupt = self.backup_dir / "corrupt.sqlite3"
        corrupt.write_bytes(b"this is not a database, whatever the name says")

        with self.using_databases(default=sqlite_entry(corrupt)):
            with self.assertRaises(CommandError) as caught:
                self.run_command()

        self.assertIn(
            "backup_db FAILED for connection 'default'", str(caught.exception)
        )
        self.assertEqual(self.dumps(), [])
        self.assertEqual([path.name for path in self.backup_dir.glob("*.partial")], [])

    def test_an_in_memory_database_is_refused_by_name(self):
        for name in (":memory:", "file:memorydb_default?mode=memory&cache=shared"):
            with self.subTest(name=name):
                with self.using_databases(
                    default={"ENGINE": "django.db.backends.sqlite3", "NAME": name}
                ):
                    with self.assertRaises(CommandError) as caught:
                        self.run_command()
                self.assertIn("in-memory", str(caught.exception))

    def test_a_missing_database_file_is_refused_by_name(self):
        with self.using_databases(default=sqlite_entry(self.backup_dir / "not-there")):
            with self.assertRaises(CommandError) as caught:
                self.run_command()

        self.assertIn("is not a file", str(caught.exception))


@tag("ops")
class RetentionTests(BackupTestCase):
    def test_prune_keeps_the_newest_n_and_removes_the_older_ones(self):
        for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
            self.write_dump(stamp)

        removed = prune(self.backup_dir, 2)

        self.assertEqual(removed, [f"{PREFIX}-20260101T000000Z.dump"])
        self.assertEqual(
            self.dumps(),
            [
                f"{PREFIX}-20260102T000000Z.dump",
                f"{PREFIX}-20260103T000000Z.dump",
            ],
        )

    def test_prune_counts_sqlite_dumps_in_the_same_retention_pool(self):
        self.write_dump("20260101T000000Z", suffix=".dump")
        self.write_dump("20260102T000000Z", suffix=".sqlite3")

        prune(self.backup_dir, 1)

        self.assertEqual(self.dumps(), [f"{PREFIX}-20260102T000000Z.sqlite3"])

    def test_prune_never_touches_a_file_it_did_not_create(self):
        keeper = self.write_foreign("notes.txt", b"an operator's own file")
        borrowed = self.write_foreign("someone-elses-backup.dump", SIGNATURES[".dump"])
        unstamped = self.write_foreign(f"{PREFIX}-latest.dump", SIGNATURES[".dump"])
        wrong_width = self.write_foreign(f"{PREFIX}-20260101T000000ZZ.dump", b"PGDMP")
        ours = self.write_dump("20260101T000000Z")

        removed = prune(self.backup_dir, 1)

        self.assertEqual(removed, [])
        for path in (keeper, borrowed, unstamped, wrong_width, ours):
            with self.subTest(file=path.name):
                self.assertTrue(path.is_file())

    def test_prune_never_deletes_a_hand_taken_dump_that_borrowed_the_scheme(self):
        hand_taken = self.write_foreign(
            f"{PREFIX}-20190101T000000Z.dump", b"-- PostgreSQL database dump\n"
        )
        self.write_dump("20260101T000000Z")
        self.write_dump("20260102T000000Z")

        removed = prune(self.backup_dir, 1)

        self.assertEqual(removed, [f"{PREFIX}-20260101T000000Z.dump"])
        self.assertTrue(hand_taken.is_file())
        self.assertEqual(
            self.dumps(),
            [
                f"{PREFIX}-20190101T000000Z.dump",
                f"{PREFIX}-20260102T000000Z.dump",
            ],
        )

    def test_prune_never_deletes_a_file_that_only_borrowed_the_name(self):
        impostor = self.write_foreign(
            f"{PREFIX}-20251231T235959Z.dump", b"a note to self"
        )
        self.write_dump("20260101T000000Z")
        self.write_dump("20260102T000000Z")

        removed = prune(self.backup_dir, 1)

        self.assertEqual(removed, [f"{PREFIX}-20260101T000000Z.dump"])
        self.assertTrue(impostor.is_file())

    def test_a_stamp_that_is_not_a_date_is_not_our_scheme(self):
        for name in (
            f"{PREFIX}-9999999T00000Z.dump",
            f"{PREFIX}-20260101T000000ZZ.dump",
            f"{PREFIX}-20260101T0000.dump",
            f"{PREFIX}-latest.dump",
        ):
            with self.subTest(name=name):
                self.write_foreign(name, SIGNATURES[".dump"])
        self.write_dump("20260102T000000Z")

        self.assertEqual(prune(self.backup_dir, 1), [])
        self.assertEqual(
            sorted(path.name for path in self.backup_dir.iterdir()),
            sorted(
                [
                    f"{PREFIX}-9999999T00000Z.dump",
                    f"{PREFIX}-20260101T000000ZZ.dump",
                    f"{PREFIX}-20260101T0000.dump",
                    f"{PREFIX}-latest.dump",
                    f"{PREFIX}-20260102T000000Z.dump",
                ]
            ),
        )

    def test_a_directory_wearing_a_dumps_name_is_never_unlinked(self):
        impostor = self.backup_dir / backup_name("20251231T235959Z", ".dump")
        impostor.mkdir()
        self.write_dump("20260101T000000Z")
        self.write_dump("20260102T000000Z")

        self.assertEqual(prune(self.backup_dir, 1), [f"{PREFIX}-20260101T000000Z.dump"])
        self.assertTrue(impostor.is_dir())

    def test_ownership_is_decided_by_the_engine_signature_not_the_name(self):
        self.assertTrue(is_own_dump(self.write_dump("20260101T000000Z")))
        self.assertTrue(is_own_dump(self.write_dump("20260101T000000Z", ".sqlite3")))
        disambiguated = self.backup_dir / f"{PREFIX}-20260101T000000Z-1.dump"
        disambiguated.write_bytes(SIGNATURES[".dump"])
        self.assertTrue(is_own_dump(disambiguated))
        self.assertFalse(
            is_own_dump(self.write_foreign(f"{PREFIX}-20260101T000000Z.gz", b"PGDMP"))
        )
        self.assertFalse(
            is_own_dump(self.write_foreign(f"{PREFIX}-20260101T000000Z.dump", b"x"))
        )

    def test_the_command_prunes_to_the_keep_count_after_a_dump(self):
        for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
            self.write_dump(stamp)

        with (
            self.using_databases(default=POSTGRES_ENTRY),
            self.pg_dump(),
            patch.object(backup_module, "utc_stamp", return_value="20260104T000000Z"),
        ):
            output = self.run_command(keep=2)

        self.assertIn("Pruned 2 older dump(s)", output)
        self.assertEqual(
            self.dumps(),
            [
                f"{PREFIX}-20260103T000000Z.dump",
                f"{PREFIX}-20260104T000000Z.dump",
            ],
        )

    def test_prune_only_needs_no_pg_dump_binary(self):
        for stamp in ("20260101T000000Z", "20260102T000000Z"):
            self.write_dump(stamp)

        output = self.run_command(prune_only=True, keep=1)

        self.assertIn("keeping the newest 1", output)
        self.assertEqual(self.dumps(), [f"{PREFIX}-20260102T000000Z.dump"])

    def test_prune_only_refuses_a_directory_that_is_not_there(self):
        with self.assertRaises(CommandError) as caught:
            self.run_command(
                output_dir=str(self.backup_dir / "never-created"), prune_only=True
            )

        self.assertIn("nothing to prune", str(caught.exception))

    def test_a_retention_count_below_one_is_refused_before_anything_is_deleted(self):
        self.write_dump("20260101T000000Z")

        with self.assertRaises(CommandError) as caught:
            self.run_command(prune_only=True, keep=0)

        self.assertIn("refuses a retention count of 0", str(caught.exception))
        self.assertEqual(len(self.dumps()), 1)

    def test_the_retention_count_comes_from_the_environment(self):
        for stamp in ("20260101T000000Z", "20260102T000000Z"):
            self.write_dump(stamp)

        with patch.dict(os.environ, {"BACKUP_RETENTION": "1"}):
            self.run_command(prune_only=True)

        self.assertEqual(self.dumps(), [f"{PREFIX}-20260102T000000Z.dump"])

    def test_the_default_retention_count_is_documented(self):
        self.assertEqual(DEFAULT_KEEP, 7)


@tag("ops")
class RefusalTests(BackupTestCase):
    def run_without_backup_dir_env(self, **kwargs):
        environment = {
            key: value for key, value in os.environ.items() if key != "BACKUP_DIR"
        }
        with patch.dict(os.environ, environment, clear=True):
            return self.run_command(output_dir=UNSET, **kwargs)

    def test_it_refuses_to_run_without_a_backup_directory(self):
        with self.using_databases(default=POSTGRES_ENTRY):
            with self.assertRaises(CommandError) as caught:
                self.run_without_backup_dir_env()

        message = str(caught.exception)
        self.assertIn("refuses to run without a backup directory", message)
        self.assertIn("BACKUP_DIR", message)

    def test_a_dry_run_is_not_a_bypass_of_the_directory_refusal(self):
        with self.using_databases(default=POSTGRES_ENTRY):
            with self.assertRaises(CommandError):
                self.run_without_backup_dir_env(dry_run=True)

    def test_a_backup_path_that_is_a_file_is_refused(self):
        blocker = self.backup_dir / "not-a-directory"
        blocker.write_text("in the way", encoding="utf-8")

        with self.assertRaises(CommandError) as caught:
            self.run_command(output_dir=str(blocker))

        self.assertIn("is not a directory", str(caught.exception))

    def test_an_unknown_connection_alias_is_refused(self):
        with self.using_databases(default=POSTGRES_ENTRY):
            with self.assertRaises(CommandError) as caught:
                self.run_command(database="nope")

        message = str(caught.exception)
        self.assertIn("unknown connection alias", message)
        self.assertIn("default", message)

    def test_an_unsupported_engine_is_refused(self):
        with self.using_databases(
            default={"ENGINE": "django.db.backends.mysql", "NAME": "shop"}
        ):
            with self.assertRaises(CommandError) as caught:
                self.run_command()

        self.assertIn("unsupported database engine", str(caught.exception))

    def test_a_dry_run_writes_nothing_and_says_so(self):
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            output = self.run_command(dry_run=True)

        self.assertIn("DRY RUN", output)
        self.assertIn("Nothing was read or written", output)
        self.assertEqual(self.dumps(), [])
        self.assertEqual(self.pg_calls, [])

    def test_a_dry_run_reports_the_configured_retention_policy(self):
        with self.using_databases(default=POSTGRES_ENTRY):
            with patch.dict(os.environ, {"BACKUP_RETENTION": "3"}):
                output = self.run_command(dry_run=True)

        self.assertIn("keeping the newest 3 dump(s)", output)

    def test_a_dry_run_reports_a_prune_only_plan(self):
        with self.using_databases(default=POSTGRES_ENTRY):
            output = self.run_command(dry_run=True, prune_only=True)

        self.assertIn("no dump would be taken", output)


@tag("ops")
class BackupScheduleArtifactTests(SimpleTestCase):
    SCRIPT = "scripts/backup.sh"
    RUNBOOK = "backend/docs/deploy-runbook.md"
    WORKFLOW = ".github/workflows/backend-tests.yml"
    SECRET_NAME = re.compile(r"(?i)(secret|password|passwd|token|ssh_key|api_key)")

    def read(self, relative_path):
        path = REPO_ROOT / relative_path
        self.assertTrue(path.is_file(), f"{relative_path} is missing")
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")

    def test_the_backup_script_is_committed_with_a_usable_shebang(self):
        # A BOM or a leading blank line breaks `bash scripts/backup.sh` with an
        # error that points at the wrong file entirely.
        raw = (REPO_ROOT / self.SCRIPT).read_bytes()
        self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\n"))

    def test_the_backup_script_is_fail_closed(self):
        text = self.read(self.SCRIPT)
        self.assertIn("set -euo pipefail", text)
        self.assertIn("[ -f .env ] || fail", text)
        self.assertIn("BACKUP_DIR=${BACKUP_DIR:-}", text)
        self.assertIn('[ -n "$BACKUP_DIR" ] || fail', text)

    def test_the_backup_script_refuses_a_relative_backup_directory(self):
        text = self.read(self.SCRIPT)
        self.assertIn('case "$BACKUP_DIR" in', text)
        self.assertIn("must be an absolute host path", text)

    def test_the_backup_script_binds_the_dump_directory_into_the_container(self):
        text = self.read(self.SCRIPT)
        self.assertIn('-v "$BACKUP_DIR:$CONTAINER_BACKUP_DIR"', text)
        self.assertIn('-e BACKUP_DIR="$CONTAINER_BACKUP_DIR"', text)

    def test_the_backup_script_runs_the_backup_command_on_the_released_image(self):
        text = self.read(self.SCRIPT)
        self.assertIn("manage backup_db", text)
        self.assertIn('"$SERVICE" python manage.py', text)

    def test_the_backup_script_never_echoes_the_environment(self):
        text = self.read(self.SCRIPT)
        for leak in ("set -x", "printenv", "env\n", "echo $", "cat .env"):
            self.assertNotIn(leak, text)

    def test_the_backup_script_carries_no_literal_credential(self):
        for line in self.read(self.SCRIPT).splitlines():
            if line.strip().startswith("#"):
                continue
            assignment = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if assignment and self.SECRET_NAME.search(assignment.group(1)):
                self.fail(
                    f"{self.SCRIPT} assigns a literal to a credential-shaped "
                    f"name: {line.strip()}"
                )

    def test_the_backup_script_gates_the_backend_suite_on_both_triggers(self):
        workflow = self.read(self.WORKFLOW)
        triggers = workflow.split("  push:", 1)[1].split("  pull_request:", 1)
        self.assertEqual(len(triggers), 2, "the workflow lost a trigger block")
        for block in triggers:
            with self.subTest(trigger="push/pull_request"):
                self.assertIn(f'"{self.SCRIPT}"', block)

    def test_the_cadence_is_documented_and_the_documented_prerequisite_is_true(self):
        runbook = self.read(self.RUNBOOK)
        self.assertIn("scripts/backup.sh", runbook)
        self.assertIn("crontab", runbook)
        self.assertIn("pg_dump", runbook)
        self.assertRegex(runbook, r"(?i)postgresql[- ]client")


@tag("ops")
class BackupScriptExecutionTests(SimpleTestCase):
    # On Windows, PATH's `bash` is usually the WSL launcher, which hangs with no
    # distribution installed; Git-for-Windows ships a real bash and is preferred.
    GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
    SCRIPT = "scripts/backup.sh"
    DOCKER_STUB = (
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$DOCKER_LOG"\nexit 0\n'
    )

    @classmethod
    def bash_executable(cls):
        if os.name == "nt" and cls.GIT_BASH.is_file():
            return str(cls.GIT_BASH)
        return shutil.which("bash")

    def run_the_documented_invocation(self, extra_env=None):
        bash = self.bash_executable()
        if not bash:
            self.skipTest("no bash on this host, so the script cannot be run")

        root = Path(mkdtemp(prefix="backup-script-test-"))
        self.addCleanup(self._remove_tree, root)
        (root / "scripts").mkdir()
        (root / self.SCRIPT).write_bytes((REPO_ROOT / self.SCRIPT).read_bytes())
        (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (root / ".env").write_text("DJANGO_SECRET_KEY=dummy\n", encoding="utf-8")
        stub_dir = root / "stub-bin"
        stub_dir.mkdir()
        (stub_dir / "docker").write_text(
            self.DOCKER_STUB, encoding="utf-8", newline="\n"
        )
        os.chmod(stub_dir / "docker", 0o755)
        backups = root / "backups"
        log = root / "docker-calls.log"

        environment = {
            "PATH": os.pathsep.join([str(stub_dir), os.environ.get("PATH", "")]),
            "BACKUP_DIR": (
                str(backups) if os.name != "nt" else f"/tmp/{root.name}/backups"
            ),
            "DOCKER_LOG": str(log),
        }
        if os.name == "nt":
            environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            environment["TEMP"] = os.environ.get("TEMP", "")
        environment.pop("BACKUP_RETENTION", None)
        environment.update(extra_env or {})

        completed = subprocess.run(
            [bash, "./" + self.SCRIPT],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        return completed, calls

    @staticmethod
    def _remove_tree(root):
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        root.rmdir()

    def test_the_documented_invocation_reaches_the_dump_with_no_unset_knob(self):
        completed, calls = self.run_the_documented_invocation()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("unbound variable", completed.stderr)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("compose -f docker-compose.yml run --rm -T", calls[0])
        self.assertIn("backup_db", calls[0])

    def test_the_documented_invocation_hands_the_default_retention_to_the_command(self):
        completed, calls = self.run_the_documented_invocation()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("-e BACKUP_RETENTION=7", calls[0])
        self.assertIn("keeping 7 dumps", completed.stdout)

    def test_an_operator_retention_knob_is_forwarded_rather_than_ignored(self):
        completed, calls = self.run_the_documented_invocation(
            {"BACKUP_RETENTION": "21"}
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("-e BACKUP_RETENTION=21", calls[0])
        self.assertIn("keeping 21 dumps", completed.stdout)

    def test_the_script_still_refuses_what_it_always_refused(self):
        completed, _ = self.run_the_documented_invocation(
            {"BACKUP_DIR": "relative/path"}
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("must be an absolute host path", completed.stderr)


@tag("ops")
class HelperTests(SimpleTestCase):
    def test_redact_masks_every_secret_and_leaves_the_rest(self):
        masked = redact(
            f"url={PROBE_URL} pass={PROBE_PASSWORD} ok", [PROBE_URL, PROBE_PASSWORD]
        )

        self.assertNotIn(PROBE_PASSWORD, masked)
        self.assertNotIn(PROBE_URL, masked)
        self.assertIn("ok", masked)

    def test_redact_ignores_empty_secrets(self):
        self.assertEqual(redact("nothing to hide", ["", None]), "nothing to hide")

    def test_scrub_masks_the_connection_shape_third_party_text_adds(self):
        masked = scrub(
            'pg_dump: error: connection to server at "db.internal" (10.42.0.7), '
            "port 5432 failed\n\tConnection parameters: host=db.internal "
            "user=app dbname=store\npg_dump: detail: sorry",
            ["db.internal", "5432"],
        )

        self.assertNotIn("10.42.0.7", masked)
        self.assertIn("Connection parameters: ***redacted***", masked)
        self.assertNotIn("db.internal", masked)
        self.assertNotIn("5432", masked)
        self.assertIn("pg_dump: detail: sorry", masked)

    def test_scrub_masks_an_ipv6_address_but_not_a_clock_time(self):
        self.assertNotIn("2001:db8::1", scrub("could not connect to 2001:db8::1", []))
        self.assertNotIn(
            "fe80:0:0:0:0:0:0:1", scrub("could not connect to fe80:0:0:0:0:0:0:1", [])
        )
        self.assertIn("02:30:00", scrub("attempt at 02:30:00 failed", []))

    def test_the_timestamp_is_fixed_width_and_sorts_chronologically(self):
        early = utc_stamp(datetime(2026, 1, 2, 3, 4, 5))
        late = utc_stamp(datetime(2026, 11, 30, 23, 59, 59))

        self.assertEqual(early, "20260102T030405Z")
        self.assertEqual(late, "20261130T235959Z")
        self.assertRegex(early, r"\A\d{8}T\d{6}Z\Z")
        self.assertRegex(late, r"\A\d{8}T\d{6}Z\Z")
        self.assertLess(early, late)
        self.assertLess(
            utc_stamp(datetime(2025, 12, 31, 23, 59, 59)),
            utc_stamp(datetime(2026, 1, 1, 0, 0, 0)),
        )

    def test_backup_names_carry_the_scheme_retention_matches(self):
        self.assertEqual(
            backup_name("20260101T000000Z", ".sqlite3"),
            f"{PREFIX}-20260101T000000Z.sqlite3",
        )

    def test_pg_dump_lookup_is_a_plain_path_lookup(self):
        with patch.object(
            backup_module.shutil, "which", return_value="/usr/bin/pg_dump"
        ):
            self.assertEqual(pg_dump_binary(), "/usr/bin/pg_dump")
        with patch.object(backup_module.shutil, "which", return_value=None):
            self.assertIsNone(pg_dump_binary())

    def test_a_malformed_retention_env_value_falls_back_to_the_default(self):
        with patch.dict(os.environ, {"BACKUP_RETENTION": "many"}):
            self.assertEqual(backup_module.env_int("BACKUP_RETENTION", DEFAULT_KEEP), 7)
        with patch.dict(os.environ, {"BACKUP_RETENTION": " 3 "}):
            self.assertEqual(backup_module.env_int("BACKUP_RETENTION", DEFAULT_KEEP), 3)

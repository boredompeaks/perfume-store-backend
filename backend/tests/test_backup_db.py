"""SPEC-22-02 [R-22.9]: the backup command's pins.

A backup is the artefact nobody notices until the day it is needed, so the
properties that make it trustworthy are pinned here rather than its happy
path: a failed dump must be loud, retention must keep the newest N and touch
nothing else, and the run must never put a credential - the password, or the
`DATABASE_URL` that carries it - anywhere near a terminal, a log or a ticket.

Hermetic by construction:

* no test opens a network connection and no test shells out to a real
  `pg_dump` - `subprocess.run` and the binary lookup are patched, exactly as
  the restore-drill tests mock what they cannot own;
* the sqlite case is real: a real sqlite file in a temp directory is dumped
  with the stdlib online backup API, and the copy is read back to prove the
  rows survived and the source is unchanged;
* every dump lands in a temp directory the test creates and removes, and the
  `DATABASE_URL` probe is a `patch.dict` over `os.environ` that never leaves
  the process.

The environment contract is asserted through the command's own public
behaviour - its exit status and its output - so a refactor cannot quietly
make a pin vacuous.
"""

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

REPO_ROOT = Path(__file__).resolve().parents[2]

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

# A probe credential. It must never appear in any command output, and the
# tests prove that by putting it in both the connection config and the
# environment. It is deliberately self-describing and low-entropy: this
# repository's own secret scan is a release gate, and a fixture that cannot be
# mistaken for a committed credential is a better fixture for it (the value
# below carries no x-padding either, so it does not read as a redaction
# placeholder). The URL is assembled from it, so no complete
# `postgres://user:password@host` string is ever written out in this repository.
PROBE_PASSWORD = "probe-fixture-only"
PROBE_USER = "probe-user"
PROBE_HOST = "db.internal"
PROBE_DB = "probe_store"
PROBE_URL = (
    f"postgres://{PROBE_USER}:{PROBE_PASSWORD}@{PROBE_HOST}:5432/{PROBE_DB}"
    "?sslmode=require"
)
DUMP_BODY = b"PGDMP-fake-dump"  # starts with pg_dump's custom-format signature

# "Pass no --output-dir at all", distinguishable from "pass an empty one".
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
    """A temp backup directory, a probe `DATABASE_URL`, and the two runners."""

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

    # -- runners ----------------------------------------------------------

    @contextmanager
    def using_databases(self, **aliases):
        """Configure the connection aliases the command will read.

        `settings.DATABASES` is patched in place rather than through
        `override_settings`: this command never resolves a connection, so the
        connection handler must stay out of it, and the in-place patch avoids
        Django's DATABASES-override warning.
        """
        with patch.dict(backup_module.settings.DATABASES, aliases, clear=True):
            yield

    @contextmanager
    def pg_dump(self, returncode=0, stderr=b"", binary="/usr/bin/pg_dump"):
        """Fake the pg_dump child process.

        The fake writes a recognisable dump into the `stdout` handle it was
        handed - which is the file the command opened - so the assertions are
        about what really landed on disk, not about a mock's return value.
        """
        calls = self.pg_calls

        def fake_run(argv, stdout=None, stderr_=None, env=None, check=False, **kwargs):
            calls.append({"argv": argv, "env": env, "check": check, "stdout": stdout})
            stdout.write(DUMP_BODY)
            return type("Completed", (), {"returncode": returncode, "stderr": stderr})()

        with patch.object(backup_module.subprocess, "run", fake_run), patch.object(
            backup_module, "pg_dump_binary", return_value=binary
        ):
            yield

    def run_command(self, **kwargs):
        """Run the command with its output captured.

        ``output_dir`` defaults to the temp directory because almost every
        case wants it; pass ``output_dir=UNSET`` to run the command exactly as
        the scheduler would, with only the environment to configure it.
        """
        if "output_dir" not in kwargs:
            kwargs["output_dir"] = str(self.backup_dir)
        elif kwargs["output_dir"] is UNSET:
            kwargs.pop("output_dir")
        out = io.StringIO()
        call_command("backup_db", stdout=out, **kwargs)
        return out.getvalue()

    # -- fixtures ---------------------------------------------------------

    def dumps(self):
        """The backup files in the directory - this command's scheme only."""
        return sorted(
            path.name
            for path in self.backup_dir.iterdir()
            if path.is_file()
            and path.name.startswith(f"{PREFIX}-")
            and path.suffix in (".dump", ".sqlite3")
        )

    def write_dump(self, stamp, suffix=".dump", content=None):
        """Write a file exactly as this command would have written it.

        The name carries the scheme's stamp AND the bytes carry the engine's
        signature, which is what retention requires before it will consider
        deleting anything. `content` overrides that to forge a foreign file.
        """
        path = self.backup_dir / backup_name(stamp, suffix)
        path.write_bytes(SIGNATURES[suffix] if content is None else content)
        return path

    def write_foreign(self, name, content=b"an operator's own file"):
        """Park a file this command did not write in the backup directory."""
        path = self.backup_dir / name
        path.write_bytes(content)
        return path

    def make_database(self, name="live.sqlite3"):
        source = self.backup_dir / name
        # closing(), not `with`: sqlite3's context manager is TRANSACTIONAL and
        # leaves the connection - and therefore the file handle, and the
        # uncommitted rows - open, which on Windows makes every later read of
        # the file fail.
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("CREATE TABLE scent (id integer primary key, name text)")
            connection.execute("INSERT INTO scent (name) VALUES ('oud')")
            connection.execute("INSERT INTO scent (name) VALUES ('rose')")
            connection.commit()
        return source


@tag("ops")
class PostgresBackupTests(BackupTestCase):
    """The pg_dump path, with the child process pinned: no client, no network."""

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
        """`stdout=handle`: the dump must not be captured by this process, or a
        large database would be buffered in memory."""
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            output = self.run_command()

        self.assertNotIn(DUMP_BODY.decode(), output)
        self.assertTrue(self.pg_calls[-1]["stdout"].name.endswith(".dump.partial"))

    def test_a_successful_run_leaves_no_partial_file_behind(self):
        """A half-written dump must never be sitting in the backup directory
        looking like a backup."""
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            self.run_command()

        self.assertEqual(
            [name for name in self.dumps() if name.endswith(".partial")], []
        )

    def test_the_command_output_never_contains_the_url_or_the_password(self):
        """The load-bearing secret pin: a probe URL carrying a password is in
        BOTH the environment and the connection config, and neither the URL,
        the password nor the user may appear in anything printed."""
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump():
            output = self.run_command()

        for secret in (PROBE_URL, PROBE_PASSWORD, PROBE_USER):
            self.assertNotIn(secret, output)

    def test_pg_dump_argv_carries_no_connection_identity(self):
        """argv is readable by any user through `ps`, so it holds flags only:
        host, port, user, database and password go through the child's
        environment instead."""
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
        # the URL's sslmode is honoured, so a managed database is dumped over
        # the same TLS the app is served with
        self.assertEqual(env["PGSSLMODE"], "require")

    def test_a_failed_dumps_error_text_cannot_leak_the_password(self):
        """`pg_dump` output is third-party text that may quote the connection
        it was given; it is redacted before it is printed."""
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump(
            returncode=1,
            stderr=f"pg_dump: error: connection to {PROBE_URL} failed".encode(),
        ):
            with self.assertRaises(CommandError) as caught:
                self.run_command()

        message = str(caught.exception)
        self.assertNotIn(PROBE_PASSWORD, message)
        self.assertNotIn(PROBE_URL, message)
        self.assertIn("***redacted***", message)
        self.assertIn("pg_dump exited 1", message)

    def test_a_failed_dumps_error_text_cannot_leak_the_connection_identity(self):
        """The load-bearing redaction pin, in the shape libpq actually prints.

        A refused connection is quoted back with the host, the address libpq
        RESOLVED, the port, the user and the database name - none of which is
        the password, and the resolved address is in no setting at all. Cron
        mails that text, so none of it may survive into the exception.
        """
        stderr = (
            f'pg_dump: error: connection to server at "{PROBE_HOST}" '
            f"(10.42.0.7), port 5432 failed: FATAL:  password authentication "
            f'failed for user "{PROBE_USER}"\n'
            f"\tConnection parameters: host={PROBE_HOST} port=5432 "
            f"user={PROBE_USER} dbname={PROBE_DB} sslmode=require\n"
        ).encode()

        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump(
            returncode=1, stderr=stderr
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
        # the operator still learns what failed and why
        self.assertIn("pg_dump exited 1", message)
        self.assertIn("password authentication failed", message)
        self.assertIn("***redacted***", message)

    def test_a_failure_with_no_diagnostic_output_still_says_something(self):
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump(
            returncode=2, stderr=b"   "
        ):
            with self.assertRaises(CommandError) as caught:
                self.run_command()

        message = str(caught.exception)
        self.assertIn("no diagnostic output", message)
        self.assertNotIn(PROBE_PASSWORD, message)

    def test_a_failed_dump_exits_non_zero_and_keeps_no_dump(self):
        """A backup that failed must be loud, or the schedule silently
        produces nothing while the runbook still claims backups exist."""
        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump(
            returncode=1, stderr=b"pg_dump: error: boom"
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
        # the refusal names the documented alternative, not just the problem
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
    """The sqlite path is a real backup of a real file - nothing is faked."""

    def test_a_sqlite_backup_copies_the_rows_and_leaves_the_source_untouched(self):
        """The point of a backup: the rows are really in the dump, and the
        live database really is unchanged (it is opened read-only)."""
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
        """A cron entry firing on a second boundary, with a manual run right
        behind it, must not destroy the dump it just took."""
        source = self.make_database()

        with self.using_databases(default=sqlite_entry(source)):
            self.run_command()
            self.run_command()

        self.assertEqual(len(self.dumps()), 2)

    def test_it_refuses_to_write_the_backup_over_an_existing_file(self):
        """A dump always lands on a fresh filename. When the live database is
        itself named like a dump and lives in the backup directory, the copy
        is disambiguated rather than written on top of the store - and the
        database is left byte-identical."""
        stamp = "20260101T000000Z"
        source = self.make_database(backup_name(stamp, ".sqlite3"))
        before = source.read_bytes()

        with self.using_databases(default=sqlite_entry(source)), patch.object(
            backup_module, "utc_stamp", return_value=stamp
        ):
            self.run_command()

        self.assertEqual(
            self.dumps(),
            [
                # '-' sorts before '.', so the disambiguated copy sorts first
                f"{PREFIX}-{stamp}-1.sqlite3",
                backup_name(stamp, ".sqlite3"),
            ],
        )
        self.assertEqual(source.read_bytes(), before)

    def test_a_corrupt_database_file_fails_loudly_and_leaves_no_partial(self):
        """A file that is not a sqlite database is an operator situation, not a
        reason to keep a half-written copy around: the run must fail with the
        driver's own diagnosis and remove the partial file."""
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
        """ "A backup" of an in-memory database is a backup of nothing - both
        spellings Django uses for one."""
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
    """Keep the newest N; only ever delete what this command created."""

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
        """Retention is a deletion loop and has to be narrow. A file parked in
        the backup directory is not a deletion candidate even when it borrows
        this command's PREFIX: `perfume-latest.dump` and `perfume-2026.dump`
        have no stamp, `perfume-20260101T000000ZZ.dump` has one of the wrong
        width. Only a stamped dump of ours is."""
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
        """The load-bearing retention pin: an operator's own dump, taken by
        hand long before this command existed, is named exactly like ours and
        still has to survive. Its bytes are not a dump this command wrote (a
        hand-taken `pg_dump` defaults to plain SQL), so it is left alone -
        while a real dump older than the keep count is pruned."""
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
        """A file that carries the exact name scheme but is not a dump this
        command wrote is still not ours to delete - retention trusts the
        content, not the name."""
        impostor = self.write_foreign(
            f"{PREFIX}-20251231T235959Z.dump", b"a note to self"
        )
        self.write_dump("20260101T000000Z")
        self.write_dump("20260102T000000Z")

        removed = prune(self.backup_dir, 1)

        self.assertEqual(removed, [f"{PREFIX}-20260101T000000Z.dump"])
        self.assertTrue(impostor.is_file())

    def test_a_stamp_that_is_not_a_date_is_not_our_scheme(self):
        """The name test is a STAMP SHAPE, not just the prefix: a file named
        with something that is not `<8 digits>T<6 digits>Z` is never a
        candidate, however much of our name it borrows."""
        for name in (
            f"{PREFIX}-9999999T00000Z.dump",  # one digit short
            f"{PREFIX}-20260101T000000ZZ.dump",  # one character long
            f"{PREFIX}-20260101T0000.dump",  # not a UTC stamp at all
            f"{PREFIX}-latest.dump",  # a name, not a stamp
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
        """An unreadable candidate is skipped, not deleted: a deletion loop
        that crashes on something it cannot read is worse than one that leaves
        it for an operator."""
        impostor = self.backup_dir / backup_name("20251231T235959Z", ".dump")
        impostor.mkdir()
        self.write_dump("20260101T000000Z")
        self.write_dump("20260102T000000Z")

        self.assertEqual(
            prune(self.backup_dir, 1), [f"{PREFIX}-20260101T000000Z.dump"]
        )
        self.assertTrue(impostor.is_dir())

    def test_ownership_is_decided_by_the_engine_signature_not_the_name(self):
        """`is_own_dump` is the content half of the rule retention applies,
        pinned directly so it cannot be widened: our engine's signature and
        nothing else. The name half is `prune`'s stamped glob."""
        self.assertTrue(is_own_dump(self.write_dump("20260101T000000Z")))
        self.assertTrue(is_own_dump(self.write_dump("20260101T000000Z", ".sqlite3")))
        disambiguated = self.backup_dir / f"{PREFIX}-20260101T000000Z-1.dump"
        disambiguated.write_bytes(SIGNATURES[".dump"])
        self.assertTrue(is_own_dump(disambiguated))
        # a suffix this command does not write is never ours, whatever it holds
        self.assertFalse(
            is_own_dump(self.write_foreign(f"{PREFIX}-20260101T000000Z.gz", b"PGDMP"))
        )
        # and a stamped name with foreign bytes is not ours either
        self.assertFalse(
            is_own_dump(self.write_foreign(f"{PREFIX}-20260101T000000Z.dump", b"x"))
        )

    def test_the_command_prunes_to_the_keep_count_after_a_dump(self):
        for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
            self.write_dump(stamp)

        with self.using_databases(default=POSTGRES_ENTRY), self.pg_dump(), patch.object(
            backup_module, "utc_stamp", return_value="20260104T000000Z"
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
        """The documented alternative path: a dump taken by the postgres
        image's own pg_dump, then pruned by this command."""
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
    """The refusals that stop an unsafe backup from looking like a safe one."""

    def run_without_backup_dir_env(self, **kwargs):
        """Run with `BACKUP_DIR` genuinely unset and no --output-dir, exactly
        as the scheduler invokes it: the refusal has to come from the
        environment contract alone."""
        environment = {
            key: value for key, value in os.environ.items() if key != "BACKUP_DIR"
        }
        with patch.dict(os.environ, environment, clear=True):
            return self.run_command(output_dir=UNSET, **kwargs)

    def test_it_refuses_to_run_without_a_backup_directory(self):
        """A dump written into a container's writable layer is lost when the
        container is replaced - the exact failure a backup exists to prevent."""
        with self.using_databases(default=POSTGRES_ENTRY):
            with self.assertRaises(CommandError) as caught:
                self.run_without_backup_dir_env()

        message = str(caught.exception)
        self.assertIn("refuses to run without a backup directory", message)
        self.assertIn("BACKUP_DIR", message)

    def test_a_dry_run_is_not_a_bypass_of_the_directory_refusal(self):
        """A dry run is a report, not an exemption."""
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
        # and it never started a subprocess
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
    """The schedule itself is a file, so it can be pinned like any other.

    The backup cadence lives in a script (`scripts/backup.sh`) plus the cron
    entry in the deploy runbook. Neither is executed by the suite - this is a
    text contract, in the spirit of `test_deployment_contract.py`, because a
    schedule that quietly stops being wired is exactly the kind of thing
    nobody notices until the day it is needed.
    """

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
        # refuses before touching anything: no .env, no backup directory
        self.assertIn("[ -f .env ] || fail", text)
        self.assertIn("BACKUP_DIR=${BACKUP_DIR:-}", text)
        self.assertIn('[ -n "$BACKUP_DIR" ] || fail', text)

    def test_the_backup_script_refuses_a_relative_backup_directory(self):
        """A relative path would depend on the working directory of whatever
        runs the scheduler - and the dump would land somewhere the operator
        never looks."""
        text = self.read(self.SCRIPT)
        self.assertIn('case "$BACKUP_DIR" in', text)
        self.assertIn("must be an absolute host path", text)

    def test_the_backup_script_binds_the_dump_directory_into_the_container(self):
        """Without the mount, the dump is written to a container filesystem
        that is thrown away when the container is replaced."""
        text = self.read(self.SCRIPT)
        self.assertIn('-v "$BACKUP_DIR:$CONTAINER_BACKUP_DIR"', text)
        # and the key the command reads is overridden with the container-side
        # path, so the host value can never be written into the container
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
        """A credential-shaped NAME must resolve through the environment."""
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
        """A commit that changes only the backup cadence still has to run the
        gate. `scripts/release.sh` is on the list for the same reason: the
        cadence is the deploy host's scheduled entry point, and a change to it
        that never runs the suite can reach a production host unremarked.

        A workflow cannot be executed here, so this is a text contract - and it
        says as much by being about the trigger list rather than about the
        script.
        """
        workflow = self.read(self.WORKFLOW)
        triggers = workflow.split("  push:", 1)[1].split("  pull_request:", 1)
        self.assertEqual(len(triggers), 2, "the workflow lost a trigger block")
        for block in triggers:
            with self.subTest(trigger="push/pull_request"):
                self.assertIn(f'"{self.SCRIPT}"', block)

    def test_the_cadence_is_documented_and_the_documented_prerequisite_is_true(self):
        """The runbook carries the cron entry AND the pg_dump prerequisite it
        depends on. A cron line that points at a missing binary is a schedule
        that silently produces nothing."""
        runbook = self.read(self.RUNBOOK)
        self.assertIn("scripts/backup.sh", runbook)
        self.assertIn("crontab", runbook)
        # the client is not in the image, so the runbook must say so
        self.assertIn("pg_dump", runbook)
        self.assertRegex(runbook, r"(?i)postgresql[- ]client")


@tag("ops")
class BackupScriptExecutionTests(SimpleTestCase):
    """The cadence is EXECUTED here, not read.

    Every other pin on `scripts/backup.sh` in this file is a text assertion,
    and text assertions cannot see a dead script: the shipped bug was a
    `set -u` reference to an OPTIONAL knob, which made the documented
    invocation - the one in the script's own header and the cron entry in the
    runbook - abort before a single dump was taken, while every text pin
    stayed green.

    So this runs the committed script in a throwaway copy of a deploy host's
    layout with `docker` replaced by a stub on PATH, and with an environment
    that holds exactly what the header documents: `BACKUP_DIR`, and nothing
    else.
    """

    # On Windows the `bash` on PATH is usually the WSL launcher, which hangs
    # forever when no distribution is installed; Git-for-Windows ships a real
    # bash, so it is preferred there.
    GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
    SCRIPT = "scripts/backup.sh"
    # Records the argv the script handed the container and succeeds: the dump
    # itself is what the management command does, not this script's business.
    DOCKER_STUB = (
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$DOCKER_LOG\"\nexit 0\n"
    )

    @classmethod
    def bash_executable(cls):
        if os.name == "nt" and cls.GIT_BASH.is_file():
            return str(cls.GIT_BASH)
        return shutil.which("bash")

    def run_the_documented_invocation(self, extra_env=None):
        """Run `./scripts/backup.sh` with only BACKUP_DIR set, as documented.

        Returns (completed_process, docker_calls).
        """
        bash = self.bash_executable()
        if not bash:
            self.skipTest("no bash on this host, so the script cannot be run")

        root = Path(mkdtemp(prefix="backup-script-test-"))
        self.addCleanup(self._remove_tree, root)
        (root / "scripts").mkdir()
        # The committed script, byte for byte: a copy, not a rewrite.
        (root / self.SCRIPT).write_bytes((REPO_ROOT / self.SCRIPT).read_bytes())
        # The two files the script checks for before it touches anything.
        (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (root / ".env").write_text("DJANGO_SECRET_KEY=dummy\n", encoding="utf-8")
        # The one-shot container is the only thing this test cannot own.
        stub_dir = root / "stub-bin"
        stub_dir.mkdir()
        (stub_dir / "docker").write_text(
            self.DOCKER_STUB, encoding="utf-8", newline="\n"
        )
        os.chmod(stub_dir / "docker", 0o755)
        # A POSIX absolute path, which is what a deploy host has and what the
        # script insists on. On Windows the temp tree is addressed the way the
        # bash running this test sees it (`/tmp` is %TEMP% there), so what is
        # under test is the script, not the host's path syntax. The script
        # creates the directory itself - that is part of what it is pinned for.
        backups = root / "backups"
        log = root / "docker-calls.log"

        environment = {
            # The stub first, so the real docker is never reached.
            "PATH": os.pathsep.join([str(stub_dir), os.environ.get("PATH", "")]),
            "BACKUP_DIR": (
                str(backups) if os.name != "nt" else f"/tmp/{root.name}/backups"
            ),
            "DOCKER_LOG": str(log),
        }
        if os.name == "nt":
            # MSYS bash needs these to run anything on a Windows host.
            environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            environment["TEMP"] = os.environ.get("TEMP", "")
        # The documented invocation sets BACKUP_DIR ALONE: the optional knob
        # must not be inherited from the machine running the suite. A test that
        # asks for it explicitly opts back in below.
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
        """The cadence as the header and the cron entry invoke it: one knob,
        no unbound variable, and the backup command actually runs."""
        completed, calls = self.run_the_documented_invocation()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("unbound variable", completed.stderr)
        # it got all the way to the dump, which is the whole point
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("compose -f docker-compose.yml run --rm -T", calls[0])
        self.assertIn("backup_db", calls[0])

    def test_the_documented_invocation_hands_the_default_retention_to_the_command(self):
        """`docker compose run` does not forward the host environment, so the
        retention the script resolved is passed explicitly - otherwise the
        command applies its own default while the log claims the operator's."""
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
        """Executing the script must not soften it: the runbook's safety pins
        are pins, not decoration."""
        completed, _ = self.run_the_documented_invocation(
            {"BACKUP_DIR": "relative/path"}
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("must be an absolute host path", completed.stderr)


@tag("ops")
class HelperTests(SimpleTestCase):
    """The pure helpers, pinned directly so a rule cannot be widened."""

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
        """Two signals, two jobs. By shape: the address libpq RESOLVED and its
        parameter dump, which are in no configured value. By value (the
        caller's secret list): the host, the user, the port, the database."""
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
        # the operator still gets the diagnosis
        self.assertIn("pg_dump: detail: sorry", masked)

    def test_scrub_masks_an_ipv6_address_but_not_a_clock_time(self):
        """Address masking must not shred the one numeric thing a 3am operator
        needs from a failure: when it happened."""
        self.assertNotIn("2001:db8::1", scrub("could not connect to 2001:db8::1", []))
        self.assertNotIn(
            "fe80:0:0:0:0:0:0:1", scrub("could not connect to fe80:0:0:0:0:0:0:1", [])
        )
        self.assertIn("02:30:00", scrub("attempt at 02:30:00 failed", []))

    def test_the_timestamp_is_fixed_width_and_sorts_chronologically(self):
        early = utc_stamp(datetime(2026, 1, 2, 3, 4, 5))
        late = utc_stamp(datetime(2026, 11, 30, 23, 59, 59))

        self.assertEqual(len(early), len(late))
        self.assertLess(early, late)

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
        """An unparseable knob must not crash a 3am backup; it takes the
        documented default."""
        with patch.dict(os.environ, {"BACKUP_RETENTION": "many"}):
            self.assertEqual(backup_module.env_int("BACKUP_RETENTION", DEFAULT_KEEP), 7)
        with patch.dict(os.environ, {"BACKUP_RETENTION": " 3 "}):
            self.assertEqual(backup_module.env_int("BACKUP_RETENTION", DEFAULT_KEEP), 3)

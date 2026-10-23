"""P0-1: one suite per test database, one cache namespace per test run.

Two halves are pinned here, mirroring the two guards:

- ``LockedDiscoverRunner`` owns the resolved test database through an
  exclusive OS file lock. A second holder is refused (in-process and from a
  real child process), a free lock is acquired and released, the identity is
  stable per database and distinct across databases, and ``--parallel`` is
  refused only where Django would put every worker on the same SQLite file.
- A test-run settings import points RedisCache at its own DB index, because
  ``clear()`` is FLUSHDB and ignores KEY_PREFIX: ``cache.clear()`` in a test
  must not remove a key written outside that index. A non-test import keeps
  the operator's location byte-for-byte.

The settings half is subprocess-pinned with the repo's existing clean-env
harness (``tests.test_settings_security.run_settings_import``), because CACHES
is built at settings-import time and an in-process read would only prove what
this run happened to import.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache, caches
from django.test import SimpleTestCase

from common import test_isolation
from common.test_isolation import (
    DatabaseSuiteLock,
    LockedDiscoverRunner,
    SuiteLockError,
    parallel_shares_one_database,
    test_database_identity,
)
from config import settings as config_settings
from tests.test_settings_security import (
    _DEV_ENV,
    _NON_DEBUG_ENV,
    run_settings_import,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent

_TEST_MODE_SNIPPET = (
    "import sys; sys.argv = ['manage.py', 'test']; "
    "import config.settings as s; "
    "print('LOCATION', s.CACHES['default']['LOCATION']); "
    "print('BACKEND', s.CACHES['default']['BACKEND'])"
)


class TestRunCacheNamespaceSettingsTests(SimpleTestCase):
    """A test run resolves the default alias to its own Redis DB index."""

    _REDIS = "redis://127.0.0.1:6390/1"

    def test_test_run_uses_the_default_test_db_index(self):
        res = run_settings_import(
            {**_DEV_ENV, "REDIS_URL": self._REDIS, "TEST_REDIS_DB": "13"},
            snippet=_TEST_MODE_SNIPPET,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("LOCATION redis://127.0.0.1:6390/13", res.stdout)
        self.assertIn("BACKEND django.core.cache.backends.redis.RedisCache", res.stdout)

    def test_test_db_index_is_overridable(self):
        res = run_settings_import(
            {**_DEV_ENV, "REDIS_URL": self._REDIS, "TEST_REDIS_DB": "9"},
            snippet=_TEST_MODE_SNIPPET,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("LOCATION redis://127.0.0.1:6390/9", res.stdout)

    def test_a_non_integer_test_db_index_falls_back_to_the_default(self):
        res = run_settings_import(
            {**_DEV_ENV, "REDIS_URL": self._REDIS, "TEST_REDIS_DB": "not-a-number"},
            snippet=_TEST_MODE_SNIPPET,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("LOCATION redis://127.0.0.1:6390/13", res.stdout)

    def test_test_run_without_redis_keeps_the_per_process_cache(self):
        res = run_settings_import(dict(_DEV_ENV), snippet=_TEST_MODE_SNIPPET)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            "BACKEND django.core.cache.backends.locmem.LocMemCache", res.stdout
        )
        self.assertIn("LOCATION default", res.stdout)

    def test_non_test_import_keeps_the_declared_redis_location_unchanged(self):
        res = run_settings_import(
            {**_NON_DEBUG_ENV, "REDIS_URL": self._REDIS},
            snippet=(
                "import config.settings as s; "
                "print('LOCATION', s.CACHES['default']['LOCATION'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"LOCATION {self._REDIS}", res.stdout)


class TestRunRedisClearIsolationTests(SimpleTestCase):
    """Runtime proof: clear() flushes only the test DB index."""

    def _redis_urls(self):
        url = (os.environ.get("REDIS_URL") or "").strip()
        if not url:
            self.skipTest("REDIS_URL is not declared; set it to a reachable Redis")
        if caches["default"].__class__.__name__ != "RedisCache":
            self.skipTest("the default cache alias is not a RedisCache here")
        # The namespace under test is the alias's resolved location, not
        # REDIS_URL: the point is where the TEST run's cache actually lives.
        operating = urlparse(url)
        operating_db = operating.path.lstrip("/") or "0"
        test_db = urlparse(settings.CACHES["default"]["LOCATION"]).path.lstrip("/")
        outside_db = operating_db
        if outside_db == test_db:
            outside_db = "9" if test_db != "9" else "10"
        outside = operating._replace(path=f"/{outside_db}").geturl()
        return test_db, outside

    def _client(self, url):
        import redis

        try:
            client = redis.Redis.from_url(
                url, socket_connect_timeout=3, socket_timeout=3
            )
            client.ping()
        except Exception as exc:  # refused / DNS / timeout - not armed here
            self.skipTest(f"no Redis answered at {url} ({type(exc).__name__})")
        self.addCleanup(client.close)
        return client

    def test_test_cache_clear_does_not_remove_a_key_outside_its_db(self):
        test_db, outside_url = self._redis_urls()
        expected_db = (os.environ.get("TEST_REDIS_DB") or "13").strip() or "13"
        self.assertEqual(test_db, expected_db)
        outside = self._client(outside_url)
        key = "p0-1:written-outside-the-test-namespace"
        outside.set(key, "keep", ex=60)
        try:
            cache.clear()
            self.assertEqual(outside.get(key), b"keep")
        finally:
            outside.delete(key)


class TestRedisLocationHelperTests(SimpleTestCase):
    """The settings helper is pure and testable without another import."""

    def test_a_non_redis_location_is_returned_unchanged(self):
        self.assertEqual(
            config_settings._test_redis_location("memcache://127.0.0.1:11211"),
            "memcache://127.0.0.1:11211",
        )

    def test_the_index_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"TEST_REDIS_DB": "9"}):
            self.assertEqual(
                config_settings._test_redis_location("redis://h:6379/1"),
                "redis://h:6379/9",
            )
        with mock.patch.dict(os.environ, {"TEST_REDIS_DB": "bogus"}):
            self.assertEqual(
                config_settings._test_redis_location("redis://h:6379/1"),
                "redis://h:6379/13",
            )


class DatabaseIdentityTests(SimpleTestCase):
    """The lock key is the resolved database, not the URL's spelling."""

    _PG = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "perfume_store_a",
            "HOST": "127.0.0.1",
            "PORT": "5432",
        }
    }

    def test_same_database_yields_the_same_key(self):
        self.assertEqual(
            test_database_identity(self._PG), test_database_identity(self._PG)
        )

    def test_different_databases_yield_different_keys(self):
        other = {"default": {**self._PG["default"], "NAME": "perfume_store_b"}}
        self.assertNotEqual(
            test_database_identity(self._PG), test_database_identity(other)
        )

    def test_pg_key_uses_the_test_database_name(self):
        # Django creates test_<NAME> unless TEST["NAME"] overrides it; the key
        # must name the database the suite will actually create.
        explicit = {
            "default": {
                **self._PG["default"],
                "TEST": {"NAME": "test_perfume_store_a"},
            }
        }
        self.assertEqual(
            test_database_identity(self._PG), test_database_identity(explicit)
        )

    def test_in_memory_sqlite_needs_no_lock(self):
        databases = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BACKEND_DIR / "db.sqlite3",
            }
        }
        self.assertIsNone(test_database_identity(databases))

    def test_file_backed_sqlite_is_keyed_on_its_path(self):
        databases = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BACKEND_DIR / "db.sqlite3",
                "TEST": {"NAME": "test_db.sqlite3"},
            }
        }
        other = {
            "default": {
                **databases["default"],
                "TEST": {"NAME": "test_other.sqlite3"},
            }
        }
        self.assertIsNotNone(test_database_identity(databases))
        self.assertNotEqual(
            test_database_identity(databases), test_database_identity(other)
        )


class ParallelGuardTests(SimpleTestCase):
    def test_parallel_is_safe_for_postgres(self):
        self.assertFalse(
            parallel_shares_one_database(DatabaseIdentityTests._PG, parallel=8)
        )

    def test_parallel_is_safe_for_in_memory_sqlite(self):
        databases = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BACKEND_DIR / "db.sqlite3",
            }
        }
        self.assertFalse(parallel_shares_one_database(databases, parallel=4))

    def test_parallel_is_refused_for_a_file_backed_sqlite_database(self):
        databases = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BACKEND_DIR / "db.sqlite3",
                "TEST": {"NAME": "test_db.sqlite3"},
            }
        }
        self.assertTrue(parallel_shares_one_database(databases, parallel=2))
        self.assertFalse(parallel_shares_one_database(databases, parallel=1))

    def test_a_non_integer_parallel_value_counts_as_no_parallelism(self):
        self.assertFalse(
            parallel_shares_one_database(DatabaseIdentityTests._PG, parallel="auto")
        )

    def test_runner_refuses_parallel_only_when_workers_share_one_database(self):
        runner = LockedDiscoverRunner(
            verbosity=0, interactive=False, lock_key="parallel-key"
        )
        runner.parallel = 2
        with mock.patch.object(
            test_isolation, "parallel_shares_one_database", return_value=True
        ):
            with self.assertRaises(SystemExit) as caught:
                runner.setup_test_environment()
        self.assertIn("--parallel", str(caught.exception))


class DatabaseSuiteLockTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp(prefix="p0-1-locks-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_a_free_lock_is_acquired_and_released(self):
        with DatabaseSuiteLock("free-key", directory=self.directory, timeout=0) as lock:
            self.assertTrue(lock.held)
            self.assertTrue(lock.path.exists())
            lock.acquire()  # re-entrant: already held, nothing changes
        self.assertFalse(lock.held)

        # Released: another holder can take it immediately.
        again = DatabaseSuiteLock("free-key", directory=self.directory, timeout=0)
        again.acquire()
        again.release()

    def test_a_second_holder_is_refused_with_the_owner_named(self):
        held = DatabaseSuiteLock("busy-key", directory=self.directory, timeout=0)
        held.acquire()
        self.addCleanup(held.release)
        refused = DatabaseSuiteLock("busy-key", directory=self.directory, timeout=0)
        with self.assertRaises(SuiteLockError) as caught:
            refused.acquire()
        message = str(caught.exception)
        self.assertIn("already owned", message)
        self.assertIn(str(os.getpid()), message)
        self.assertIn("busy-key", message)

    def test_the_wait_is_bounded_by_the_timeout(self):
        held = DatabaseSuiteLock("timed-key", directory=self.directory, timeout=0)
        held.acquire()
        self.addCleanup(held.release)
        refused = DatabaseSuiteLock("timed-key", directory=self.directory, timeout=0.2)
        started = time.monotonic()
        with self.assertRaises(SuiteLockError):
            refused.acquire()
        self.assertLess(time.monotonic() - started, 5.0)

    def test_an_invalid_timeout_env_falls_back_instead_of_crashing(self):
        with mock.patch.dict(os.environ, {"TEST_SUITE_LOCK_TIMEOUT": "nope"}):
            lock = DatabaseSuiteLock("env-key", directory=self.directory)
        self.assertEqual(lock.timeout, 5.0)

    def test_a_real_second_process_is_refused(self):
        held = DatabaseSuiteLock("process-key", directory=self.directory, timeout=0)
        held.acquire()
        self.addCleanup(held.release)
        child = (
            "import sys\n"
            "from common.test_isolation import DatabaseSuiteLock, SuiteLockError\n"
            "lock = DatabaseSuiteLock(sys.argv[1], directory=sys.argv[2], timeout=0)\n"
            "try:\n"
            "    lock.acquire()\n"
            "except SuiteLockError as exc:\n"
            "    print('REFUSED', exc)\n"
            "    sys.exit(3)\n"
            "print('ACQUIRED')\n"
            "lock.release()\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", child, "process-key", self.directory],
            capture_output=True,
            text=True,
            cwd=tempfile.gettempdir(),
            env={**os.environ, "PYTHONPATH": str(BACKEND_DIR)},
            timeout=60,
        )
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("REFUSED", res.stdout)
        self.assertNotIn("ACQUIRED", res.stdout)

    def test_the_key_from_this_run_is_the_configured_database(self):
        # The live suite runs against some database; the runner's default key
        # must be exactly what the pure identity function derives for it.
        self.assertEqual(
            LockedDiscoverRunner(verbosity=0, interactive=False).get_lock_key(),
            test_database_identity(),
        )


class LockedRunnerTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp(prefix="p0-1-runner-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _runner(self, key):
        return LockedDiscoverRunner(
            verbosity=0,
            interactive=False,
            lock_key=key,
            lock_dir=self.directory,
            lock_timeout=0.2,
        )

    def test_runner_takes_the_lock_and_gives_it_back_on_teardown(self):
        runner = self._runner("runner-key")
        runner._acquire_suite_lock()
        self.assertTrue(runner._suite_lock.held)
        runner._acquire_suite_lock()  # already held: no second lock
        self.assertTrue(runner._suite_lock.held)
        refused = DatabaseSuiteLock("runner-key", directory=self.directory, timeout=0)
        with self.assertRaises(SuiteLockError):
            refused.acquire()
        runner._release_suite_lock()
        runner._release_suite_lock()  # already released: no-op
        self.assertIsNone(runner._suite_lock)
        reopened = DatabaseSuiteLock("runner-key", directory=self.directory, timeout=0)
        reopened.acquire()
        reopened.release()

    def test_runner_refuses_to_start_while_the_database_is_owned(self):
        held = DatabaseSuiteLock("owned-key", directory=self.directory, timeout=0)
        held.acquire()
        self.addCleanup(held.release)
        runner = self._runner("owned-key")
        with self.assertRaises(SystemExit) as caught:
            runner._acquire_suite_lock()
        self.assertIn("Refusing to start", str(caught.exception))
        self.assertIn("already owned", str(caught.exception))
        self.assertIsNone(runner._suite_lock)

    def test_an_in_memory_database_is_not_locked(self):
        runner = LockedDiscoverRunner(verbosity=0, interactive=False)
        with mock.patch.object(
            test_isolation, "test_database_identity", return_value=None
        ):
            runner._acquire_suite_lock()
        self.assertIsNone(runner._suite_lock)


class LockDiagnosticsTests(SimpleTestCase):
    """The defensive paths around the lock: hooks, owner file, failure paths."""

    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp(prefix="p0-1-diag-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _lock(self, key="diag-key"):
        return DatabaseSuiteLock(key, directory=self.directory, timeout=0)

    def test_lock_directory_is_env_overridable(self):
        with mock.patch.dict(os.environ, {"TEST_SUITE_LOCK_DIR": self.directory}):
            self.assertEqual(test_isolation._lock_directory(), Path(self.directory))

    def test_lock_directory_defaults_beside_the_os_temp_dir(self):
        with mock.patch.dict(os.environ, {"TEST_SUITE_LOCK_DIR": ""}):
            self.assertEqual(
                test_isolation._lock_directory(),
                Path(tempfile.gettempdir()) / "perfume_store_test_locks",
            )

    def test_release_on_an_unheld_lock_is_a_no_op(self):
        lock = self._lock()
        lock.release()
        self.assertFalse(lock.held)

    def test_a_vanished_owner_file_does_not_fail_the_release(self):
        lock = self._lock()
        lock.acquire()
        lock.owner_path.unlink()
        lock.release()
        self.assertFalse(lock.held)

    def test_an_unlock_error_still_closes_the_descriptor(self):
        lock = self._lock()
        lock.acquire()
        with mock.patch.object(test_isolation, "_unlock", side_effect=OSError):
            lock.release()
        self.assertFalse(lock.held)

    def test_a_signal_handler_that_already_went_away_is_ignored(self):
        lock = self._lock()
        lock._previous_handlers = {signal.SIGINT: signal.getsignal(signal.SIGINT)}
        with mock.patch.object(signal, "signal", side_effect=ValueError):
            lock.release()
        self.assertEqual(lock._previous_handlers, {})

    def test_an_unwritable_owner_file_is_only_diagnostic(self):
        lock = self._lock()
        lock.owner_path = Path(self.directory) / "missing-dir" / "owner"
        lock._write_owner()
        self.assertFalse(lock.owner_path.exists())

    def test_a_missing_owner_file_reads_as_another_process(self):
        self.assertEqual(self._lock()._holder(), "another process")

    def test_a_non_numeric_owner_file_reads_as_another_process(self):
        lock = self._lock()
        lock.owner_path.write_text("not-a-pid\n", encoding="ascii")
        self.assertEqual(lock._holder(), "another process")

    def test_hooks_install_without_signals_from_a_worker_thread(self):
        lock = self._lock()
        worker = threading.Thread(target=lock._install_hooks)
        worker.start()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(lock._previous_handlers, {})

    def test_a_platform_without_a_signal_name_skips_it(self):
        fake = SimpleNamespace(
            SIGINT=object(),
            getsignal=mock.Mock(return_value="previous"),
            signal=mock.Mock(),
        )
        lock = self._lock()
        with mock.patch.object(test_isolation, "signal", fake):
            lock._install_hooks()
        self.assertEqual(len(lock._previous_handlers), 1)
        self.assertEqual(lock._previous_handlers[fake.SIGINT], "previous")
        # The fake signal object cannot be handed to the real signal module.
        lock._previous_handlers.clear()

    def test_a_signal_handler_that_cannot_be_installed_is_skipped(self):
        fake = SimpleNamespace(
            SIGINT=object(),
            SIGTERM=None,
            getsignal=mock.Mock(return_value="previous"),
            signal=mock.Mock(side_effect=ValueError),
        )
        lock = self._lock()
        with mock.patch.object(test_isolation, "signal", fake):
            lock._install_hooks()
        # The failed install never became a handler; the stored previous value
        # is discarded with the lock (and cleared here for atexit hygiene).
        self.assertEqual(len(lock._previous_handlers), 1)
        backup = lock._previous_handlers
        self.addCleanup(backup.clear)
        lock._previous_handlers.clear()

    def test_the_signal_handler_releases_and_exits_non_zero(self):
        lock = self._lock()
        with self.assertRaises(SystemExit) as caught:
            lock._signal_handler(signal.SIGTERM, None)
        self.assertEqual(caught.exception.code, 128 + int(signal.SIGTERM))
        self.assertFalse(lock.held)

    def test_the_posix_branch_uses_flock(self):
        fake_fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=mock.Mock())
        fake_os = SimpleNamespace(name="posix")
        with (
            mock.patch.object(test_isolation, "os", fake_os),
            mock.patch.dict(sys.modules, {"fcntl": fake_fcntl}),
        ):
            test_isolation._try_lock(7)
            test_isolation._unlock(7)
        fake_fcntl.flock.assert_has_calls(
            [mock.call(7, fake_fcntl.LOCK_EX | fake_fcntl.LOCK_NB), mock.call(7, 4)]
        )

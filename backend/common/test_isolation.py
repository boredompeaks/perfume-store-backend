"""Test-run isolation: one suite per test database, one cache namespace per run.

Two suites pointed at one test database destroy each other. The 2026-10-20
parallel run on pg2a measured it: 1,110 ``FATAL database ... does not exist``,
192 schema-drift errors and 32 deadlocks (docs/run/PG-1-damage-report.md). The
same run made 125,640 ``FLUSHDB`` calls against one shared Redis database and
collected 466 ``ERR`` replies, because ``cache.clear()`` on Django's
``RedisCache`` is ``FLUSHDB`` - it ignores ``KEY_PREFIX`` and wipes every key
in the selected database, including a deployment's.

This module closes the first half: ``LockedDiscoverRunner`` takes an exclusive
OS-level file lock keyed on the database the suite is about to create, refuses
to start (non-zero) when another suite owns it, and releases on teardown,
process exit and SIGINT/SIGTERM. The second half lives in ``config.settings``,
which points a test run's Redis cache at its own DB index so ``clear()`` can
only flush the test namespace.

The lock is deliberately stdlib-only (``msvcrt`` on Windows, ``fcntl`` on
POSIX): a test-infrastructure guard that needed installing before it could
protect the suite would be missing exactly when a fresh checkout runs first.
"""

import atexit
import hashlib
import os
import signal
import tempfile
import threading
import time
from pathlib import Path

from django.conf import settings
from django.test.runner import DiscoverRunner

# A test database that lives in memory is private to the process: Django gives
# every worker its own, so two processes cannot corrupt each other and no lock
# is needed. A real file (or a server database) is shared, and that is what the
# lock key names.
_IN_MEMORY_MARKERS = (":memory:", "mode=memory")


class SuiteLockError(RuntimeError):
    """A test database is already owned by another running suite."""


def _is_in_memory(test_name):
    if not test_name:
        return True
    return any(marker in str(test_name) for marker in _IN_MEMORY_MARKERS)


def test_database_identity(databases=None):
    """Return a stable, filesystem-safe key for the suite's test database.

    The identity is derived from the RESOLVED connection settings - engine,
    host, port and the database name Django will test - never from
    ``DATABASE_URL`` itself, so two processes that name the same database by
    different spellings (an explicit ``TEST["NAME"]`` versus the default
    ``test_<NAME>``) still collide, which is the point. Credentials are not
    part of the key and never reach the lock file.

    Returns ``None`` for an in-memory SQLite database, which no other process
    can share.
    """
    databases = settings.DATABASES if databases is None else databases
    config = databases["default"]
    engine = str(config.get("ENGINE") or "")
    test_name = (config.get("TEST") or {}).get("NAME")

    if engine.endswith("sqlite3"):
        if _is_in_memory(test_name):
            return None
        identity = f"sqlite:{os.path.abspath(str(test_name))}"
    else:
        name = test_name or f"test_{config.get('NAME') or ''}"
        host = config.get("HOST") or "localhost"
        port = config.get("PORT") or ""
        identity = f"{engine}:{host}:{port}:{name}"

    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def parallel_shares_one_database(databases=None, parallel=None):
    """True when ``--parallel`` would put workers on ONE shared test database.

    PostgreSQL (and MySQL) give each worker its own cloned database, so
    parallel runs are safe there. A file-backed SQLite test database does not:
    every worker opens the same file, which is the suite-vs-suite corruption
    this task exists to stop, reproduced inside one run.
    """
    try:
        workers = int(parallel or 0)
    except (TypeError, ValueError):
        workers = 0
    if workers < 2:
        return False
    databases = settings.DATABASES if databases is None else databases
    config = databases["default"]
    if not str(config.get("ENGINE") or "").endswith("sqlite3"):
        return False
    return not _is_in_memory((config.get("TEST") or {}).get("NAME"))


def _lock_directory():
    configured = (os.environ.get("TEST_SUITE_LOCK_DIR") or "").strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "perfume_store_test_locks"


def _try_lock(fd):
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd):
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class DatabaseSuiteLock:
    """Exclusive, non-blocking-by-default lock file for one test database.

    ``timeout`` is the total time spent retrying before refusing; zero means
    one attempt. The holder's PID is written to a sidecar file so the refusal
    can name the process that owns the database instead of only the lock path
    (Windows blocks reads of the locked byte itself).
    """

    def __init__(self, key, *, directory=None, timeout=None):
        self.key = key
        self.directory = Path(directory) if directory else _lock_directory()
        self.path = self.directory / f"suite-{key}.lock"
        if timeout is None:
            raw = (os.environ.get("TEST_SUITE_LOCK_TIMEOUT") or "").strip()
            try:
                timeout = float(raw) if raw else 5.0
            except ValueError:
                timeout = 5.0
        self.timeout = max(0.0, float(timeout))
        self._fd = None
        self._previous_handlers = {}
        # Windows file locks block reads of the locked byte too, so the owner
        # diagnostic lives beside the lock in an ordinary (unlocked) file.
        self.owner_path = self.directory / f"suite-{key}.owner"

    @property
    def held(self):
        return self._fd is not None

    def acquire(self):
        if self.held:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT)
        if os.fstat(fd).st_size == 0:
            # msvcrt locks a byte range; a one-byte file makes the range exist
            # before any contender opens it (two contenders may both write it;
            # the content is overwritten by whoever wins the lock).
            os.write(fd, b"\0")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _try_lock(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    holder = self._holder()
                    os.close(fd)
                    raise SuiteLockError(self._refusal(holder))
                time.sleep(0.1)
        self._fd = fd
        self._write_owner()
        self._install_hooks()

    def release(self):
        fd, self._fd = self._fd, None
        for sig, previous in self._previous_handlers.items():
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError):
                pass
        self._previous_handlers.clear()
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            # The OS drops the lock when the descriptor closes anyway.
            pass
        os.close(fd)
        try:
            self.owner_path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    def _write_owner(self):
        try:
            self.owner_path.write_text(f"{os.getpid()}\n", encoding="ascii")
        except OSError:
            # Diagnostic only: the file lock, not this file, is the authority.
            pass

    def _holder(self):
        try:
            raw = self.owner_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return "another process"
        if raw.isdigit():
            return f"pid {raw}"
        return "another process"

    def _refusal(self, holder):
        return (
            f"Refusing to start: the test database for this run is already "
            f"owned by {holder} (lock {self.path}). Two suites on one test "
            f"database corrupt each other - dropped databases, schema drift "
            f"and deadlocks. Wait for that run to finish, or point this one "
            f"at a different database (DATABASE_URL / its own lane)."
        )

    def _install_hooks(self):
        atexit.register(self.release)
        if threading.current_thread() is not threading.main_thread():
            # signal handlers can only be installed from the main thread; the
            # lock still releases on normal teardown and atexit.
            return
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                self._previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._signal_handler)
            except (ValueError, OSError):
                continue

    def _signal_handler(self, signum, frame):
        self.release()
        raise SystemExit(128 + int(signum))


class LockedDiscoverRunner(DiscoverRunner):
    """``DiscoverRunner`` that owns the test database for the whole run.

    Acquisition happens in ``setup_test_environment`` - before Django creates
    or destroys any database - and release in ``teardown_test_environment`` so
    a second suite can start as soon as this one is done. ``--parallel`` is
    only refused where Django would give every worker the SAME database; PG
    clones and in-memory SQLite are allowed.
    """

    def __init__(self, *args, **kwargs):
        self._lock_key_override = kwargs.pop("lock_key", None)
        self._lock_dir_override = kwargs.pop("lock_dir", None)
        self._lock_timeout_override = kwargs.pop("lock_timeout", None)
        super().__init__(*args, **kwargs)
        self._suite_lock = None

    def get_lock_key(self):
        if self._lock_key_override is not None:
            return self._lock_key_override
        return test_database_identity()

    def get_lock_dir(self):
        return self._lock_dir_override

    def get_lock_timeout(self):
        return self._lock_timeout_override

    def setup_test_environment(self, **kwargs):
        self._acquire_suite_lock()
        super().setup_test_environment(**kwargs)

    def teardown_test_environment(self, **kwargs):
        try:
            super().teardown_test_environment(**kwargs)
        finally:
            self._release_suite_lock()

    def _release_suite_lock(self):
        if self._suite_lock is not None:
            self._suite_lock.release()
            self._suite_lock = None

    def _acquire_suite_lock(self):
        if self._suite_lock is not None:
            return
        if parallel_shares_one_database(parallel=self.parallel):
            raise SystemExit(
                "Refusing --parallel: this suite's SQLite test database is a "
                "shared file, so every worker would open the same database. "
                "Use --parallel 1, or run against PostgreSQL where Django "
                "clones a database per worker."
            )
        key = self.get_lock_key()
        if key is None:
            # In-memory SQLite is private to this process; nothing to guard.
            return
        lock = DatabaseSuiteLock(
            key,
            directory=self.get_lock_dir(),
            timeout=self.get_lock_timeout(),
        )
        try:
            lock.acquire()
        except SuiteLockError as exc:
            raise SystemExit(str(exc)) from exc
        self._suite_lock = lock

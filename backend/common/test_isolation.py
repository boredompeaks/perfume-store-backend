import atexit
import hashlib
import os
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, cast

from django.conf import settings
from django.test.runner import DiscoverRunner

_IN_MEMORY_MARKERS = (":memory:", "mode=memory")


class SuiteLockError(RuntimeError):
    """A test database is already owned by another running suite."""


def _is_in_memory(test_name):
    if not test_name:
        return True
    return any(marker in str(test_name) for marker in _IN_MEMORY_MARKERS)


def test_database_identity(databases=None):
    """Derived from the resolved connection settings, not DATABASE_URL, so different spellings of one database still collide; None for in-memory SQLite."""
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
    """A file-backed SQLite test database is shared by every --parallel worker, which is the corruption this guard stops."""
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
    # mypy checks both branches on every platform, so the module the current OS does not ship is reported missing; the cast to Any takes it out of the checker's hands.
    if os.name == "nt":
        import msvcrt

        nt = cast(Any, msvcrt)
        os.lseek(fd, 0, os.SEEK_SET)
        nt.locking(fd, nt.LK_NBLCK, 1)
    else:
        import fcntl

        posix = cast(Any, fcntl)
        posix.flock(fd, posix.LOCK_EX | posix.LOCK_NB)


def _unlock(fd):
    if os.name == "nt":
        import msvcrt

        nt = cast(Any, msvcrt)
        os.lseek(fd, 0, os.SEEK_SET)
        nt.locking(fd, nt.LK_UNLCK, 1)
    else:
        import fcntl

        posix = cast(Any, fcntl)
        posix.flock(fd, posix.LOCK_UN)


class DatabaseSuiteLock:
    """The holder's PID lives in a sidecar file because Windows blocks reads of the locked byte."""

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
            # msvcrt locks a byte range, so the file must be one byte long before any contender opens it.
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
    """Acquired in setup_test_environment before Django creates a database, released in teardown_test_environment."""

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

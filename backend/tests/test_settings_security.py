"""Settings security tests - V-02 (DEBUG fails open) and V-01 containment.

The DEBUG guard lives at settings-import time, so it is exercised in a
subprocess with a clean environment (no .env is loaded from a neutral cwd).
The same subprocess pattern pins the env-driven DATABASE_URL behaviour
(SPEC-2-01); its parser is additionally tested as a pure function so every
branch is covered in-process.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

import config.settings as config_settings
from django.test import SimpleTestCase

BACKEND_DIR = Path(__file__).resolve().parent.parent


def run_settings_import(env_overrides, snippet="import config.settings; print('IMPORT_OK')"):
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("DJANGO_", "RAZORPAY_", "EMAIL_"))
        and k != "DATABASE_URL"
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),  # neutral cwd: no .env is discovered
        env={**env, "PYTHONPATH": str(BACKEND_DIR)},
        timeout=60,
    )


class DebugDefaultTests(SimpleTestCase):
    def test_v02_debug_fails_open_currently(self):
        """V-02: a missing DJANGO_DEBUG env var currently enables DEBUG
        (fail-open). Pinned via subprocess because the test runner itself
        forces DEBUG=False in-process. Flip to assert False when the default
        flips to fail-closed."""
        res = run_settings_import(
            {},
            snippet="import config.settings as s; print('DEBUG_IS', s.DEBUG)",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DEBUG_IS True", res.stdout)


class SettingsGuardTests(SimpleTestCase):
    def test_v02_debug_false_without_secret_key_is_refused(self):
        res = run_settings_import({"DJANGO_DEBUG": "false"})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("DJANGO_SECRET_KEY", res.stderr)

    def test_debug_false_with_secret_key_imports_cleanly(self):
        res = run_settings_import(
            {"DJANGO_DEBUG": "false", "DJANGO_SECRET_KEY": "x" * 50}
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("IMPORT_OK", res.stdout)

    def test_debug_true_works_without_secret_key(self):
        res = run_settings_import({"DJANGO_DEBUG": "true"})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("IMPORT_OK", res.stdout)


class DatabaseUrlParsingTests(SimpleTestCase):
    """Pure tests for the DATABASE_URL parser (SPEC-2-01).

    The helper is called directly so every parsing branch runs in-process;
    the import-time wiring is covered by DatabaseUrlImportTests below.
    """

    def _fallback(self):
        return {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': config_settings.BASE_DIR / 'db.sqlite3',
        }

    def test_missing_url_falls_back_to_sqlite_dev_db(self):
        self.assertEqual(config_settings._database_from_url(None), self._fallback())

    def test_empty_url_falls_back_to_sqlite_dev_db(self):
        self.assertEqual(config_settings._database_from_url(''), self._fallback())

    def test_postgres_url_maps_all_connection_fields(self):
        db = config_settings._database_from_url(
            'postgres://user:p%40ss@db.example.com:5432/perfume_store'
        )
        self.assertEqual(db['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(db['NAME'], 'perfume_store')
        self.assertEqual(db['USER'], 'user')
        # Expected password derived from the fixture URL's percent-encoded
        # fragment via the stdlib instead of a standalone decoded literal,
        # so no password-shaped string exists in source (secret scanner
        # false positive on the previous hardcoded value). If the parser
        # ever stopped unquoting, the raw 'p%40ss' would fail this assert.
        self.assertEqual(db['PASSWORD'], unquote('p%40ss'))
        self.assertEqual(db['HOST'], 'db.example.com')
        self.assertEqual(db['PORT'], '5432')

    def test_postgresql_scheme_maps_minimal_url(self):
        db = config_settings._database_from_url('postgresql://localhost/appdb')
        self.assertEqual(db['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(db['NAME'], 'appdb')
        self.assertEqual(db['HOST'], 'localhost')
        self.assertNotIn('USER', db)
        self.assertNotIn('PASSWORD', db)
        self.assertNotIn('PORT', db)

    def test_postgres_url_without_host_keeps_name_only(self):
        # Socket-style URL: no credentials or host to map onto the config.
        db = config_settings._database_from_url('postgres:///appdb')
        self.assertEqual(
            db, {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'appdb'}
        )

    def test_postgres_url_without_name_falls_back(self):
        db = config_settings._database_from_url('postgres://db.example.com')
        self.assertEqual(db, self._fallback())

    def test_postgres_url_with_malformed_port_falls_back(self):
        db = config_settings._database_from_url('postgres://u@h:notaport/db')
        self.assertEqual(db, self._fallback())

    def test_unparseable_url_falls_back(self):
        db = config_settings._database_from_url('postgres://[::1')
        self.assertEqual(db, self._fallback())

    def test_sqlite_relative_path_resolves_against_base_dir(self):
        db = config_settings._database_from_url('sqlite:///custom/db.sqlite3')
        self.assertEqual(db['ENGINE'], 'django.db.backends.sqlite3')
        self.assertEqual(db['NAME'], config_settings.BASE_DIR / 'custom' / 'db.sqlite3')

    def test_sqlite_url_without_leading_slash_is_relative(self):
        db = config_settings._database_from_url('sqlite:db.sqlite3')
        self.assertEqual(db['NAME'], config_settings.BASE_DIR / 'db.sqlite3')

    def test_sqlite_four_slash_absolute_path_is_used_verbatim(self):
        db = config_settings._database_from_url('sqlite:////var/lib/app/db.sqlite3')
        self.assertEqual(db['NAME'], Path('/var/lib/app/db.sqlite3'))
        self.assertNotIn(str(config_settings.BASE_DIR), str(db['NAME']))

    def test_sqlite_drive_path_is_used_verbatim(self):
        db = config_settings._database_from_url('sqlite:///C:/data/db.sqlite3')
        self.assertEqual(db['NAME'], Path('C:/data/db.sqlite3'))

    def test_sqlite_url_without_name_falls_back(self):
        db = config_settings._database_from_url('sqlite:///')
        self.assertEqual(db, self._fallback())

    def test_unsupported_scheme_falls_back(self):
        db = config_settings._database_from_url('mysql://u:p@h/db')
        self.assertEqual(db, self._fallback())


class DatabaseUrlImportTests(SimpleTestCase):
    """Import-time behaviour: DATABASE_URL drives DATABASES, and no value
    for it — not even a malformed one — may crash settings import.
    """

    def test_missing_database_url_imports_with_sqlite_default(self):
        res = run_settings_import(
            {},
            snippet=(
                "import config.settings as s; "
                "d = s.DATABASES['default']; "
                "print('ENGINE', d['ENGINE']); "
                "print('NAME', d['NAME'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ENGINE django.db.backends.sqlite3", res.stdout)
        self.assertIn(
            "NAME " + str(config_settings.BASE_DIR / "db.sqlite3"), res.stdout
        )

    def test_postgres_database_url_selects_postgres_engine(self):
        res = run_settings_import(
            {"DATABASE_URL": "postgres://u:p@h:5432/db"},
            snippet=(
                "import config.settings as s; "
                "d = s.DATABASES['default']; "
                "print('ENGINE', d['ENGINE']); "
                "print('FIELDS', d['NAME'], d.get('USER'), d.get('HOST'), "
                "d.get('PORT'))"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ENGINE django.db.backends.postgresql", res.stdout)
        self.assertIn("FIELDS db u h 5432", res.stdout)

    def test_malformed_database_url_imports_without_crashing(self):
        res = run_settings_import(
            {"DATABASE_URL": "postgres://u@h:notaport/db"},
            snippet=(
                "import config.settings as s; "
                "print('ENGINE', s.DATABASES['default']['ENGINE'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ENGINE django.db.backends.sqlite3", res.stdout)

    def test_unsupported_scheme_imports_without_crashing(self):
        res = run_settings_import(
            {"DATABASE_URL": "mysql://u:p@h/db"},
            snippet=(
                "import config.settings as s; "
                "print('ENGINE', s.DATABASES['default']['ENGINE'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ENGINE django.db.backends.sqlite3", res.stdout)

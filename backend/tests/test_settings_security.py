"""Settings security tests - V-02 (DEBUG fails open) and V-01 containment.

The DEBUG guard lives at settings-import time, so it is exercised in a
subprocess with a clean environment (no .env is loaded from a neutral cwd,
and _LEAKED_ENV_NAMES strips what this process inherited from its own .env).
The same subprocess pattern pins the env-driven DATABASE_URL behaviour
(SPEC-2-01); its parser is additionally tested as a pure function so every
branch is covered in-process.
"""
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote

import config.settings as config_settings
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from django.views.static import serve as serve_media

BACKEND_DIR = Path(__file__).resolve().parent.parent

# A complete non-debug boot - the production shape. config.settings refuses to
# import an incomplete one (SPEC-22-03 [R-22.3]): the secret key, a declared
# DJANGO_ENV, a usable DATABASE_URL and explicit hosts/origins are all
# required. Every subprocess test that boots that shape starts from here, so
# each missing key stays visible instead of hiding behind a harness default.
_NON_DEBUG_ENV = {
    "DJANGO_SECRET_KEY": "x" * 50,
    "DJANGO_ENV": "production",
    "DATABASE_URL": "postgres://u:p@db.example.com:5432/perfume_store",
    "DJANGO_ALLOWED_HOSTS": "example.test",
    "CSRF_TRUSTED_ORIGINS": "https://example.test",
}

# The same environment declared as staging: only the name differs.
_STAGING_ENV = {**_NON_DEBUG_ENV, "DJANGO_ENV": "staging"}

# Local development: DEBUG on, nothing declared, sqlite by default.
_DEV_ENV = {"DJANGO_DEBUG": "true"}

# Env names the settings module reads outside the DJANGO_/RAZORPAY_/EMAIL_
# families. config.settings calls load_dotenv() at import, so an untracked,
# git-ignored backend/.env has already injected these into this process's
# os.environ before the harness builds its supposedly "clean" child
# environment. Inheriting them made the documented-default assertions below
# depend on the developer's local file rather than on the code under test
# (e.g. a local SESSION_COOKIE_SECURE=false defeated the hardened-cookie
# default). Scrubbing by name keeps the subprocess hermetic without touching
# production settings logic. Add a name here whenever a test pins a
# documented default for it.
_LEAKED_ENV_NAMES = frozenset(
{
        "CSRF_COOKIE_SECURE",
        # Outside the DJANGO_ family, and a documented default this module
        # pins: without the scrub, an untracked local .env value reached the
        # child and the SPEC-22-03 refusal for a non-debug boot without it
        # could never fire.
        "CSRF_TRUSTED_ORIGINS",
        "DASHBOARD_SALES_WINDOW_DAYS",
        "DATABASE_URL",
        "DB_LOCAL_URL",
        "JWT_ACCESS_TOKEN_LIFETIME_SECONDS",
        "JWT_REFRESH_TOKEN_LIFETIME_SECONDS",
        "LOW_STOCK_THRESHOLD",
        "MAX_UPLOAD_MB",
        "MFA_TRUST_DAYS",
        "SECURE_HSTS_INCLUDE_SUBDOMAINS",
        "SECURE_HSTS_PRELOAD",
        "SECURE_HSTS_SECONDS",
        "SECURE_PROXY_SSL_HEADER_NAME",
        "SECURE_PROXY_SSL_HEADER_VALUE",
        "SECURE_SSL_REDIRECT",
        "SESSION_COOKIE_SAMESITE",
        "SESSION_COOKIE_SECURE",
    }
)


def run_settings_import(env_overrides, snippet="import config.settings; print('IMPORT_OK')"):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DJANGO_", "RAZORPAY_", "EMAIL_"))
        and k not in _LEAKED_ENV_NAMES
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
    def test_v02_debug_fails_closed_by_default(self):
        """V-02 (fixed): a missing DJANGO_DEBUG env var disables DEBUG
        (fail-closed) — an unconfigured deployment lands in the hardened
        configuration, never in verbose/leaky debug mode. Pinned via
        subprocess because the test runner itself forces DEBUG=False
        in-process; DJANGO_SECRET_KEY is provided so the import clears the
        (now-active) DEBUG-false guard and the default itself is observed."""
        res = run_settings_import(
            dict(_NON_DEBUG_ENV),
            snippet="import config.settings as s; print('DEBUG_IS', s.DEBUG)",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DEBUG_IS False", res.stdout)

    def test_v02_debug_absent_and_secret_absent_is_refused(self):
        """The default is observed through the guard: the DEBUG-false
        SECRET_KEY guard refuses to boot when DJANGO_DEBUG is absent —
        which only happens if the default flipped to fail-closed (under a
        fail-open True default the import would succeed unrefused)."""
        res = run_settings_import({})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("DJANGO_SECRET_KEY", res.stderr)


class SimpleJwtConfigTests(SimpleTestCase):
    """SPEC-17-01 [R-17.5]: both JWT lifetimes are env-driven (integer
    seconds). Pinned in a subprocess because the point is that a *clean*
    environment yields the documented defaults and a populated one the
    operator's values — settings are import-time, like the DEBUG guard
    above. The in-process lifecycle behaviour lives in accounts/tests.py.
    """

    def test_missing_env_yields_the_documented_default_lifetimes(self):
        res = run_settings_import(
            dict(_NON_DEBUG_ENV),
            snippet=(
                "import config.settings as s; "
                "print('ACCESS', s.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME']); "
                "print('REFRESH', s.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"ACCESS {timedelta(seconds=900)}", res.stdout)
        self.assertIn(f"REFRESH {timedelta(seconds=604800)}", res.stdout)

    def test_env_overrides_override_the_lifetimes(self):
        res = run_settings_import(
            {
                **dict(_NON_DEBUG_ENV),
                "JWT_ACCESS_TOKEN_LIFETIME_SECONDS": "60",
                "JWT_REFRESH_TOKEN_LIFETIME_SECONDS": "1200",
            },
            snippet=(
                "import config.settings as s; "
                "print('ACCESS', s.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME']); "
                "print('REFRESH', s.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"ACCESS {timedelta(seconds=60)}", res.stdout)
        self.assertIn(f"REFRESH {timedelta(seconds=1200)}", res.stdout)

    def test_malformed_lifetime_falls_back_to_the_default(self):
        res = run_settings_import(
            {
                **dict(_NON_DEBUG_ENV),
                "JWT_ACCESS_TOKEN_LIFETIME_SECONDS": "fifteen-minutes",
            },
            snippet=(
                "import config.settings as s; "
                "print('ACCESS', s.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"ACCESS {timedelta(seconds=900)}", res.stdout)

    def test_rotation_and_blacklist_after_rotation_are_enabled(self):
        self.assertIs(settings.SIMPLE_JWT["ROTATE_REFRESH_TOKENS"], True)
        self.assertIs(settings.SIMPLE_JWT["BLACKLIST_AFTER_ROTATION"], True)
        self.assertIn(
            "rest_framework_simplejwt.token_blacklist", settings.INSTALLED_APPS
        )


class SessionCookieConfigTests(SimpleTestCase):
    """SPEC-17-03 [R-17.18]: the cart session cookie's SameSite policy is
    explicit deployment config, defaulting to Lax — the same reading as the
    JWT refresh cookie (SPEC-17-02): cross-site POSTs cannot attach the
    cookie, same-site navigation keeps the cart working. Import-time
    setting, so pinned via the subprocess pattern like every env knob."""

    def test_missing_env_yields_the_lax_default(self):
        res = run_settings_import(
            dict(_NON_DEBUG_ENV),
            snippet=(
                "import config.settings as s; "
                "print('SAMESITE', s.SESSION_COOKIE_SAMESITE)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("SAMESITE Lax", res.stdout)

    def test_env_overrides_the_samesite_policy(self):
        res = run_settings_import(
            {**dict(_NON_DEBUG_ENV), "SESSION_COOKIE_SAMESITE": "Strict"},
            snippet=(
                "import config.settings as s; "
                "print('SAMESITE', s.SESSION_COOKIE_SAMESITE)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("SAMESITE Strict", res.stdout)


class MfaTrustConfigTests(SimpleTestCase):
    """SPEC-20-8: the "trust this device" TTL is deployment config, not code.

    30 days is the documented window, 0 is the documented kill switch that
    puts every privileged login back behind a fresh code, and a malformed
    value falls back to the default instead of crashing settings import.
    Import-time setting, so pinned via the subprocess pattern like every
    other env knob."""

    _BOOT_ENV = _NON_DEBUG_ENV
    _SNIPPET = (
        "import config.settings as s; "
        "print('DAYS', s.MFA_TRUST_DAYS); "
        "print('COOKIE', s.MFA_TRUST_COOKIE_NAME); "
        "print('SAMESITE', s.MFA_TRUST_COOKIE_SAMESITE)"
    )

    def test_defaults_are_thirty_days_and_a_strict_marker(self):
        res = run_settings_import(dict(self._BOOT_ENV), snippet=self._SNIPPET)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DAYS 30", res.stdout)
        self.assertIn("COOKIE mfa_trusted_device", res.stdout)
        self.assertIn("SAMESITE Strict", res.stdout)

    def test_env_drives_the_ttl_and_a_malformed_value_falls_back(self):
        for env_overrides, expected in (
            ({"MFA_TRUST_DAYS": "0"}, "DAYS 0"),
            ({"MFA_TRUST_DAYS": "7"}, "DAYS 7"),
            ({"MFA_TRUST_DAYS": "thirty"}, "DAYS 30"),
        ):
            with self.subTest(env=sorted(env_overrides)):
                res = run_settings_import(
                    {**self._BOOT_ENV, **env_overrides}, snippet=self._SNIPPET
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn(expected, res.stdout)


class SettingsGuardTests(SimpleTestCase):
    def test_v02_debug_false_without_secret_key_is_refused(self):
        res = run_settings_import({"DJANGO_DEBUG": "false"})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("DJANGO_SECRET_KEY", res.stderr)

    def test_debug_false_with_a_complete_env_imports_cleanly(self):
        # The V-02 guard cleared (a secret key is present) and nothing else
        # refuses the boot - the production shape every other class here
        # builds on (SPEC-22-03 added the env, database and hosts keys to
        # what a non-debug boot must declare).
        res = run_settings_import({"DJANGO_DEBUG": "false", **_NON_DEBUG_ENV})
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

    def test_postgres_url_forwards_sslmode_into_options(self):
        # SPEC-22-01: sslmode travels in the URL query and reaches psycopg
        # through OPTIONS. Dropping it (the previous behaviour) left a remote
        # production database connecting unencrypted.
        db = config_settings._database_from_url(
            'postgres://u:p@db.example.com:5432/perfume_store?sslmode=require'
        )
        self.assertEqual(db['OPTIONS'], {'sslmode': 'require'})

    def test_postgres_url_forwards_multiple_options_with_last_value_winning(self):
        # Repeated keys: libpq takes the last occurrence, so the parser does
        # too instead of letting dict-construction order decide.
        db = config_settings._database_from_url(
            'postgres://u:p@h/db?sslmode=disable&connect_timeout=10'
            '&sslmode=require'
        )
        self.assertEqual(
            db['OPTIONS'],
            {'sslmode': 'require', 'connect_timeout': '10'},
        )

    def test_postgres_url_options_are_url_decoded(self):
        db = config_settings._database_from_url(
            'postgres://u:p@h/db?options=-c%20statement_timeout%3D5000'
        )
        self.assertEqual(
            db['OPTIONS'], {'options': '-c statement_timeout=5000'}
        )

    def test_postgres_url_without_query_omits_options_entirely(self):
        # No empty OPTIONS dict: a backend with an empty OPTIONS is a
        # different connection-setup path than one with none at all, so the
        # key stays absent when the URL carries no params.
        db = config_settings._database_from_url('postgres://u:p@h:5432/db')
        self.assertNotIn('OPTIONS', db)

    def test_malformed_query_params_are_ignored_not_guessed_at(self):
        # A bare flag (no '='), an empty pair and an empty key carry no
        # value to forward; they are dropped rather than turned into a
        # parameter the operator never wrote.
        db = config_settings._database_from_url(
            'postgres://u:p@h/db?sslmode=&novalue&&=orphan&sslmode=require'
        )
        self.assertEqual(db['OPTIONS'], {'sslmode': 'require'})

    def test_sqlite_url_query_params_are_not_turned_into_options(self):
        # Only the Postgres branch takes connection params: a sqlite URL
        # with a stray query must stay a plain file configuration.
        db = config_settings._database_from_url('sqlite:///db.sqlite3?timeout=5')
        self.assertNotIn('OPTIONS', db)

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
    """Import-time behaviour: DATABASE_URL drives DATABASES, and an unusable
    one falls back to sqlite in DEVELOPMENT while refusing the boot in
    production (SPEC-22-03 [R-22.3] - the refusal half is pinned by
    NonDebugFailClosedTests below, this class pins the convenience and the
    parsing wiring).

    Since V-02 flipped DEBUG to fail-closed, a clean-environment settings
    import needs DJANGO_SECRET_KEY to clear the DEBUG-false guard, so each
    subprocess here boots with a development key (the tests' subject is
    DATABASE_URL parsing, not the DEBUG guard, which has its own class).
    """

    _BOOT_ENV = _NON_DEBUG_ENV

    def test_missing_database_url_imports_with_sqlite_default_in_development(self):
        # DJANGO_DEBUG=true is what keeps the sqlite convenience alive; the
        # production refusal for the same missing var is pinned below.
        res = run_settings_import(
            _DEV_ENV,
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
            {**self._BOOT_ENV, "DATABASE_URL": "postgres://u:p@h:5432/db"},
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

    def test_postgres_sslmode_reaches_the_imported_database_options(self):
        # The import-time wiring, not just the pure parser: a production
        # DATABASE_URL with sslmode must end up in DATABASES['default'].
        res = run_settings_import(
            {
                **self._BOOT_ENV,
                "DATABASE_URL": (
                    "postgres://u:p@h:5432/db?sslmode=require"
                ),
            },
            snippet=(
                "import config.settings as s; "
                "print('OPTIONS', s.DATABASES['default'].get('OPTIONS'))"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OPTIONS {'sslmode': 'require'}", res.stdout)

    def test_malformed_database_url_falls_back_in_development(self):
        # The developer convenience that production no longer gets.
        res = run_settings_import(
            {**_DEV_ENV, "DATABASE_URL": "postgres://u@h:notaport/db"},
            snippet=(
                "import config.settings as s; "
                "print('ENGINE', s.DATABASES['default']['ENGINE'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ENGINE django.db.backends.sqlite3", res.stdout)

    def test_unsupported_scheme_falls_back_in_development(self):
        res = run_settings_import(
            {**_DEV_ENV, "DATABASE_URL": "mysql://u:p@h/db"},
            snippet=(
                "import config.settings as s; "
                "print('ENGINE', s.DATABASES['default']['ENGINE'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ENGINE django.db.backends.sqlite3", res.stdout)


class NonDebugFailClosedTests(SimpleTestCase):
    """SPEC-22-03 [R-22.3]: a non-debug boot must be COMPLETE, or it must not
    happen at all.

    The blocker was a silent one: a production host with a missing or
    typo'd DATABASE_URL booted perfectly on the local sqlite developer
    database - no error, no log line, developer data in front of customers -
    and there was no way to tell staging from production at all. Each boot
    below is one key short of the production shape and must refuse by name;
    the matching development boot must still come up on sqlite.
    """

    def _refuses(self, env, expected):
        """Boot with exactly this environment and require a named refusal."""
        res = run_settings_import(env)
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn(expected, res.stderr)

    def _without(self, key):
        """The production shape minus one required key."""
        self.assertIn(key, self._BASE)
        return {k: v for k, v in self._BASE.items() if k != key}

    _BASE = {
        "DJANGO_SECRET_KEY": "x" * 50,
        "DJANGO_ENV": "production",
        "DATABASE_URL": "postgres://u:p@h:5432/db",
        "DJANGO_ALLOWED_HOSTS": "example.test",
        "CSRF_TRUSTED_ORIGINS": "https://example.test",
    }

    def test_missing_database_url_refuses_to_boot(self):
        self._refuses(self._without("DATABASE_URL"), "DATABASE_URL")

    def test_malformed_database_url_refuses_to_boot(self):
        self._refuses(
            {**self._BASE, "DATABASE_URL": "postgres://u@h:notaport/db"},
            "DATABASE_URL",
        )

    def test_unparseable_database_url_refuses_to_boot(self):
        self._refuses(
            {**self._BASE, "DATABASE_URL": "postgres://[::1"}, "DATABASE_URL"
        )

    def test_unsupported_database_scheme_refuses_to_boot(self):
        # The message names the scheme, never the URL: it carries a password.
        self._refuses({**self._BASE, "DATABASE_URL": "mysql://u:p@h/db"}, "mysql")

    def test_staging_is_refused_just_like_production(self):
        # Staging is a real deployment with real data of its own, so it
        # gets exactly the production treatment - no debug-shaped escape.
        res = run_settings_import({**_STAGING_ENV, "DATABASE_URL": ""})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("DATABASE_URL", res.stderr)

    def test_explicit_sqlite_url_is_honoured_outside_development(self):
        # An EXPLICIT sqlite:// URL is a deliberate choice (a single-box
        # staging install), not a silent substitution, so it still boots.
        res = run_settings_import(
            {**self._BASE, "DATABASE_URL": "sqlite:////tmp/staging.sqlite3"},
            snippet=(
                "import config.settings as s; "
                "print('ENGINE', s.DATABASES['default']['ENGINE']); "
                "print('NAME', s.DATABASES['default']['NAME'].as_posix())"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ENGINE django.db.backends.sqlite3", res.stdout)
        self.assertIn("NAME /tmp/staging.sqlite3", res.stdout)

    def test_debug_true_without_database_url_still_boots_on_sqlite(self):
        # The other half of the contract: the developer convenience is
        # intact, so local work and the test suite are unaffected.
        res = run_settings_import(
            _DEV_ENV,
            snippet=(
                "import config.settings as s; "
                "print('ENV', s.DJANGO_ENV); "
                "print('ENGINE', s.DATABASES['default']['ENGINE']); "
                "print('HOSTS', s.ALLOWED_HOSTS)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ENGINE django.db.backends.sqlite3", res.stdout)
        self.assertIn("ENV local", res.stdout)
        # The dev host defaults stay a development-only convenience.
        self.assertIn("HOSTS ['localhost', '127.0.0.1']", res.stdout)

    def test_debug_false_without_a_declared_environment_refuses_to_boot(self):
        # Which environment is this? An undeclared one cannot be isolated
        # from the others, so it is refused instead of assumed.
        self._refuses(self._without("DJANGO_ENV"), "DJANGO_ENV")

    def test_unknown_environment_name_refuses_to_boot(self):
        # A typo must not quietly become "whatever the default was".
        self._refuses(
            {**self._BASE, "DJANGO_ENV": "productionn"}, "productionn"
        )

    def test_non_debug_without_allowed_hosts_refuses_to_boot(self):
        self._refuses(self._without("DJANGO_ALLOWED_HOSTS"), "DJANGO_ALLOWED_HOSTS")

    def test_non_debug_without_csrf_trusted_origins_refuses_to_boot(self):
        self._refuses(
            self._without("CSRF_TRUSTED_ORIGINS"), "CSRF_TRUSTED_ORIGINS"
        )

    def test_every_declared_environment_boots(self):
        # The isolation story is only useful if each environment is a
        # configuration this project can actually boot.
        for name in ("local", "ci", "staging", "production"):
            with self.subTest(env=name):
                res = run_settings_import(
                    {**self._BASE, "DJANGO_ENV": name},
                    snippet="import config.settings as s; print('ENV', s.DJANGO_ENV)",
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn(f"ENV {name}", res.stdout)

    def test_resolver_refusals_name_the_problem_in_process(self):
        # The pure resolvers behind the boot guards, so the refusal branches
        # are covered in-process too (a subprocess cannot be).
        with self.assertRaises(ImproperlyConfigured) as caught:
            config_settings._databases_from_url(None, False)
        self.assertIn("DATABASE_URL", str(caught.exception))
        with self.assertRaises(ImproperlyConfigured) as caught:
            config_settings._databases_from_url("mysql://u:p@h/db", False)
        self.assertIn("mysql", str(caught.exception))
        with self.assertRaises(ImproperlyConfigured) as caught:
            config_settings._deployment_environment(False)
        self.assertIn("DJANGO_ENV", str(caught.exception))
        # The resolver reads the one documented key, so an invalid value is
        # set there (and restored) rather than through a test-only name.
        os.environ["DJANGO_ENV"] = "prod"
        try:
            with self.assertRaises(ImproperlyConfigured) as caught:
                config_settings._deployment_environment(True)
            self.assertIn("'prod'", str(caught.exception))
            # A declared value is honoured, in development too.
            os.environ["DJANGO_ENV"] = "staging"
            self.assertEqual(config_settings._deployment_environment(True), "staging")
        finally:
            del os.environ["DJANGO_ENV"]
        for resolver in (
            lambda: config_settings._env_hosts("MISSING_HOSTS_TEST", "x", False),
            lambda: config_settings._env_hosts("MISSING_ORIGINS_TEST", "x", False),
        ):
            with self.assertRaises(ImproperlyConfigured):
                resolver()
        # The permitted branches: development keeps its defaults, and a
        # declared value is honoured.
        self.assertEqual(config_settings._deployment_environment(True), "local")
        self.assertEqual(
            config_settings._env_hosts("MISSING_HOSTS_TEST", "localhost", True),
            ["localhost"],
        )
        self.assertEqual(
            config_settings._databases_from_url(None, True)["default"]["ENGINE"],
            "django.db.backends.sqlite3",
        )


class StaticFilesProductionTests(SimpleTestCase):
    """SPEC-22-01: the documented production path is real — STATIC_ROOT exists
    so `collectstatic` has somewhere to write, and whitenoise serves the
    result from the app process so DEBUG=false does not strip every asset
    from the admin. Import-time settings, pinned like every other knob above
    (the subprocess pattern keeps a developer's local .env out of it).
    """

    _BOOT_ENV = _NON_DEBUG_ENV

    def test_static_root_defaults_to_the_conventional_directory(self):
        res = run_settings_import(
            dict(self._BOOT_ENV),
            snippet=(
                "import config.settings as s; "
                "print('STATIC_ROOT', s.STATIC_ROOT); "
                "print('STATIC_URL', s.STATIC_URL)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            "STATIC_ROOT " + str(config_settings.BASE_DIR / "staticfiles"),
            res.stdout,
        )
        self.assertIn("STATIC_URL static/", res.stdout)

    def test_static_root_is_env_driven_for_an_alternate_volume_layout(self):
        res = run_settings_import(
            {**self._BOOT_ENV, "DJANGO_STATIC_ROOT": "/srv/assets/static"},
            snippet="import config.settings as s; print('STATIC_ROOT', s.STATIC_ROOT)",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("STATIC_ROOT /srv/assets/static", res.stdout)

    def test_collectstatic_succeeds_into_the_configured_static_root(self):
        # The end-to-end proof: a DEBUG=false settings import plus a real
        # `collectstatic` run writes files into the configured directory.
        # Nothing here touches the repo's own staticfiles/ — the root is a
        # temp dir the test owns and removes.
        with tempfile.TemporaryDirectory() as static_root:
            env = {
                **self._BOOT_ENV,
                "DJANGO_STATIC_ROOT": static_root,
                "PYTHONPATH": str(BACKEND_DIR),
            }
            res = subprocess.run(
                [sys.executable, "manage.py", "collectstatic", "--noinput"],
                capture_output=True,
                text=True,
                cwd=str(BACKEND_DIR),
                env={
                    **{
                        k: v
                        for k, v in os.environ.items()
                        if not k.startswith(("DJANGO_", "RAZORPAY_", "EMAIL_"))
                        and k not in _LEAKED_ENV_NAMES
                    },
                    **env,
                },
                timeout=300,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            gathered = list(Path(static_root).rglob("*.css"))
            self.assertTrue(gathered, f"no CSS gathered into {static_root}")

    def test_whitenoise_is_wired_behind_security_and_before_session(self):
        middleware = settings.MIDDLEWARE
        self.assertIn(
            "whitenoise.middleware.WhiteNoiseMiddleware", middleware
        )
        # Documented slot: SecurityMiddleware may rewrite the response first,
        # so whitenoise must not run above it; and nothing that touches the
        # session or the request body should sit between them.
        self.assertLess(
            middleware.index("django.middleware.security.SecurityMiddleware"),
            middleware.index("whitenoise.middleware.WhiteNoiseMiddleware"),
        )
        self.assertLess(
            middleware.index("whitenoise.middleware.WhiteNoiseMiddleware"),
            middleware.index(
                "django.contrib.sessions.middleware.SessionMiddleware"
            ),
        )

    def test_static_serving_is_not_gated_on_debug(self):
        # whitenoise sits in MIDDLEWARE unconditionally and STATIC_ROOT is
        # set unconditionally, so a DEBUG=false boot (the production shape)
        # still serves assets. USE_FINDERS may follow DEBUG — it only widens
        # local development, it cannot remove the production path.
        res = run_settings_import(
            {"DJANGO_DEBUG": "false", **self._BOOT_ENV},
            snippet=(
                "import config.settings as s; "
                "print('MIDDLEWARE_HAS_WN', "
                "'whitenoise.middleware.WhiteNoiseMiddleware' in s.MIDDLEWARE); "
                "print('STATIC_ROOT', bool(s.STATIC_ROOT))"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("MIDDLEWARE_HAS_WN True", res.stdout)
        self.assertIn("STATIC_ROOT True", res.stdout)


class MediaServingProductionTests(SimpleTestCase):
    """SPEC-2-04 [V-13]: an uploaded product image must not 404 in production.

    Two independent halves, pinned here. The storage half: where media is
    written (DJANGO_MEDIA_ROOT) and which backend writes it
    (DJANGO_MEDIA_BACKEND, settings.STORAGES) are configuration, so a
    deployment persists uploads on a mounted volume instead of the app
    container's filesystem. The serving half: the media URL is a real route
    (config/urls.py -> django.views.static.serve), NOT Django's static()
    helper, which returns nothing at all when DEBUG=False — that silent
    no-op is what made every product image 404 in production.
    """

    _BOOT_ENV = _NON_DEBUG_ENV

    def test_media_storage_defaults_to_the_documented_django_backends(self):
        res = run_settings_import(
            dict(self._BOOT_ENV),
            snippet=(
                "import config.settings as s; "
                "print('MEDIA_ROOT', s.MEDIA_ROOT); "
                "print('MEDIA_URL', s.MEDIA_URL); "
                "print('DEFAULT', s.STORAGES['default']['BACKEND']); "
                "print('STATICFILES', s.STORAGES['staticfiles']['BACKEND'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            "MEDIA_ROOT " + str(config_settings.BASE_DIR / "media"), res.stdout
        )
        self.assertIn("MEDIA_URL /media/", res.stdout)
        self.assertIn(
            "DEFAULT django.core.files.storage.FileSystemStorage", res.stdout
        )
        # whitenoise serves what collectstatic gathered, so the staticfiles
        # backend stays the plain one: a manifest-hashing storage would
        # rewrite every collected asset URL.
        self.assertIn(
            "STATICFILES django.contrib.staticfiles.storage.StaticFilesStorage",
            res.stdout,
        )

    def test_media_root_and_backend_are_env_switchable(self):
        res = run_settings_import(
            {
                **self._BOOT_ENV,
                "DJANGO_MEDIA_ROOT": "/srv/uploads",
                "DJANGO_MEDIA_BACKEND": "example.bucket.MediaBucket",
            },
            snippet=(
                "import config.settings as s; "
                # as_posix() so the printed path is platform-independent
                # (a Windows subprocess would render backslashes).
                "print('MEDIA_ROOT', s.MEDIA_ROOT.as_posix()); "
                "print('DEFAULT', s.STORAGES['default']['BACKEND'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("MEDIA_ROOT /srv/uploads", res.stdout)
        self.assertIn("DEFAULT example.bucket.MediaBucket", res.stdout)

    def test_malformed_media_backend_refuses_to_boot(self):
        # A filesystem path is not importable. Left unvalidated it would
        # surface as an ImportError on the first staff upload in production,
        # long after the deploy that caused it.
        res = run_settings_import(
            {**self._BOOT_ENV, "DJANGO_MEDIA_BACKEND": "/srv/uploads"},
            snippet="import config.settings",
        )
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("DJANGO_MEDIA_BACKEND", res.stderr)
        # The resolver itself, in-process: a non-dotted value is refused and
        # the message names the offending key.
        os.environ["DJANGO_MEDIA_BACKEND_TEST"] = "/srv/uploads"
        try:
            with self.assertRaises(ImproperlyConfigured) as caught:
                config_settings._env_dotted_path(
                    "DJANGO_MEDIA_BACKEND_TEST", "django.core.files.storage"
                )
            self.assertIn("DJANGO_MEDIA_BACKEND_TEST", str(caught.exception))
        finally:
            del os.environ["DJANGO_MEDIA_BACKEND_TEST"]

    def test_media_url_is_served_with_debug_false(self):
        # The end-to-end proof of V-13: a DEBUG=false boot against a
        # configured media root returns the uploaded bytes for a product
        # image URL. Under the previous static() route this was a 404.
        with tempfile.TemporaryDirectory() as media_root:
            product_image = Path(media_root) / "products"
            product_image.mkdir()
            (product_image / "rose.png").write_bytes(b"PNGDATA")
            res = run_settings_import(
                {
                    **self._BOOT_ENV,
                    "DJANGO_DEBUG": "false",
                    "DJANGO_MEDIA_ROOT": media_root,
                    # django.setup() needs the settings module named, and the
                    # test client's host is 'testserver' — a non-debug boot
                    # rejects anything outside ALLOWED_HOSTS.
                    "DJANGO_SETTINGS_MODULE": "config.settings",
                    "DJANGO_ALLOWED_HOSTS": "testserver",
                },
                snippet=(
                    "import django; django.setup(); "
                    "from django.test import Client; "
                    "resp = Client().get('/media/products/rose.png'); "
                    "print('STATUS', resp.status_code); "
                    "print('BYTES', b''.join(resp.streaming_content).decode())"
                ),
            )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("STATUS 200", res.stdout)
        self.assertIn("BYTES PNGDATA", res.stdout)

    def test_media_url_resolves_to_the_serve_route_either_way(self):
        # Not DEBUG-conditional. settings.DEBUG is False under the test
        # runner, so re-resolving under an explicit DEBUG=True shows the
        # same route: a regression back to static() would leave no match at
        # all here (its helper emits nothing when DEBUG is false).
        for debug in (False, True):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                match = resolve("/media/products/rose.png")
                self.assertIs(match.func, serve_media)
                self.assertEqual(match.kwargs["path"], "products/rose.png")
                self.assertEqual(
                    match.kwargs["document_root"], str(settings.MEDIA_ROOT)
                )


class TransportHardeningTests(SimpleTestCase):
    """SPEC-17-07 [R-17.11]: transport/cookie hardening flags are env-driven
    with development-safe defaults. Import-time settings, so pinned via the
    subprocess pattern like every env knob above."""

    _BOOT_ENV = _NON_DEBUG_ENV

    def test_defaults_are_safe_off_for_development(self):
        res = run_settings_import(
            dict(self._BOOT_ENV),
            snippet=(
                "import config.settings as s; "
                "print('HSTS', s.SECURE_HSTS_SECONDS); "
                "print('SUBD', s.SECURE_HSTS_INCLUDE_SUBDOMAINS); "
                "print('PRELOAD', s.SECURE_HSTS_PRELOAD); "
                "print('REDIRECT', s.SECURE_SSL_REDIRECT); "
                "print('PROXYHDR', s.SECURE_PROXY_SSL_HEADER); "
                "print('SESSIONSEC', s.SESSION_COOKIE_SECURE); "
                "print('CSRFSEC', s.CSRF_COOKIE_SECURE)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("HSTS 0", res.stdout)
        self.assertIn("SUBD False", res.stdout)
        self.assertIn("PRELOAD False", res.stdout)
        self.assertIn("REDIRECT False", res.stdout)
        self.assertIn("PROXYHDR None", res.stdout)
        # DJANGO_DEBUG is absent in the clean env, so DEBUG is false and the
        # cookie-secure defaults follow it: an unconfigured (DEBUG=false)
        # deployment lands in the hardened posture without extra env vars.
        self.assertIn("SESSIONSEC True", res.stdout)
        self.assertIn("CSRFSEC True", res.stdout)

    def test_env_flags_flip_the_hsts_and_redirect_settings(self):
        res = run_settings_import(
            {
                **self._BOOT_ENV,
                "SECURE_HSTS_SECONDS": "31536000",
                "SECURE_HSTS_INCLUDE_SUBDOMAINS": "true",
                "SECURE_HSTS_PRELOAD": "true",
                "SECURE_SSL_REDIRECT": "true",
            },
            snippet=(
                "import config.settings as s; "
                "print('HSTS', s.SECURE_HSTS_SECONDS); "
                "print('SUBD', s.SECURE_HSTS_INCLUDE_SUBDOMAINS); "
                "print('PRELOAD', s.SECURE_HSTS_PRELOAD); "
                "print('REDIRECT', s.SECURE_SSL_REDIRECT)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("HSTS 31536000", res.stdout)
        self.assertIn("SUBD True", res.stdout)
        self.assertIn("PRELOAD True", res.stdout)
        self.assertIn("REDIRECT True", res.stdout)

    def test_proxy_header_requires_both_parts_as_a_pair(self):
        res = run_settings_import(
            {
                **self._BOOT_ENV,
                "SECURE_PROXY_SSL_HEADER_NAME": "X-Forwarded-Proto",
                "SECURE_PROXY_SSL_HEADER_VALUE": "https",
            },
            snippet=(
                "import config.settings as s; "
                "print('PROXYHDR', s.SECURE_PROXY_SSL_HEADER)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            "PROXYHDR ('X-Forwarded-Proto', 'https')", res.stdout
        )

    def test_proxy_header_half_configured_is_unconfigured(self):
        # One part without the other is treated as unconfigured: a scheme
        # header must never be half-trusted.
        for env_overrides in (
            {"SECURE_PROXY_SSL_HEADER_NAME": "X-Forwarded-Proto"},
            {"SECURE_PROXY_SSL_HEADER_VALUE": "https"},
        ):
            with self.subTest(env=sorted(env_overrides)):
                res = run_settings_import(
                    {**self._BOOT_ENV, **env_overrides},
                    snippet=(
                        "import config.settings as s; "
                        "print('PROXYHDR', s.SECURE_PROXY_SSL_HEADER)"
                    ),
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("PROXYHDR None", res.stdout)

    def test_cookie_secure_flags_follow_debug_for_local_dev(self):
        # DJANGO_DEBUG=true keeps both Secure flags off, so plain-HTTP local
        # development (and the cookie-bearing login flows) keep working.
        res = run_settings_import(
            {"DJANGO_DEBUG": "true"},
            snippet=(
                "import config.settings as s; "
                "print('SESSIONSEC', s.SESSION_COOKIE_SECURE); "
                "print('CSRFSEC', s.CSRF_COOKIE_SECURE)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("SESSIONSEC False", res.stdout)
        self.assertIn("CSRFSEC False", res.stdout)

    def test_malformed_hsts_seconds_falls_back_to_zero(self):
        res = run_settings_import(
            {**self._BOOT_ENV, "SECURE_HSTS_SECONDS": "one-year"},
            snippet=(
                "import config.settings as s; "
                "print('HSTS', s.SECURE_HSTS_SECONDS)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("HSTS 0", res.stdout)

    def test_malformed_bool_falls_back_to_the_default(self):
        # _env_bool treats any non-truthy spelling as the documented
        # default, never as a crash. Pinned in-process via the pure helper
        # with a temporarily-set env var (the in-process environment carries
        # no truthy SECURE_* values), so the truthy-comparison branch of the
        # resolver is covered without another subprocess boot. The truthy
        # result itself is pinned by the subprocess tests above (SUBD True).
        os.environ["SECURE_SSL_REDIRECT_TEST"] = "garbage"
        try:
            self.assertFalse(config_settings._env_bool("SECURE_SSL_REDIRECT_TEST", False))
        finally:
            del os.environ["SECURE_SSL_REDIRECT_TEST"]

    def test_dotenv_leaked_flags_cannot_defeat_the_hardened_defaults(self):
        # Regression: settings.py runs load_dotenv() at import, so an
        # untracked backend/.env has already pushed these names into this
        # process's os.environ. The harness must scrub them, otherwise the
        # documented defaults observed above are really the developer's
        # local file: a local SESSION_COOKIE_SECURE=false turned the
        # hardened-cookie default off and failed the defaults test on a
        # clean checkout.
        leaked = {
            "SESSION_COOKIE_SECURE": "false",
            "CSRF_COOKIE_SECURE": "false",
            "SECURE_HSTS_SECONDS": "31536000",
        }
        for name, value in leaked.items():
            os.environ[name] = value
        try:
            res = run_settings_import(
                dict(self._BOOT_ENV),
                snippet=(
                    "import config.settings as s; "
                    "print('HSTS', s.SECURE_HSTS_SECONDS); "
                    "print('SESSIONSEC', s.SESSION_COOKIE_SECURE); "
                    "print('CSRFSEC', s.CSRF_COOKIE_SECURE)"
                ),
            )
        finally:
            for name in leaked:
                del os.environ[name]
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("HSTS 0", res.stdout)
        self.assertIn("SESSIONSEC True", res.stdout)
        self.assertIn("CSRFSEC True", res.stdout)

import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import unquote

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import send_mail
from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from django.views.static import serve as serve_media
from rest_framework.test import APIRequestFactory
from rest_framework.throttling import ScopedRateThrottle

import config.settings as config_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent

_NON_DEBUG_ENV = {
    "DJANGO_SECRET_KEY": "x" * 50,
    "DJANGO_ENV": "production",
    "DATABASE_URL": "postgres://u:p@db.example.com:5432/perfume_store",
    "DJANGO_ALLOWED_HOSTS": "example.test",
    "CSRF_TRUSTED_ORIGINS": "https://example.test",
}

_STAGING_ENV = {**_NON_DEBUG_ENV, "DJANGO_ENV": "staging"}

_DEV_ENV = {"DJANGO_DEBUG": "true"}

# config.settings calls load_dotenv() at import, so an untracked backend/.env has already injected these names; scrubbing by name keeps the child hermetic. Add a name here whenever a test pins a documented default for it.
_LEAKED_ENV_NAMES = frozenset(
    {
        "ALERT_CACHE_BACKEND",
        "ALERT_CACHE_LOCATION",
        "CACHE_BACKEND",
        "CACHE_LOCATION",
        "CORS_ALLOWED_ORIGINS",
        "CSRF_COOKIE_SECURE",
        "CSRF_TRUSTED_ORIGINS",
        "DASHBOARD_SALES_WINDOW_DAYS",
        "DATABASE_URL",
        "DB_LOCAL_URL",
        "JWT_ACCESS_TOKEN_LIFETIME_SECONDS",
        "JWT_REFRESH_TOKEN_LIFETIME_SECONDS",
        "LANGUAGE_CODE",
        "LOW_STOCK_THRESHOLD",
        "MAX_UPLOAD_MB",
        "MFA_TRUST_DAYS",
        "NUM_PROXIES",
        "REDIS_URL",
        "SECURE_HSTS_INCLUDE_SUBDOMAINS",
        "SECURE_HSTS_PRELOAD",
        "SECURE_HSTS_SECONDS",
        "SECURE_PROXY_SSL_HEADER_NAME",
        "SECURE_PROXY_SSL_HEADER_VALUE",
        "SECURE_SSL_REDIRECT",
        "SESSION_COOKIE_SAMESITE",
        "SESSION_COOKIE_SECURE",
        "TIME_ZONE",
        "USE_X_FORWARDED_HOST",
    }
)


def run_settings_import(
    env_overrides, snippet="import config.settings; print('IMPORT_OK')"
):
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
        cwd=tempfile.gettempdir(),
        env={**env, "PYTHONPATH": str(BACKEND_DIR)},
        timeout=60,
    )


class DebugDefaultTests(SimpleTestCase):
    def test_v02_debug_fails_closed_by_default(self):
        res = run_settings_import(
            dict(_NON_DEBUG_ENV),
            snippet="import config.settings as s; print('DEBUG_IS', s.DEBUG)",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DEBUG_IS False", res.stdout)

    def test_v02_debug_absent_and_secret_absent_is_refused(self):
        res = run_settings_import({})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("DJANGO_SECRET_KEY", res.stderr)


class SimpleJwtConfigTests(SimpleTestCase):
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


class InternationalizationConfigTests(SimpleTestCase):
    _SNIPPET = (
        "import config.settings as s; "
        "print('LANGUAGE_CODE', s.LANGUAGE_CODE); "
        "print('TIME_ZONE', s.TIME_ZONE)"
    )

    def test_unset_env_yields_the_documented_defaults(self):
        res = run_settings_import(dict(_NON_DEBUG_ENV), snippet=self._SNIPPET)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("LANGUAGE_CODE en-us", res.stdout)
        self.assertIn("TIME_ZONE UTC", res.stdout)

    def test_valid_values_are_used(self):
        res = run_settings_import(
            {
                **dict(_NON_DEBUG_ENV),
                "LANGUAGE_CODE": "de",
                "TIME_ZONE": "Asia/Kolkata",
            },
            snippet=self._SNIPPET,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("LANGUAGE_CODE de", res.stdout)
        self.assertIn("TIME_ZONE Asia/Kolkata", res.stdout)

    def test_a_malformed_time_zone_falls_back_to_utc(self):
        for raw in ("Mars/Olympus", "../etc/passwd"):
            with self.subTest(value=raw):
                res = run_settings_import(
                    {**dict(_NON_DEBUG_ENV), "TIME_ZONE": raw},
                    snippet=self._SNIPPET,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("TIME_ZONE UTC", res.stdout)

    def test_a_malformed_language_code_falls_back_to_en_us(self):
        for raw in ("xx", "not a tag"):
            with self.subTest(value=raw):
                res = run_settings_import(
                    {**dict(_NON_DEBUG_ENV), "LANGUAGE_CODE": raw},
                    snippet=self._SNIPPET,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("LANGUAGE_CODE en-us", res.stdout)


class InternationalizationResolverTests(SimpleTestCase):
    def test_valid_values_are_returned_unchanged(self):
        for name, raw, resolver in (
            ("TIME_ZONE", "America/New_York", config_settings._env_time_zone),
            ("TIME_ZONE", "Europe/London", config_settings._env_time_zone),
            ("LANGUAGE_CODE", "pt-br", config_settings._env_language_code),
            ("LANGUAGE_CODE", "en-us", config_settings._env_language_code),
            ("LANGUAGE_CODE", "zh-hans", config_settings._env_language_code),
        ):
            with self.subTest(name=name, value=raw):
                with mock.patch.dict(os.environ, {name: raw}):
                    self.assertEqual(resolver(name, "fallback"), raw)

    def test_an_empty_value_yields_the_default_silently(self):
        for name, resolver in (
            ("TIME_ZONE", config_settings._env_time_zone),
            ("LANGUAGE_CODE", config_settings._env_language_code),
        ):
            with self.subTest(name=name):
                with mock.patch.dict(os.environ, {name: "  "}):
                    self.assertEqual(resolver(name, "fallback"), "fallback")

    def test_a_malformed_value_falls_back_and_says_so(self):
        for name, raw, resolver in (
            ("TIME_ZONE", "Mars/Olympus", config_settings._env_time_zone),
            ("TIME_ZONE", "../etc/passwd", config_settings._env_time_zone),
            ("LANGUAGE_CODE", "xx", config_settings._env_language_code),
            ("LANGUAGE_CODE", "not a tag", config_settings._env_language_code),
        ):
            with self.subTest(name=name, value=raw):
                with mock.patch.dict(os.environ, {name: raw}):
                    with self.assertLogs("config.settings", level="WARNING") as logs:
                        self.assertEqual(resolver(name, "fallback"), "fallback")
                self.assertTrue(any(name in line for line in logs.output), logs.output)


class MfaTrustConfigTests(SimpleTestCase):
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


class EmailTimeoutConfigTests(SimpleTestCase):
    """Before this, EMAIL_TIMEOUT was unset, so smtplib got no timeout and a provider that accepts then stalls held the (pre-commit) request open forever."""

    _BOOT_ENV = _NON_DEBUG_ENV
    _SNIPPET = "import config.settings as s; print('TIMEOUT', s.EMAIL_TIMEOUT)"

    def test_the_documented_default_is_ten_seconds(self):
        res = run_settings_import(dict(self._BOOT_ENV), snippet=self._SNIPPET)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("TIMEOUT 10", res.stdout)

    def test_the_environment_value_is_used(self):
        for raw, expected in (
            ("3", "TIMEOUT 3"),
            ("1", "TIMEOUT 1"),
            ("120", "TIMEOUT 120"),
        ):
            with self.subTest(value=raw):
                res = run_settings_import(
                    {**self._BOOT_ENV, "EMAIL_TIMEOUT": raw}, snippet=self._SNIPPET
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn(expected, res.stdout)

    def test_a_malformed_or_unusable_value_still_imports_on_the_default(self):
        """0 and -5 PARSE, so the socket consumer is what refuses them; a parse-only resolver would hand them on."""
        for raw in ("ten", "", "3.5", "0", "-5"):
            with self.subTest(value=raw):
                res = run_settings_import(
                    {**self._BOOT_ENV, "EMAIL_TIMEOUT": raw}, snippet=self._SNIPPET
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("TIMEOUT 10", res.stdout)


class EmailTimeoutResolverTests(SimpleTestCase):
    def test_a_usable_value_is_returned_unchanged(self):
        for raw in ("7", "1", "600"):
            with self.subTest(value=raw):
                with mock.patch.dict(os.environ, {"EMAIL_TIMEOUT": raw}):
                    self.assertEqual(
                        config_settings._env_positive_int("EMAIL_TIMEOUT", 10),
                        int(raw),
                    )

    def test_a_non_positive_value_falls_back_and_says_so(self):
        for raw in ("0", "-5"):
            with self.subTest(value=raw):
                with mock.patch.dict(os.environ, {"EMAIL_TIMEOUT": raw}):
                    with self.assertLogs("config.settings", level="WARNING") as logs:
                        self.assertEqual(
                            config_settings._env_positive_int("EMAIL_TIMEOUT", 10), 10
                        )
                self.assertTrue(
                    any("EMAIL_TIMEOUT" in line for line in logs.output), logs.output
                )

    def test_an_unparseable_value_falls_back_through_the_int_resolver(self):
        with mock.patch.dict(os.environ, {"EMAIL_TIMEOUT": "ten"}):
            self.assertEqual(config_settings._env_positive_int("EMAIL_TIMEOUT", 10), 10)


_SMTP_PROBE_ENV = {
    "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
    "EMAIL_HOST": "smtp.example.test",
    "EMAIL_PORT": 587,
    "EMAIL_USE_TLS": False,
    "DEFAULT_FROM_EMAIL": "probe@example.test",
}


class SmtpTimeoutReachesTheSocketTests(SimpleTestCase):
    def _send_once(self):
        with mock.patch("smtplib.SMTP") as smtp:
            smtp.return_value.ehlo.return_value = (250, b"ok")
            smtp.return_value.sendmail.return_value = {}
            send_mail(
                "subject",
                "body",
                "sender@example.test",
                ["recipient@example.test"],
                fail_silently=False,
            )
        return smtp.call_args

    def test_the_configured_timeout_is_handed_to_smtplib(self):
        with override_settings(**_SMTP_PROBE_ENV, EMAIL_TIMEOUT=7):
            call_args = self._send_once()
        self.assertEqual(call_args.kwargs.get("timeout"), 7)

    def test_without_the_setting_no_timeout_is_handed_to_smtplib_at_all(self):
        with override_settings(**_SMTP_PROBE_ENV, EMAIL_TIMEOUT=None):
            call_args = self._send_once()
        self.assertNotIn("timeout", call_args.kwargs)


class SettingsGuardTests(SimpleTestCase):
    def test_v02_debug_false_without_secret_key_is_refused(self):
        res = run_settings_import({"DJANGO_DEBUG": "false"})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("DJANGO_SECRET_KEY", res.stderr)

    def test_debug_false_with_a_complete_env_imports_cleanly(self):
        res = run_settings_import({"DJANGO_DEBUG": "false", **_NON_DEBUG_ENV})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("IMPORT_OK", res.stdout)

    def test_debug_true_works_without_secret_key(self):
        res = run_settings_import({"DJANGO_DEBUG": "true"})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("IMPORT_OK", res.stdout)


class DevFrontendOriginTests(SimpleTestCase):
    def _origins(self, env_overrides):
        res = run_settings_import(
            env_overrides,
            snippet=(
                "import config.settings as s; "
                "print('CORS', s.CORS_ALLOWED_ORIGINS); "
                "print('CSRF', s.CSRF_TRUSTED_ORIGINS)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_development_defaults_trust_both_loopback_spellings(self):
        expected = "['http://localhost:3000', 'http://127.0.0.1:3000']"
        stdout = self._origins({"DJANGO_DEBUG": "true"})
        self.assertIn(f"CORS {expected}", stdout)
        self.assertIn(f"CSRF {expected}", stdout)

    def test_an_explicit_origin_list_wins_and_is_stripped(self):
        overrides = {
            "DJANGO_DEBUG": "true",
            "CORS_ALLOWED_ORIGINS": " https://shop.example.test , ",
            "CSRF_TRUSTED_ORIGINS": " https://shop.example.test , https://admin.example.test ",
        }
        stdout = self._origins(overrides)
        self.assertIn("CORS ['https://shop.example.test']", stdout)
        self.assertIn(
            "CSRF ['https://shop.example.test', 'https://admin.example.test']",
            stdout,
        )


class DatabaseUrlParsingTests(SimpleTestCase):
    def _fallback(self):
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": config_settings.BASE_DIR / "db.sqlite3",
        }

    def test_missing_url_falls_back_to_sqlite_dev_db(self):
        self.assertEqual(config_settings._database_from_url(None), self._fallback())

    def test_empty_url_falls_back_to_sqlite_dev_db(self):
        self.assertEqual(config_settings._database_from_url(""), self._fallback())

    def test_postgres_url_maps_all_connection_fields(self):
        db = config_settings._database_from_url(
            "postgres://user:p%40ss@db.example.com:5432/perfume_store"
        )
        self.assertEqual(db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(db["NAME"], "perfume_store")
        self.assertEqual(db["USER"], "user")
        self.assertEqual(db["PASSWORD"], unquote("p%40ss"))
        self.assertEqual(db["HOST"], "db.example.com")
        self.assertEqual(db["PORT"], "5432")

    def test_postgresql_scheme_maps_minimal_url(self):
        db = config_settings._database_from_url("postgresql://localhost/appdb")
        self.assertEqual(db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(db["NAME"], "appdb")
        self.assertEqual(db["HOST"], "localhost")
        self.assertNotIn("USER", db)
        self.assertNotIn("PASSWORD", db)
        self.assertNotIn("PORT", db)

    def test_postgres_url_forwards_sslmode_into_options(self):
        # SPEC-22-01: sslmode travels in the URL query and reaches psycopg through OPTIONS; dropping it left a remote database connecting unencrypted.
        db = config_settings._database_from_url(
            "postgres://u:p@db.example.com:5432/perfume_store?sslmode=require"
        )
        self.assertEqual(db["OPTIONS"], {"sslmode": "require"})

    def test_postgres_url_forwards_multiple_options_with_last_value_winning(self):
        db = config_settings._database_from_url(
            "postgres://u:p@h/db?sslmode=disable&connect_timeout=10&sslmode=require"
        )
        self.assertEqual(
            db["OPTIONS"],
            {"sslmode": "require", "connect_timeout": "10"},
        )

    def test_postgres_url_options_are_url_decoded(self):
        db = config_settings._database_from_url(
            "postgres://u:p@h/db?options=-c%20statement_timeout%3D5000"
        )
        self.assertEqual(db["OPTIONS"], {"options": "-c statement_timeout=5000"})

    def test_postgres_url_without_query_omits_options_entirely(self):
        db = config_settings._database_from_url("postgres://u:p@h:5432/db")
        self.assertNotIn("OPTIONS", db)

    def test_malformed_query_params_are_ignored_not_guessed_at(self):
        db = config_settings._database_from_url(
            "postgres://u:p@h/db?sslmode=&novalue&&=orphan&sslmode=require"
        )
        self.assertEqual(db["OPTIONS"], {"sslmode": "require"})

    def test_sqlite_url_query_params_are_not_turned_into_options(self):
        db = config_settings._database_from_url("sqlite:///db.sqlite3?timeout=5")
        self.assertNotIn("OPTIONS", db)

    def test_postgres_url_without_host_keeps_name_only(self):
        db = config_settings._database_from_url("postgres:///appdb")
        self.assertEqual(
            db, {"ENGINE": "django.db.backends.postgresql", "NAME": "appdb"}
        )

    def test_postgres_url_without_name_falls_back(self):
        db = config_settings._database_from_url("postgres://db.example.com")
        self.assertEqual(db, self._fallback())

    def test_postgres_url_with_malformed_port_falls_back(self):
        db = config_settings._database_from_url("postgres://u@h:notaport/db")
        self.assertEqual(db, self._fallback())

    def test_unparseable_url_falls_back(self):
        db = config_settings._database_from_url("postgres://[::1")
        self.assertEqual(db, self._fallback())

    def test_sqlite_relative_path_resolves_against_base_dir(self):
        db = config_settings._database_from_url("sqlite:///custom/db.sqlite3")
        self.assertEqual(db["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(db["NAME"], config_settings.BASE_DIR / "custom" / "db.sqlite3")

    def test_sqlite_url_without_leading_slash_is_relative(self):
        db = config_settings._database_from_url("sqlite:db.sqlite3")
        self.assertEqual(db["NAME"], config_settings.BASE_DIR / "db.sqlite3")

    def test_sqlite_four_slash_absolute_path_is_used_verbatim(self):
        db = config_settings._database_from_url("sqlite:////var/lib/app/db.sqlite3")
        self.assertEqual(db["NAME"], Path("/var/lib/app/db.sqlite3"))
        self.assertNotIn(str(config_settings.BASE_DIR), str(db["NAME"]))

    def test_sqlite_drive_path_is_used_verbatim(self):
        db = config_settings._database_from_url("sqlite:///C:/data/db.sqlite3")
        self.assertEqual(db["NAME"], Path("C:/data/db.sqlite3"))

    def test_sqlite_url_without_name_falls_back(self):
        db = config_settings._database_from_url("sqlite:///")
        self.assertEqual(db, self._fallback())

    def test_unsupported_scheme_falls_back(self):
        db = config_settings._database_from_url("mysql://u:p@h/db")
        self.assertEqual(db, self._fallback())


class DatabaseUrlImportTests(SimpleTestCase):
    _BOOT_ENV = _NON_DEBUG_ENV

    def test_missing_database_url_imports_with_sqlite_default_in_development(self):
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
        res = run_settings_import(
            {
                **self._BOOT_ENV,
                "DATABASE_URL": ("postgres://u:p@h:5432/db?sslmode=require"),
            },
            snippet=(
                "import config.settings as s; "
                "print('OPTIONS', s.DATABASES['default'].get('OPTIONS'))"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OPTIONS {'sslmode': 'require'}", res.stdout)

    def test_malformed_database_url_falls_back_in_development(self):
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
    def _refuses(self, env, expected):
        res = run_settings_import(env)
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn(expected, res.stderr)

    def _without(self, key):
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
        self._refuses({**self._BASE, "DATABASE_URL": "postgres://[::1"}, "DATABASE_URL")

    def test_unsupported_database_scheme_refuses_to_boot(self):
        self._refuses({**self._BASE, "DATABASE_URL": "mysql://u:p@h/db"}, "mysql")

    def test_staging_is_refused_just_like_production(self):
        res = run_settings_import({**_STAGING_ENV, "DATABASE_URL": ""})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("DATABASE_URL", res.stderr)

    def test_explicit_sqlite_url_is_honoured_outside_development(self):
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
        self.assertIn("HOSTS ['localhost', '127.0.0.1']", res.stdout)

    def test_debug_false_without_a_declared_environment_refuses_to_boot(self):
        self._refuses(self._without("DJANGO_ENV"), "DJANGO_ENV")

    def test_unknown_environment_name_refuses_to_boot(self):
        self._refuses({**self._BASE, "DJANGO_ENV": "productionn"}, "productionn")

    def test_non_debug_without_allowed_hosts_refuses_to_boot(self):
        self._refuses(self._without("DJANGO_ALLOWED_HOSTS"), "DJANGO_ALLOWED_HOSTS")

    def test_non_debug_without_csrf_trusted_origins_refuses_to_boot(self):
        self._refuses(self._without("CSRF_TRUSTED_ORIGINS"), "CSRF_TRUSTED_ORIGINS")

    def test_every_declared_environment_boots(self):
        for name in ("local", "ci", "staging", "production"):
            with self.subTest(env=name):
                res = run_settings_import(
                    {**self._BASE, "DJANGO_ENV": name},
                    snippet="import config.settings as s; print('ENV', s.DJANGO_ENV)",
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn(f"ENV {name}", res.stdout)

    def test_resolver_refusals_name_the_problem_in_process(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            config_settings._databases_from_url(None, False)
        self.assertIn("DATABASE_URL", str(caught.exception))
        with self.assertRaises(ImproperlyConfigured) as caught:
            config_settings._databases_from_url("mysql://u:p@h/db", False)
        self.assertIn("mysql", str(caught.exception))
        with self.assertRaises(ImproperlyConfigured) as caught:
            config_settings._deployment_environment(False)
        self.assertIn("DJANGO_ENV", str(caught.exception))
        os.environ["DJANGO_ENV"] = "prod"
        try:
            with self.assertRaises(ImproperlyConfigured) as caught:
                config_settings._deployment_environment(True)
            self.assertIn("'prod'", str(caught.exception))
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
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", middleware)
        self.assertLess(
            middleware.index("django.middleware.security.SecurityMiddleware"),
            middleware.index("whitenoise.middleware.WhiteNoiseMiddleware"),
        )
        self.assertLess(
            middleware.index("whitenoise.middleware.WhiteNoiseMiddleware"),
            middleware.index("django.contrib.sessions.middleware.SessionMiddleware"),
        )

    def test_static_serving_is_not_gated_on_debug(self):
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
        self.assertIn("DEFAULT django.core.files.storage.FileSystemStorage", res.stdout)
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
                "print('MEDIA_ROOT', s.MEDIA_ROOT.as_posix()); "
                "print('DEFAULT', s.STORAGES['default']['BACKEND'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("MEDIA_ROOT /srv/uploads", res.stdout)
        self.assertIn("DEFAULT example.bucket.MediaBucket", res.stdout)

    def test_malformed_media_backend_refuses_to_boot(self):
        res = run_settings_import(
            {**self._BOOT_ENV, "DJANGO_MEDIA_BACKEND": "/srv/uploads"},
            snippet="import config.settings",
        )
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("DJANGO_MEDIA_BACKEND", res.stderr)
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
        with tempfile.TemporaryDirectory() as media_root:
            product_image = Path(media_root) / "products"
            product_image.mkdir()
            (product_image / "rose.png").write_bytes(b"PNGDATA")
            res = run_settings_import(
                {
                    **self._BOOT_ENV,
                    "DJANGO_DEBUG": "false",
                    "DJANGO_MEDIA_ROOT": media_root,
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
        for debug in (False, True):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                match = resolve("/media/products/rose.png")
                self.assertIs(match.func, serve_media)
                self.assertEqual(match.kwargs["path"], "products/rose.png")
                self.assertEqual(
                    match.kwargs["document_root"], str(settings.MEDIA_ROOT)
                )


class TransportHardeningTests(SimpleTestCase):
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
        self.assertIn("PROXYHDR ('X-Forwarded-Proto', 'https')", res.stdout)

    def test_proxy_header_half_configured_is_unconfigured(self):
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

    def test_num_proxies_decides_whose_address_a_throttle_budget_is_keyed_on(
        self,
    ):
        # Unset, DRF trusts the WHOLE X-Forwarded-For header, which the caller writes, so varying it buys a fresh budget per request; the default trusts nothing and NUM_PROXIES=N follows the real address.
        for env_overrides, expected in (
            ({}, "NUMPROXIES 0"),
            ({"NUM_PROXIES": "1"}, "NUMPROXIES 1"),
            ({"NUM_PROXIES": "3"}, "NUMPROXIES 3"),
            ({"NUM_PROXIES": "two"}, "NUMPROXIES 0"),
            ({"NUM_PROXIES": "-1"}, "NUMPROXIES 0"),
        ):
            with self.subTest(env=sorted(env_overrides)):
                res = run_settings_import(
                    {**self._BOOT_ENV, **env_overrides},
                    snippet=(
                        "import config.settings as s; "
                        "print('NUMPROXIES', s.REST_FRAMEWORK['NUM_PROXIES'])"
                    ),
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn(expected, res.stdout)

        for env_overrides, expected in (
            ({}, "XFHOST False"),
            ({"USE_X_FORWARDED_HOST": "true"}, "XFHOST True"),
        ):
            with self.subTest(x_forwarded_host=sorted(env_overrides)):
                res = run_settings_import(
                    {**self._BOOT_ENV, **env_overrides},
                    snippet=(
                        "import config.settings as s; "
                        "print('XFHOST', s.USE_X_FORWARDED_HOST)"
                    ),
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn(expected, res.stdout)

        proxy_ip = "198.18.0.1"
        factory = APIRequestFactory()
        view = SimpleNamespace(throttle_scope="auth")

        def probe(throttle, forwarded_for):
            request = factory.post("/api/accounts/register/")
            request.META["REMOTE_ADDR"] = proxy_ip
            request.META["HTTP_X_FORWARDED_FOR"] = forwarded_for
            request.user = AnonymousUser()
            return throttle.get_ident(request), throttle.allow_request(request, view)

        # Each case is a PAIR of requests against a one-request budget and deletes its own keys first, so the table is order-independent and re-runnable.
        cases = (
            (
                0,
                "198.51.100.7, 203.0.113.5",
                "198.51.100.8, 203.0.113.6",
                proxy_ip,
                proxy_ip,
                False,
            ),
            (0, "203.0.113.99", "203.0.113.98", proxy_ip, proxy_ip, False),
            (
                1,
                "198.51.100.7, 203.0.113.5",
                "198.51.100.8, 203.0.113.6",
                "203.0.113.5",
                "203.0.113.6",
                True,
            ),
            (
                1,
                "198.51.100.7, 203.0.113.5",
                "198.51.100.99, 203.0.113.5",
                "203.0.113.5",
                "203.0.113.5",
                False,
            ),
            (
                2,
                "198.51.100.12, 203.0.113.7, 192.0.2.8",
                "198.51.100.13, 203.0.113.8, 192.0.2.9",
                "203.0.113.7",
                "203.0.113.8",
                True,
            ),
        )
        for case in cases:
            num_proxies, first_xff, second_xff, first_key, second_key, second_ok = case
            with self.subTest(num_proxies=num_proxies, pair=case[1:3]):
                with override_settings(
                    REST_FRAMEWORK={
                        **settings.REST_FRAMEWORK,
                        "NUM_PROXIES": num_proxies,
                    }
                ):
                    with mock.patch.object(
                        ScopedRateThrottle, "THROTTLE_RATES", {"auth": "1/min"}
                    ):
                        throttle = ScopedRateThrottle()
                        for ident in {first_key, second_key}:
                            cache.delete(
                                throttle.cache_format
                                % {"scope": "auth", "ident": ident}
                            )
                        self.assertEqual(probe(throttle, first_xff), (first_key, True))
                        self.assertEqual(
                            probe(throttle, second_xff), (second_key, second_ok)
                        )

    def test_cookie_secure_flags_follow_debug_for_local_dev(self):
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
                "import config.settings as s; print('HSTS', s.SECURE_HSTS_SECONDS)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("HSTS 0", res.stdout)

    def test_malformed_bool_falls_back_to_the_default(self):
        os.environ["SECURE_SSL_REDIRECT_TEST"] = "garbage"
        try:
            self.assertFalse(
                config_settings._env_bool("SECURE_SSL_REDIRECT_TEST", False)
            )
        finally:
            del os.environ["SECURE_SSL_REDIRECT_TEST"]

    def test_dotenv_leaked_flags_cannot_defeat_the_hardened_defaults(self):
        # settings.py runs load_dotenv() at import, so an untracked backend/.env has already pushed these names into os.environ; the harness must scrub them.
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


class CacheAliasTests(SimpleTestCase):
    _PRINT_ALIASES = (
        "import config.settings as s; "
        "print('ALIASES', sorted(s.CACHES)); "
        "print('DEFAULT', s.CACHES['default']['BACKEND']); "
        "print('ALERTS', s.CACHES['alerts']['BACKEND'])"
    )

    def test_alert_cooldown_alias_is_separate_and_shared_by_default(self):
        res = run_settings_import(_DEV_ENV, snippet=self._PRINT_ALIASES)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ALIASES ['alerts', 'default']", res.stdout)
        self.assertIn(
            "DEFAULT django.core.cache.backends.locmem.LocMemCache", res.stdout
        )
        self.assertIn(
            "ALERTS django.core.cache.backends.filebased.FileBasedCache", res.stdout
        )

    def test_empty_alert_cache_location_does_not_resolve_to_the_cwd(self):
        res = run_settings_import(
            {**_DEV_ENV, "ALERT_CACHE_LOCATION": ""},
            snippet=(
                "import config.settings as s; "
                "print('LOCATION', s.CACHES['alerts']['LOCATION'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        location = next(
            line.split(" ", 1)[1]
            for line in res.stdout.splitlines()
            if line.startswith("LOCATION ")
        )
        self.assertNotEqual(location.strip(), "")
        self.assertNotEqual(location.strip(), tempfile.gettempdir())
        self.assertEqual(os.path.basename(location.strip()), "alert_cache")

    def test_a_malformed_alert_cache_backend_refuses_to_boot(self):
        res = run_settings_import({**_DEV_ENV, "ALERT_CACHE_BACKEND": "/srv/cache"})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("ALERT_CACHE_BACKEND", res.stderr)

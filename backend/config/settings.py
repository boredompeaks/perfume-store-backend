"""Django settings for the config project.

Every deployment-shaped value is env-driven, and non-development boots fail closed
when a required value is missing.
"""

import logging
import os
import re
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery.schedules import crontab, schedule
from corsheaders.defaults import default_headers as _cors_default_headers
from django.conf.locale import LANG_INFO
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# The repo's one money-rounding rule; common.money is pure decimal, so importing
# it here cannot touch a model before the apps are ready.
from common.money import quantize_money

load_dotenv()
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
# Razorpay signs webhooks with this secret, a different credential from the key
# pair above; the endpoint fails closed (503) while it is unset.
RAZORPAY_WEBHOOK_SECRET = (os.getenv("RAZORPAY_WEBHOOK_SECRET") or "").strip()


BASE_DIR = Path(__file__).resolve().parent.parent


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-key-change-me")

# Fails CLOSED: an absent DJANGO_DEBUG means DEBUG=False, so an unconfigured
# deployment lands hardened and must set a real DJANGO_SECRET_KEY.
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"

if not DEBUG and not os.getenv("DJANGO_SECRET_KEY"):
    raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false.")


# The environment this deployment separates itself from; declared, validated and
# mandatory outside development.
_VALID_DEPLOY_ENVIRONMENTS = frozenset({"local", "ci", "staging", "production"})


def _deployment_environment(debug):
    """Return the declared deployment environment.

    Development may stay undeclared; every non-debug boot must name it, and an
    unknown value is refused rather than guessed at.
    """
    raw = (os.getenv("DJANGO_ENV") or "").strip().lower()
    if not raw:
        if debug:
            return "local"
        raise ImproperlyConfigured(
            "DJANGO_ENV must name the deployment environment ("
            + ", ".join(sorted(_VALID_DEPLOY_ENVIRONMENTS))
            + ") when DJANGO_DEBUG is false: an environment that cannot be "
            "named cannot be isolated from the others."
        )
    if raw not in _VALID_DEPLOY_ENVIRONMENTS:
        raise ImproperlyConfigured(
            f"Unsupported DJANGO_ENV {raw!r}; expected one of "
            + ", ".join(sorted(_VALID_DEPLOY_ENVIRONMENTS))
            + "."
        )
    return raw


DJANGO_ENV = _deployment_environment(DEBUG)


def _host_list(raw):
    """Split a comma-separated host/origin list, tolerating padding and blanks;
    an untrimmed entry would match nothing."""
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def _env_hosts(name, default, debug):
    """Return a comma-separated host/origin list, refusing the development
    default outside development."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        if not debug:
            raise ImproperlyConfigured(
                f"{name} must be set explicitly when DJANGO_DEBUG is false "
                f"(comma-separated); the development default {default!r} is "
                f"never a deployed environment's configuration."
            )
        raw = default
    return _host_list(raw)


ALLOWED_HOSTS = _env_hosts("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1", DEBUG)


def _env_bool(name, default):
    """Resolve an env boolean, falling back to the documented default instead of
    raising."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


CSRF_COOKIE_SECURE = _env_bool("CSRF_COOKIE_SECURE", not DEBUG)

# Split-origin SPA: without a parent domain the csrftoken cookie is host-scoped
# to the API and the storefront cannot read it. Empty keeps Django's default.
SESSION_COOKIE_DOMAIN = (os.getenv("SESSION_COOKIE_DOMAIN") or "").strip() or None
CSRF_COOKIE_DOMAIN = (os.getenv("CSRF_COOKIE_DOMAIN") or "").strip() or None
# Must stay false: the SPA reads csrftoken from document.cookie.
CSRF_COOKIE_HTTPONLY = _env_bool("CSRF_COOKIE_HTTPONLY", False)


INSTALLED_APPS = [
    # Swaps the default admin site for one whose login form requires a TOTP code
    # from privileged staff.
    "config.admin.MFAAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    # Token tables behind refresh rotation, logout and session invalidation.
    "rest_framework_simplejwt.token_blacklist",
    "products",
    "cart",
    "orders",
    "accounts",
    # Shipping methods/rates and the server-side pricing both estimate and
    # checkout read.
    "shipping",
    "ops",
    "common",
    "corsheaders",
]

MIDDLEWARE = [
    # First so X-Request-ID is on every response, including ones produced above
    # the view and the unhandled-exception 500.
    "common.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Serves STATIC_ROOT from the app process so DEBUG=false still returns admin
    # assets; its documented slot is below SecurityMiddleware.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Rewrites ad-hoc {"error": ...} returns into the uniform envelope; DRF
    # exceptions are enveloped at the source, so each response is wrapped once.
    "common.errors.ErrorEnvelopeMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"



def _query_options(query):
    """Return DATABASES OPTIONS from a DATABASE_URL query string, forwarding bare
    key=value pairs (a repeated key takes its last value, matching libpq).

    Dropping these would leave a remote database connecting without ``sslmode``.
    """
    if not query:
        return {}
    options = {}
    for pair in query.split("&"):
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = unquote(key).strip()
        if key:
            options[key] = unquote(value)
    return options


def _parse_database_url(url):
    """Return ``(entry, resolved, reason)`` for a DATABASE_URL.

    ``reason`` never includes the URL itself, which carries credentials, and
    ``resolved`` is False for a missing, malformed or unsupported URL.
    """
    fallback = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
    if not url:
        return fallback, False, "is not set"
    try:
        parsed = urlparse(url)
        # Accessing .port validates it: a malformed port raises ValueError.
        port = parsed.port
    except ValueError:
        return fallback, False, "is not a parseable URL (malformed host or port)"
    scheme = parsed.scheme.lower()
    if scheme in ("postgres", "postgresql"):
        # The db name is never a filesystem path, so stripping all leading
        # slashes is safe here and accepts both /name and name forms.
        name = parsed.path.lstrip("/")
        if not name:
            return fallback, False, "names no database"
        config = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": name,
        }
        if parsed.username:
            config["USER"] = unquote(parsed.username)
        if parsed.password is not None:
            config["PASSWORD"] = unquote(parsed.password)
        if parsed.hostname:
            config["HOST"] = parsed.hostname
        if port is not None:
            config["PORT"] = str(port)
        # libpq params (sslmode above all) are part of the URL, not noise: a
        # remote Postgres reached without sslmode carries traffic in the clear.
        options = _query_options(parsed.query)
        if options:
            config["OPTIONS"] = options
        return config, True, ""
    if scheme == "sqlite":
        # Strip one leading slash so sqlite:///db names a file relative to
        # BASE_DIR while four slashes or a drive prefix are absolute; the check
        # is on the raw string because Path.isabs is platform-dependent.
        raw = parsed.path
        if raw.startswith("/"):
            raw = raw[1:]
        if not raw:
            return fallback, False, "names no sqlite file"
        name = Path(raw)
        is_absolute = raw.startswith("/") or (len(raw) > 1 and raw[1] == ":")
        return (
            {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": name if is_absolute else BASE_DIR / name,
            },
            True,
            "",
        )
    # Unsupported scheme (mysql://, ...): reported, never silently accepted.
    return (
        fallback,
        False,
        f"has unsupported scheme {scheme!r} (supported: postgres, postgresql, sqlite)",
    )


def _database_from_url(url):
    """Return just the DATABASES entry; the boot decision lives in
    ``_databases_from_url``."""
    return _parse_database_url(url)[0]


def _databases_from_url(url, debug):
    """Resolve DATABASES, failing CLOSED outside development.

    A typo'd URL must not boot on developer data; an explicit sqlite URL is still
    honoured in any environment.
    """
    config, resolved, reason = _parse_database_url(url)
    if not resolved and not debug:
        raise ImproperlyConfigured(
            f"DATABASE_URL {reason}. With DJANGO_DEBUG=false the app refuses "
            f"to fall back to the local sqlite development database "
            f"({BASE_DIR / 'db.sqlite3'}): a production or staging host "
            f"serving developer data is worse than a failed boot. Set "
            f"DATABASE_URL (see backend/.env.example), or run with "
            f"DJANGO_DEBUG=true for local development."
        )
    return {"default": config}


DATABASES = _databases_from_url(os.getenv("DATABASE_URL"), DEBUG)

# One suite per test database: the runner takes an exclusive file lock keyed on
# the resolved test DB and refuses while another run owns it
# (common/test_isolation.py). In-memory SQLite is process-private and not locked.
TEST_RUNNER = "common.test_isolation.LockedDiscoverRunner"


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# BCP 47 shape: a 2-3 letter primary subtag plus optional alphanumeric subtags.
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$")


def _env_time_zone(name, default):
    """Resolve an IANA time zone; an unknown value would raise on the first
    localized read, so it falls back with a warning."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        logging.getLogger(__name__).warning(
            "Ignoring unknown %s value %r; using %s", name, raw, default
        )
        return default
    return raw


def _env_language_code(name, default):
    """Resolve a Django language tag; a value with no shipped translations
    (LANG_INFO) cannot change any string, so it falls back with a warning."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    parts = raw.lower().split("-")
    if _LANGUAGE_TAG.fullmatch(raw) and any(
        "-".join(parts[:i]) in LANG_INFO for i in range(len(parts), 0, -1)
    ):
        return raw
    logging.getLogger(__name__).warning(
        "Ignoring unsupported %s value %r; using %s", name, raw, default
    )
    return default


LANGUAGE_CODE = _env_language_code("LANGUAGE_CODE", "en-us")

TIME_ZONE = _env_time_zone("TIME_ZONE", "UTC")

USE_I18N = True

USE_TZ = True


STATIC_URL = "static/"

# collectstatic needs a destination; env-driven because container and host
# deployments differ, with the conventional in-backend default.
STATIC_ROOT = os.getenv("DJANGO_STATIC_ROOT") or str(BASE_DIR / "staticfiles")

# USE_FINDERS follows DEBUG so a dev server finds assets without collectstatic;
# production reads only STATIC_ROOT.
WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG


# Env ints never take the app down at startup: a malformed value falls back.
def _env_int(name, default):
    # Declared before the Email block, which is the first consumer.
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_positive_int(name, default):
    """Resolve an env int the consumer cannot accept zero/negative for.

    ``_env_int`` refuses a value it cannot parse; this refuses one the consumer
    cannot use (``settimeout(0)`` is non-blocking), falling back with a warning.
    """
    value = _env_int(name, default)
    if value < 1:
        logging.getLogger(__name__).warning(
            "Ignoring non-positive %s value %r; using %s", name, value, default
        )
        return default
    return value


EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() == "true"
# The SMTP socket wait is bounded: an unset value means no timeout, and an
# inline send holds select_for_update row locks for the whole stall, blocking
# checkouts. 10s is several times a healthy handshake and still a bounded failure.
EMAIL_TIMEOUT = _env_positive_int("EMAIL_TIMEOUT", 10)
# Same bounded-failure argument for the gateway: a stalled refund parks every
# other refund for the order behind its lock. The SDK takes it as a per-request
# kwarg, because a constructor timeout would be discarded.
RAZORPAY_TIMEOUT = _env_positive_int("RAZORPAY_TIMEOUT", 10)
# Outbox payloads deliberately keep one-time token material the audit scrubber
# would drop, so deletion on a clock is the control: three days outlives any
# queue backlog and is bounded independently by PASSWORD_RESET_TIMEOUT.
NOTIFICATION_OUTBOX_TTL_SECONDS = _env_positive_int(
    "NOTIFICATION_OUTBOX_TTL_SECONDS", 3 * 24 * 60 * 60
)
# The five drain knobs are all deployment properties; see .env.example.
# Batch size bounds one pass (a ceiling; the pass stops early when idle).
NOTIFICATION_OUTBOX_DRAIN_BATCH_SIZE = _env_positive_int(
    "NOTIFICATION_OUTBOX_DRAIN_BATCH_SIZE", 100
)
# MUST exceed EMAIL_TIMEOUT or a second worker can start the same send, and
# should not exceed the drain interval by much.
NOTIFICATION_OUTBOX_LEASE_SECONDS = _env_positive_int(
    "NOTIFICATION_OUTBOX_LEASE_SECONDS", 60
)
# Attempts per row before dead-lettering; the only thing that stops a
# permanently failing notification consuming the batch.
NOTIFICATION_OUTBOX_MAX_ATTEMPTS = _env_positive_int(
    "NOTIFICATION_OUTBOX_MAX_ATTEMPTS", 5
)
# base * 2**(attempt-1), capped: base is the first-failure wait and cap the
# longest wait a row ever sits through.
NOTIFICATION_OUTBOX_RETRY_BASE_SECONDS = _env_positive_int(
    "NOTIFICATION_OUTBOX_RETRY_BASE_SECONDS", 30
)
NOTIFICATION_OUTBOX_RETRY_MAX_SECONDS = _env_positive_int(
    "NOTIFICATION_OUTBOX_RETRY_MAX_SECONDS", 3600
)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")

# REDIS_URL is the single knob for the broker and the result backend; each can be
# overridden. The localhost default carries no password, so a real deployment
# sets its own URL in the environment.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0").strip()
CELERY_BROKER_URL = (os.getenv("CELERY_BROKER_URL") or REDIS_URL).strip()
CELERY_RESULT_BACKEND = (os.getenv("CELERY_RESULT_BACKEND") or REDIS_URL).strip()

# JSON on every serializer: pickle over a result backend is remote code
# execution. All three already default to json, so this pins the security
# property explicitly rather than inheriting it.
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
# One result write per task buys the queued-vs-stuck distinction.
CELERY_TASK_TRACK_STARTED = True
# The store's time zone, not the host's, so sweeps fire at the same hour
# everywhere.
CELERY_TIMEZONE = TIME_ZONE
# Timezone-aware, so beat's arithmetic does not drift across DST.
CELERY_ENABLE_UTC = True
# False: this project owns LOGGING, and celery must not replace it.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
# Explicit queue so a shared Redis cannot mix another app's tasks in.
CELERY_TASK_DEFAULT_QUEUE = os.getenv("CELERY_TASK_DEFAULT_QUEUE", "perfume_store")

# Eager mode is a documented single-host deployment option, not a test setting;
# off by default.
CELERY_TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", False)


# Cadence values are settings so an operator can read them and a test can
# override them.
CELERY_BEAT_EXPIRE_EVERY_MINUTES = _env_positive_int(
    "CELERY_BEAT_EXPIRE_EVERY_MINUTES", 5
)
CELERY_BEAT_DRAIN_OUTBOX_SECONDS = _env_positive_int(
    "CELERY_BEAT_DRAIN_OUTBOX_SECONDS", 60
)
CELERY_BEAT_PURGE_HOUR = _env_int("CELERY_BEAT_PURGE_HOUR", 3)


def _interval_schedule(seconds):
    """Return a beat entry firing every ``seconds``; crontab cannot express a
    sub-minute cadence. Clamped at 1."""
    return schedule(run_every=max(1, seconds))


def _beat_schedule():
    """Return the beat schedule, keyed by management command name so an
    operator's vocabulary finds each tick.

    Every task calls its command, so the scheduled and manual runs are one
    implementation. The dict is frozen at import; tests override the settings.
    """
    return {
        # Must run well inside RESERVATION_TTL or a hold can sit past its own
        # expiry; 300s is a third of the default, leaving room for late ticks.
        "expire_reservations": {
            "task": "common.tasks.expire_reservations",
            "schedule": crontab(
                minute=f"*/{CELERY_BEAT_EXPIRE_EVERY_MINUTES}",
                hour="*",
                day_of_week="*",
                day_of_month="*",
                month_of_year="*",
            ),
        },
        # Every minute, the floor crontab can express: a retried row waits its
        # backoff plus up to one tick. In seconds because this cadence is the one
        # an operator may want to tune.
        "drain_notification_outbox": {
            "task": "common.tasks.drain_notification_outbox",
            "schedule": _interval_schedule(CELERY_BEAT_DRAIN_OUTBOX_SECONDS),
        },
        # Daily at an off-hour minute: a retention sweep needs no sub-daily
        # cadence, and the off-hour avoids the shared 00:00 spike.
        "purge_notification_outbox": {
            "task": "common.tasks.purge_notification_outbox",
            "schedule": crontab(
                minute="17",
                hour=CELERY_BEAT_PURGE_HOUR,
                day_of_week="*",
                day_of_month="*",
                month_of_year="*",
            ),
        },
    }


CELERY_BEAT_SCHEDULE = _beat_schedule()


# Mandatory MFA refuses an unenrolled privileged login, so the blocked account
# needs a reachable enrollment URL; the default suits a single-host deployment.
MFA_ENROLL_URL = os.getenv("MFA_ENROLL_URL", f"{FRONTEND_URL}/staff/mfa/enroll")
MEDIA_URL = "/media/"
# Uploads are MEDIA, not STATIC; MEDIA_ROOT is env-driven because a container
# volume and a host deployment differ.
MEDIA_ROOT = Path(os.getenv("DJANGO_MEDIA_ROOT") or (BASE_DIR / "media"))

# Importable dotted path only: a filesystem path, URL or trailing dot cannot import.
_DOTTED_PATH = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")


def _env_dotted_path(name, default):
    """Resolve an importable dotted path, refusing a malformed value at boot.

    Only the shape is checked: importing an operator-supplied module at
    settings-import time is out.
    """
    raw = (os.getenv(name) or "").strip() or default
    if not _DOTTED_PATH.fullmatch(raw):
        raise ImproperlyConfigured(
            f"{name} must be an importable dotted Python path (the default is "
            f"{default}); got {raw!r}."
        )
    return raw


# STORAGES makes the media backend env-switchable with no code change. Defaults
# are Django's own; staticfiles deliberately stays StaticFilesStorage because
# whitenoise serves collectstatic's output.
MEDIA_BACKEND = _env_dotted_path(
    "DJANGO_MEDIA_BACKEND", "django.core.files.storage.FileSystemStorage"
)

# Object-storage options for the S3-compatible backend (R2 or S3). No bucket,
# credential or endpoint lives in the repo; selecting this backend without a
# bucket is refused at boot.
_S3_MEDIA_BACKEND = "storages.backends.s3.S3Storage"


def _env_media_storage_options(backend):
    """STORAGES options for the media backend.

    Only the S3 backend carries any; an unset endpoint means AWS itself, and
    ``file_overwrite=False`` matches FileSystemStorage's collision behaviour.
    """
    if backend != _S3_MEDIA_BACKEND:
        return {}
    bucket = (os.getenv("AWS_STORAGE_BUCKET_NAME") or "").strip()
    if not bucket:
        raise ImproperlyConfigured(
            "DJANGO_MEDIA_BACKEND selects the S3-compatible storage backend "
            "but AWS_STORAGE_BUCKET_NAME is unset or empty; set it (see "
            ".env.example) or point DJANGO_MEDIA_BACKEND at another backend."
        )
    options = {
        "bucket_name": bucket,
        "file_overwrite": False,
    }
    for env_name, option_name in (
        ("AWS_S3_ENDPOINT_URL", "endpoint_url"),
        ("AWS_S3_REGION_NAME", "region_name"),
        ("AWS_ACCESS_KEY_ID", "access_key"),
        ("AWS_SECRET_ACCESS_KEY", "secret_key"),
        ("AWS_S3_CUSTOM_DOMAIN", "custom_domain"),
    ):
        value = (os.getenv(env_name) or "").strip()
        if value:
            options[option_name] = value
    return options


STORAGES = {
    "default": {
        "BACKEND": MEDIA_BACKEND,
        "OPTIONS": _env_media_storage_options(MEDIA_BACKEND),
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


# A product at or below this many units counts as low stock.
LOW_STOCK_THRESHOLD = _env_int("LOW_STOCK_THRESHOLD", 5)

# The sales-over-time chart covers this many calendar days, ending today.
DASHBOARD_SALES_WINDOW_DAYS = _env_int("DASHBOARD_SALES_WINDOW_DAYS", 30)

# Trusted-device TTL; 0 is a kill switch that puts every privileged login back
# behind a code. Trust is opt-in per device, so this cannot grant one.
MFA_TRUST_DAYS = _env_int("MFA_TRUST_DAYS", 30)
# Signed marker binding a trusted device to the browser that asked (HttpOnly,
# SameSite=Strict), so a stolen password elsewhere still earns the challenge.
MFA_TRUST_COOKIE_NAME = os.getenv("MFA_TRUST_COOKIE_NAME", "mfa_trusted_device")
MFA_TRUST_COOKIE_SAMESITE = os.getenv("MFA_TRUST_COOKIE_SAMESITE", "Strict")

# Failed-attempt budget for the admin login form; DRF's throttles never see that
# plain Django view. An empty value disables the bound.
ADMIN_LOGIN_THROTTLE_RATE = os.getenv("THROTTLE_ADMIN_LOGIN_RATE", "10/min")

# Signed-challenge hook for the username-enumeration endpoints; unset disables
# it. The minting contract is on _enforce_access_challenge in accounts/views.py.
ACCOUNT_CHALLENGE_SECRET = os.getenv("ACCOUNT_CHALLENGE_SECRET", "")
# Freshness window for a signed challenge; below 1 disables the age check only,
# not the signature.
ACCOUNT_CHALLENGE_MAX_AGE = _env_int("ACCOUNT_CHALLENGE_MAX_AGE", 300)

# Comma-separated admin mailboxes; empty disables alerts.
ALERT_RECIPIENTS = os.getenv("ALERT_RECIPIENTS", "")

# DRF throttles keep history on the default alias, and LocMemCache is
# per-process, so N workers kept N windows. A declared REDIS_URL switches the
# alias to Redis; the alerts alias keeps a shared file cache either way. Under
# the test runner the alias uses its own DB index, because clear() is FLUSHDB.
_REDIS_URL = (os.getenv("REDIS_URL") or "").strip()


def _is_test_run():
    """True for a test-runner invocation; argv is the only signal available at
    settings-import time, before the runner object exists."""
    return "test" in sys.argv[1:]


def _test_redis_location(location):
    """Point a test run's Redis cache at its own DB index: RedisCache.clear()
    is FLUSHDB and ignores KEY_PREFIX, so only a separate index is a real
    namespace."""
    db = (os.getenv("TEST_REDIS_DB") or "").strip()
    if not db.isdigit():
        db = "13"
    parsed = urlparse(location)
    if parsed.scheme not in ("redis", "rediss"):
        return location
    return parsed._replace(path=f"/{db}").geturl()


_default_cache_backend = _env_dotted_path(
    "CACHE_BACKEND",
    "django.core.cache.backends.redis.RedisCache"
    if _REDIS_URL
    else "django.core.cache.backends.locmem.LocMemCache",
)
_default_cache_location = (os.getenv("CACHE_LOCATION") or "").strip() or (
    _REDIS_URL or "default"
)
if _is_test_run() and (
    _default_cache_backend == "django.core.cache.backends.redis.RedisCache"
):
    _default_cache_location = _test_redis_location(_default_cache_location)

CACHES = {
    "default": {
        "BACKEND": _default_cache_backend,
        "LOCATION": _default_cache_location,
    },
    "alerts": {
        "BACKEND": _env_dotted_path(
            "ALERT_CACHE_BACKEND",
            "django.core.cache.backends.filebased.FileBasedCache",
        ),
        "LOCATION": (os.getenv("ALERT_CACHE_LOCATION") or "").strip()
        or str(BASE_DIR / "alert_cache"),
    },
}
# Per-type dedupe window for the pollable /health/ and dashboard triggers.
ALERT_COOLDOWN_SECONDS = _env_int("ALERT_COOLDOWN_SECONDS", 300)
# At least this many failed payments in the trailing window fires the spike
# alert (defaults: 3 in 300).
PAYMENT_FAILURE_SPIKE_COUNT = _env_int("PAYMENT_FAILURE_SPIKE_COUNT", 3)
PAYMENT_FAILURE_SPIKE_WINDOW_SECONDS = _env_int(
    "PAYMENT_FAILURE_SPIKE_WINDOW_SECONDS", 300
)

# Storefront rows per page; 12 is storefront-appropriate (the old 2 was a dev
# artifact).
PRODUCTS_PAGE_SIZE = _env_int("PRODUCTS_PAGE_SIZE", 12)

# Duplicate checkout submissions collapse onto the still-payable pending order
# with the identical payload inside this window.
CHECKOUT_DEDUP_WINDOW_SECONDS = _env_int("CHECKOUT_DEDUP_WINDOW_SECONDS", 300)

# Rows per page and the ceiling a ?page_size caller may request; deployment
# config, not hardcoded.
ORDER_HISTORY_PAGE_SIZE = _env_int("ORDER_HISTORY_PAGE_SIZE", 10)
ORDER_HISTORY_MAX_PAGE_SIZE = _env_int("ORDER_HISTORY_MAX_PAGE_SIZE", 100)

# Rows per page and the ?page_size ceiling for returns, its own pair so the
# store can choose a different density from order history. The return WINDOW is
# deliberately not here: it is merchant policy in ops.SiteSettings.
RETURNS_HISTORY_PAGE_SIZE = _env_int("RETURNS_HISTORY_PAGE_SIZE", 10)
RETURNS_HISTORY_MAX_PAGE_SIZE = _env_int("RETURNS_HISTORY_MAX_PAGE_SIZE", 100)

# Seconds a stock reservation holds units before the reconciler may release
# them; must comfortably exceed a payment session.
RESERVATION_TTL = _env_int("RESERVATION_TTL", 900)


# The largest amount a money column can hold (8 integer digits); a bigger
# threshold is a typo, and the safe reading keeps charging.
_MONEY_MAX = Decimal("99999999.99")


def _env_money(name, default=None):
    """Resolve a money threshold as a Decimal, falling back with a warning.

    NaN, infinity, negative and over-``_MONEY_MAX`` values are refused
    explicitly: ``quantize`` accepts NaN without raising, so the comparison
    would later crash the import.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = quantize_money(Decimal(raw))
        usable = value.is_finite()
    except (ArithmeticError, ValueError):
        usable = False
        value = None

    if not usable:
        logging.getLogger(__name__).warning(
            "Ignoring unparseable or non-finite %s value %r; the rule stays off",
            name,
            raw,
        )
        return default
    if value < Decimal("0.00"):
        logging.getLogger(__name__).warning(
            "Ignoring negative %s value %r; the rule stays off", name, raw
        )
        return default
    if value > _MONEY_MAX:
        logging.getLogger(__name__).warning(
            "Ignoring %s value %r: above %s, the largest amount this store can "
            "hold, so no order could ever reach it; the rule stays off",
            name,
            raw,
            _MONEY_MAX,
        )
        return default
    return value


# An order whose merchandise total reaches this amount ships free; unset means
# the rule is off. Rates themselves are staff-managed rows, not config.
SHIPPING_FREE_THRESHOLD = _env_money("SHIPPING_FREE_THRESHOLD")


# ISO 4217 currency codes are exactly three uppercase letters.
_ISO_4217 = re.compile(r"^[A-Z]{3}$")


def _env_currency(name, default):
    """Resolve an ISO 4217 currency code; anything not three letters falls back
    with a warning."""
    raw = os.getenv(name, "").strip().upper()
    if _ISO_4217.fullmatch(raw):
        return raw
    if raw:
        logging.getLogger(__name__).warning(
            "Ignoring unsupported %s value %r; using %s", name, raw, default
        )
    return default


# The store currency orders are minted and charged in. Env-driven, not
# merchant-editable, because changing it would reprice historical rows.
DEFAULT_CURRENCY = _env_currency("DEFAULT_CURRENCY", "INR")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        # After JWT, so bearer requests short-circuit and gain no CSRF friction;
        # session-cookie requests are CSRF-checked on unsafe methods.
        "common.authentication.SessionCartCSRFAuthentication",
    ),
    # Envelops DRF exceptions before rendering; the middleware above owns only
    # ad-hoc returns, so each response is wrapped once.
    "EXCEPTION_HANDLER": "common.errors.error_envelope_exception_handler",
    # Endpoints opt in with a ``throttle_scope``; views without one are
    # unthrottled.
    "DEFAULT_THROTTLE_CLASSES": ("rest_framework.throttling.ScopedRateThrottle",),
    "DEFAULT_THROTTLE_RATES": {
        "coupon": os.getenv("THROTTLE_COUPON_RATE", "10/min"),
        "cart": os.getenv("THROTTLE_CART_RATE", "60/min"),
        "auth": os.getenv("THROTTLE_AUTH_RATE", "10/min"),
        # Dedicated scopes, each default tighter than the shared auth budget:
        # separate counters and knobs.
        "register": os.getenv("THROTTLE_REGISTER_RATE", "5/min"),
        "username-probe": os.getenv("THROTTLE_USERNAME_PROBE_RATE", "5/min"),
        # Tighter than auth: each accepted request sends an email.
        "recovery": os.getenv("THROTTLE_RECOVERY_RATE", "5/min"),
        # Bounds gateway spend and order-id guessing.
        "payment": os.getenv("PAYMENT_THROTTLE_RATE", "10/min"),
        # Same outbound-mail shape as the recovery bound.
        "restock": os.getenv("THROTTLE_RESTOCK_RATE", "5/min"),
        # Per-(IP + email) budget for anonymous opt-ins; separate from
        # 'restock' so the mail-bomb shape can be tightened independently.
        "restock-guest": os.getenv("THROTTLE_RESTOCK_GUEST_RATE", "5/min"),
        # Server-to-server bursts, not brute force: signature verification is
        # the defence; this only bounds parsing work.
        "webhook": os.getenv("THROTTLE_WEBHOOK_RATE", "120/min"),
        # The anonymous tracking read carries parcel data and its order-number
        # half is enumerable; this bounds enumeration, not authorization.
        "tracking": os.getenv("THROTTLE_TRACKING_RATE", "60/min"),
    },
    # How many reverse proxies stand in front, deciding whose address an
    # anonymous throttle budget keys on. The default trusts nothing and uses
    # REMOTE_ADDR; set it only behind a proxy that appends the real address and
    # overwrites any client-supplied X-Forwarded-For.
    "NUM_PROXIES": max(0, _env_int("NUM_PROXIES", 0)),
}

# Short-lived access token, rotating refresh token: the presented refresh dies
# on rotation, and the blacklist tables make logout/reset revoke sessions.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        seconds=_env_int("JWT_ACCESS_TOKEN_LIFETIME_SECONDS", 900)
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        seconds=_env_int("JWT_REFRESH_TOKEN_LIFETIME_SECONDS", 604800)
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

# The refresh token rides an HttpOnly cookie instead of localStorage. Path /api/
# covers both account mount families; SameSite=Lax blocks cross-site POST
# attachment.
JWT_REFRESH_COOKIE_NAME = os.getenv("JWT_REFRESH_COOKIE_NAME", "refresh_token")
JWT_REFRESH_COOKIE_PATH = os.getenv("JWT_REFRESH_COOKIE_PATH", "/api/")
JWT_REFRESH_COOKIE_SAMESITE = os.getenv("JWT_REFRESH_COOKIE_SAMESITE", "Lax")


# Dual-accept window for the legacy body-borne refresh token, so an
# already-logged-in browser is not bricked mid-cutover; it never ISSUES one.
# The window closes START + DAYS after the anchor, and a malformed START falls
# back to the default rather than closing early.
def _env_iso_date(name, default):
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return default


JWT_REFRESH_BODY_ACCEPT_ENABLED = _env_bool("JWT_REFRESH_BODY_ACCEPT_ENABLED", True)
JWT_REFRESH_BODY_ACCEPT_START = _env_iso_date(
    "JWT_REFRESH_BODY_ACCEPT_START", date(2026, 10, 15)
)
JWT_REFRESH_BODY_ACCEPT_DAYS = _env_int("JWT_REFRESH_BODY_ACCEPT_DAYS", 90)

# The session cookie carries cart identity; Lax blocks cross-site POST
# attachment, with server-side CSRF enforcement as the actual gate.
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")

# Both loopback spellings of the dev storefront, so a 127.0.0.1 page talks to
# the API on the same host; refused outside development for CSRF.
DEV_FRONTEND_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"

CORS_ALLOWED_ORIGINS = _host_list(
    os.getenv("CORS_ALLOWED_ORIGINS", DEV_FRONTEND_ORIGINS)
)
CSRF_TRUSTED_ORIGINS = _env_hosts("CSRF_TRUSTED_ORIGINS", DEV_FRONTEND_ORIGINS, DEBUG)

CORS_ALLOW_CREDENTIALS = True

# Browsers cannot read a cross-origin response header unless it is named here;
# a test pins this to the middleware's constant.
CORS_EXPOSE_HEADERS = ["X-Request-ID"]

# The browser refuses to send a header missing from the preflight list, so
# extend the package defaults with the checkout/guest headers rather than
# retyping them; a test pins both names.
CORS_ALLOW_HEADERS = list(_cors_default_headers) + [
    "idempotency-key",
    "x-guest-order-token",
]


# Env-driven dictConfig baseline: only console and an optional rotating file, so
# an unconfigured deployment cannot reach the network from logging.

# NOTSET is absent: a root logger at NOTSET logs everything.
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _env_log_level(name, default):
    """Resolve a log level; an unknown value falls back with a warning instead
    of failing the import."""
    raw = os.getenv(name, "").strip().upper()
    if raw in _VALID_LOG_LEVELS:
        return raw
    if raw:
        logging.getLogger(__name__).warning(
            "Ignoring unsupported %s value %r; using %s", name, raw, default
        )
    return default


def _build_logging(app_level, file_path=None):
    """Return the LOGGING dict.

    Console is always wired; a rotating file joins only when LOG_FILE names a
    path, because no hardcoded path may assume a writable location.
    """
    handlers = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
        },
    }
    root_handlers = ["console"]
    if file_path:
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "plain",
            "filename": file_path,
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
        }
        root_handlers.append("file")
    return {
        "version": 1,
        # Loggers created before settings import keep working.
        "disable_existing_loggers": False,
        "formatters": {
            # Brace-style: audit details render dicts, which %-style templates
            # collide with. The correlation id is stamped by the formatter
            # itself, so a new call site cannot forget it.
            "plain": {
                "()": "common.middleware.RequestIDFormatter",
                "format": (
                    "{levelname} {asctime} {name} request_id={request_id} {message}"
                ),
                "style": "{",
            },
        },
        "handlers": handlers,
        "root": {"handlers": root_handlers, "level": app_level},
        "loggers": {
            "django.request": {"level": "ERROR"},
            # Pinned at INFO so AuditEvent.record lines survive a raised LOG_LEVEL.
            "common.audit": {"level": "INFO"},
        },
    }


APP_LOG_LEVEL = _env_log_level("LOG_LEVEL", "INFO")
LOGGING = _build_logging(APP_LOG_LEVEL, os.getenv("LOG_FILE"))


# Error tracking is optional and env-gated: with no SENTRY_DSN nothing is
# imported or initialised. The DSN lives in the environment, never the repo.


def _env_float(name, default):
    """Resolve an env float, falling back with a warning instead of raising."""
    raw = (os.getenv(name) or "").strip() or str(default)
    try:
        return float(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "Ignoring unparseable %s value %r; using %s", name, raw, default
        )
        return default


# The declared deployment environment is the default label, so a staging error
# can never be filed as production.
SENTRY_DSN = (os.getenv("SENTRY_DSN") or "").strip()
SENTRY_ENVIRONMENT = (os.getenv("SENTRY_ENVIRONMENT") or "").strip() or DJANGO_ENV
# 1.0 ships every event; lower it to bound spend during volume.
SENTRY_SAMPLE_RATE = _env_float("SENTRY_SAMPLE_RATE", 1.0)
# Tracing is off: this app instruments no transactions.
SENTRY_TRACES_SAMPLE_RATE = _env_float("SENTRY_TRACES_SAMPLE_RATE", 0.0)
# The deploy SHA labels a release; either name is accepted because the two CI
# systems export different ones, and neither defaults (a guess is worse).
SENTRY_RELEASE = (
    (os.getenv("RELEASE_SHA") or "").strip()
    or (os.getenv("GIT_SHA") or "").strip()
    or None
)


def _init_error_tracking(
    dsn,
    environment,
    sample_rate,
    traces_sample_rate,
    *,
    release=None,
):
    """Initialise the denylist-guarded tracker; inert without a DSN.

    The implementation lives in common.sentry, imported here so an unconfigured
    process never loads the SDK; its logger is passed through so a refused DSN or
    missing package warns from config.settings.
    """
    from common.sentry import init_sentry

    return init_sentry(
        dsn=dsn,
        environment=environment,
        release=release,
        sample_rate=sample_rate,
        traces_sample_rate=traces_sample_rate,
        log=logging.getLogger(__name__),
    )


ERROR_TRACKING_ENABLED = _init_error_tracking(
    SENTRY_DSN,
    SENTRY_ENVIRONMENT,
    SENTRY_SAMPLE_RATE,
    SENTRY_TRACES_SAMPLE_RATE,
    release=SENTRY_RELEASE,
)


# Transport/cookie hardening, all env-gated with development-safe defaults that
# follow DEBUG.

# 0 leaves HSTS unset, the safe default: enabling it on a plain-HTTP host locks
# users out for the declared window.
SECURE_HSTS_SECONDS = _env_int("SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = _env_bool("SECURE_HSTS_PRELOAD", False)

# Off so local dev and tests (no TLS) keep working.
SECURE_SSL_REDIRECT = _env_bool("SECURE_SSL_REDIRECT", False)

# A TLS-terminating proxy means Django sees plain HTTP, so the forwarded scheme
# must be trusted explicitly. Both parts must be set as a pair; never enable
# without the proxy actually overwriting the header.
_proxy_header_name = os.getenv("SECURE_PROXY_SSL_HEADER_NAME", "")
_proxy_header_value = os.getenv("SECURE_PROXY_SSL_HEADER_VALUE", "")
SECURE_PROXY_SSL_HEADER = (
    (_proxy_header_name, _proxy_header_value)
    if _proxy_header_name and _proxy_header_value
    else None
)

# Behind a proxy the Host header is the proxy's; this makes ``get_host()`` read
# X-Forwarded-Host. Enable only with a proxy that overwrites it, or a caller can
# dictate the host validated against ALLOWED_HOSTS.
USE_X_FORWARDED_HOST = _env_bool("USE_X_FORWARDED_HOST", False)

# Secure whenever DEBUG is off, mirroring the JWT refresh cookie, so the three
# auth cookies cannot disagree about transport security.
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", not DEBUG)

# Product image size ceiling in MB, enforced on every upload surface; a
# deployment raising it should raise its web server body limit too.
MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 5)

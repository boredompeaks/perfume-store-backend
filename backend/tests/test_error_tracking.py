import logging
import os
import re
import subprocess
import sys
import tempfile
import types
import urllib.error
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

import config.settings as config_settings
from tests.test_settings_security import run_settings_import

BACKEND_DIR = Path(__file__).resolve().parent.parent

FAKE_DSN = "https://not-a-real-key@o0.ingest.us.example.invalid/0"

STUB_INIT_CALLS = []

PROD_BOOT_ENV = {
    "DJANGO_SECRET_KEY": "x" * 50,
    "DJANGO_ENV": "production",
    "DJANGO_ALLOWED_HOSTS": "shop.example.test",
    "CSRF_TRUSTED_ORIGINS": "https://shop.example.test",
    "DATABASE_URL": "postgres://u:p@db.example.com:5432/perfume_store",
}

BLOCK_NETWORK_PREAMBLE = """
import socket


def _no_network(*args, **kwargs):
    raise AssertionError("the boot opened a socket")


socket.socket.connect = _no_network
socket.socket.connect_ex = _no_network
socket.create_connection = _no_network
socket.getaddrinfo = _no_network
"""


def install_stub_sdk(modules=None):
    integrations = types.ModuleType("sentry_sdk.integrations")
    django_integration = types.ModuleType("sentry_sdk.integrations.django")
    logging_integration = types.ModuleType("sentry_sdk.integrations.logging")

    class DjangoIntegration:
        identifier = "django"

    class LoggingIntegration:
        identifier = "logging"

        def __init__(self, level=None, event_level=None):
            self.level = level
            self.event_level = event_level

    django_integration.DjangoIntegration = DjangoIntegration
    logging_integration.LoggingIntegration = LoggingIntegration

    sentry_sdk = types.ModuleType("sentry_sdk")

    def init(**kwargs):
        STUB_INIT_CALLS.append(kwargs)

    sentry_sdk.init = init
    sentry_sdk.integrations = integrations
    integrations.django = django_integration
    integrations.logging = logging_integration

    stub = {
        "sentry_sdk": sentry_sdk,
        "sentry_sdk.integrations": integrations,
        "sentry_sdk.integrations.django": django_integration,
        "sentry_sdk.integrations.logging": logging_integration,
    }
    if modules is not None:
        modules.update(stub)
    return stub


def stub_sdk_import_fails(modules=None):
    """`sys.modules[name] = None` is the interpreter's own "this import is blocked" sentinel: it raises ImportError without touching the real installation."""
    blocked = {
        "sentry_sdk": None,
        "sentry_sdk.integrations": None,
        "sentry_sdk.integrations.django": None,
        "sentry_sdk.integrations.logging": None,
    }
    if modules is not None:
        modules.update(blocked)
    return blocked


class ErrorTrackingInertWithoutADsnTests(SimpleTestCase):
    def test_init_returns_false_and_never_reaches_the_sdk(self):
        with patch.dict("sys.modules", install_stub_sdk()):
            STUB_INIT_CALLS.clear()
            live = config_settings._init_error_tracking("", "production", 1.0, 0.0)
        self.assertFalse(live)
        self.assertEqual(STUB_INIT_CALLS, [])

    def test_a_missing_sdk_with_a_dsn_warns_instead_of_breaking_the_boot(self):
        with patch.dict("sys.modules", stub_sdk_import_fails()):
            with self.assertLogs("config.settings", level="WARNING") as logs:
                live = config_settings._init_error_tracking(
                    FAKE_DSN, "production", 1.0, 0.0
                )
        self.assertFalse(live)
        self.assertIn("sentry-sdk", logs.output[0])

    def test_boot_without_a_dsn_never_imports_the_sdk(self):
        with tempfile.TemporaryDirectory() as workdir:
            Path(workdir, "sentry_sdk.py").write_text(
                "LOADED = True\ninit = None\n",
                encoding="utf-8",
            )
            snippet = (
                "import sys; import config.settings as s;"
                "print('ENABLED', s.ERROR_TRACKING_ENABLED);"
                "print('DSN_EMPTY', s.SENTRY_DSN == '');"
                "print('SDK_IMPORTED', 'sentry_sdk' in sys.modules);"
                "import sentry_sdk;"
                "print('SENTINEL_LOADED', sentry_sdk.LOADED)"
            )
            # settings calls load_dotenv() at import, so the child needs a scrubbed environment or an untracked local .env could hand it a DSN.
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("DJANGO_", "SENTRY_", "RAZORPAY_", "EMAIL_"))
                and key != "PYTHONPATH"
            }
            env.update(
                {
                    "DJANGO_SECRET_KEY": "x" * 50,
                    "DJANGO_ENV": "production",
                    "DJANGO_ALLOWED_HOSTS": "shop.example.test",
                    "CSRF_TRUSTED_ORIGINS": "https://shop.example.test",
                    "DATABASE_URL": (
                        "postgres://u:p@db.example.com:5432/perfume_store"
                    ),
                    "PYTHONPATH": f"{workdir}{os.pathsep}{BACKEND_DIR}",
                }
            )
            res = subprocess.run(
                [sys.executable, "-c", snippet],
                capture_output=True,
                text=True,
                cwd=tempfile.gettempdir(),
                env=env,
                timeout=60,
            )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ENABLED False", res.stdout)
        self.assertIn("DSN_EMPTY True", res.stdout)
        self.assertIn("SDK_IMPORTED False", res.stdout)
        self.assertIn("SENTINEL_LOADED True", res.stdout)

    def test_a_dsn_configured_at_boot_is_labelled_with_the_declared_env(self):
        snippet = (
            "import sys, types;"
            "calls = [];"
            "sdk = types.ModuleType('sentry_sdk');"
            "sdk.init = lambda **kw: calls.append(kw);"
            "integ = types.ModuleType('sentry_sdk.integrations');"
            "dj = types.ModuleType('sentry_sdk.integrations.django');"
            "lg = types.ModuleType('sentry_sdk.integrations.logging');"
            "dj.DjangoIntegration = type('D', (), {'__init__': lambda self: None});"
            "lg.LoggingIntegration = type('L', (), "
            "{'__init__': lambda self, level=None, event_level=None: None});"
            "sdk.integrations = integ; integ.django = dj; integ.logging = lg;"
            "sys.modules.update({'sentry_sdk': sdk, 'sentry_sdk.integrations': integ,"
            " 'sentry_sdk.integrations.django': dj, 'sentry_sdk.integrations.logging': lg});"
            "import config.settings as s;"
            "print('ENABLED', s.ERROR_TRACKING_ENABLED);"
            "print('ENVIRONMENT', calls[0]['environment']);"
            "print('SAMPLE', calls[0]['sample_rate']);"
            "print('PII', calls[0]['send_default_pii'])"
        )
        res = run_settings_import(
            {
                "DJANGO_SECRET_KEY": "x" * 50,
                "DJANGO_ENV": "staging",
                "DATABASE_URL": "postgres://u:p@db.example.com:5432/perfume_store",
                "DJANGO_ALLOWED_HOSTS": "example.test",
                "CSRF_TRUSTED_ORIGINS": "https://example.test",
                "SENTRY_DSN": FAKE_DSN,
                "SENTRY_ENVIRONMENT": "",
                "SENTRY_SAMPLE_RATE": "0.25",
            },
            snippet=snippet,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ENABLED True", res.stdout)
        self.assertIn("ENVIRONMENT staging", res.stdout)
        self.assertIn("SAMPLE 0.25", res.stdout)
        self.assertIn("PII False", res.stdout)


class MalformedDsnBootTests(SimpleTestCase):
    MALFORMED_DSNS = ("not-a-dsn", "https://")

    def test_a_malformed_dsn_warns_and_stays_off(self):
        for dsn in self.MALFORMED_DSNS:
            with self.subTest(dsn=dsn):
                with self.assertLogs("config.settings", level="WARNING") as logs:
                    live = config_settings._init_error_tracking(
                        dsn, "production", 1.0, 0.0
                    )
                self.assertFalse(live)
                self.assertIn("rejected by the sentry-sdk", logs.output[0])
                self.assertIn("the value in .env", logs.output[0])

    def test_the_settings_import_survives_a_malformed_dsn(self):
        for dsn in self.MALFORMED_DSNS:
            with self.subTest(dsn=dsn):
                res = run_settings_import(
                    {
                        **PROD_BOOT_ENV,
                        "SENTRY_DSN": dsn,
                        "SENTRY_ENVIRONMENT": "",
                    },
                    snippet=(
                        "import config.settings as s;"
                        "print('ENABLED', s.ERROR_TRACKING_ENABLED);"
                        "print('DSN', s.SENTRY_DSN)"
                    ),
                )
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertIn("ENABLED False", res.stdout)
                self.assertIn(f"DSN {dsn}", res.stdout)
                self.assertIn("rejected by the sentry-sdk", res.stderr)

    def test_a_valid_dsn_still_boots_without_touching_the_network(self):
        # A well-formed DSN really does initialise the SDK, and still dials nothing at boot: its transport is lazy.
        res = run_settings_import(
            {**PROD_BOOT_ENV, "SENTRY_DSN": FAKE_DSN, "SENTRY_ENVIRONMENT": ""},
            snippet=(
                BLOCK_NETWORK_PREAMBLE + "import config.settings as s;"
                "print('ENABLED', s.ERROR_TRACKING_ENABLED);"
                "print('CLIENT', bool(__import__('sentry_sdk').get_client()))"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ENABLED True", res.stdout)
        self.assertIn("CLIENT True", res.stdout)

    def test_the_pinned_sdk_is_really_installed(self):
        import sentry_sdk

        lock = (BACKEND_DIR / "requirements.lock").read_text(encoding="utf-8")
        pinned = re.search(r"^sentry-sdk==(\S+)", lock, re.M)
        self.assertIsNotNone(pinned, "requirements.lock must pin sentry-sdk")
        self.assertEqual(sentry_sdk.VERSION, pinned.group(1))


class ErrorTrackingWithADsnTests(SimpleTestCase):
    def setUp(self):
        STUB_INIT_CALLS.clear()
        self.addCleanup(STUB_INIT_CALLS.clear)

    def init_with_stub(self, **kwargs):
        defaults = {
            "dsn": FAKE_DSN,
            "environment": "production",
            "sample_rate": 1.0,
            "traces_sample_rate": 0.0,
        }
        defaults.update(kwargs)
        with patch.dict("sys.modules", install_stub_sdk()):
            live = config_settings._init_error_tracking(**defaults)
        self.assertTrue(live)
        self.assertEqual(len(STUB_INIT_CALLS), 1)
        return STUB_INIT_CALLS[0]

    def test_init_receives_the_dsn_environment_and_sample_rates(self):
        recorded = self.init_with_stub(
            dsn=FAKE_DSN, environment="staging", sample_rate=0.5
        )
        self.assertEqual(recorded["dsn"], FAKE_DSN)
        self.assertEqual(recorded["environment"], "staging")
        self.assertEqual(recorded["sample_rate"], 0.5)
        self.assertEqual(recorded["traces_sample_rate"], 0.0)

    def test_pii_is_never_sent_by_default(self):
        self.assertIs(self.init_with_stub()["send_default_pii"], False)

    def test_django_request_five_hundreds_route_into_the_tracker(self):
        recorded = self.init_with_stub()
        identifiers = [
            getattr(integration, "identifier", None)
            for integration in recorded["integrations"]
        ]
        self.assertIn("django", identifiers)
        self.assertIn("logging", identifiers)
        logging_integration = next(
            integration
            for integration in recorded["integrations"]
            if getattr(integration, "identifier", None) == "logging"
        )
        self.assertEqual(logging.getLevelName(logging_integration.event_level), "ERROR")

    def test_the_request_channel_the_integration_reads_is_the_pinned_one(self):
        self.assertEqual(
            config_settings.LOGGING["loggers"]["django.request"]["level"], "ERROR"
        )


class SampleRateEnvTests(SimpleTestCase):
    def test_absent_keys_take_the_documented_defaults(self):
        env = {k: v for k, v in os.environ.items() if "SENTRY" not in k}
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(config_settings._env_float("SENTRY_SAMPLE_RATE", 1.0), 1.0)
            self.assertEqual(
                config_settings._env_float("SENTRY_TRACES_SAMPLE_RATE", 0.0), 0.0
            )

    def test_a_numeric_value_is_used(self):
        with patch.dict("os.environ", {"SENTRY_SAMPLE_RATE": "0.1"}):
            self.assertEqual(config_settings._env_float("SENTRY_SAMPLE_RATE", 1.0), 0.1)

    def test_an_unparseable_value_falls_back_with_a_warning(self):
        with patch.dict("os.environ", {"SENTRY_SAMPLE_RATE": "everything"}):
            with self.assertLogs("config.settings", level="WARNING") as logs:
                self.assertEqual(
                    config_settings._env_float("SENTRY_SAMPLE_RATE", 1.0), 1.0
                )
        self.assertIn("everything", logs.output[0])


class DockerfileHealthcheckTests(SimpleTestCase):
    @staticmethod
    def healthcheck_command():
        dockerfile = (
            (BACKEND_DIR / "Dockerfile")
            .read_text(encoding="utf-8")
            .replace("\r\n", "\n")
        )
        blocks, buffer = [], ""
        for raw in dockerfile.splitlines():
            line = raw.rstrip()
            if line.endswith("\\"):
                buffer += line[:-1] + " "
                continue
            blocks.append((buffer + line).strip())
            buffer = ""
        instructions = [block for block in blocks if block.startswith("HEALTHCHECK")]
        assert len(instructions) == 1, f"expected one HEALTHCHECK, got {instructions}"
        cmd = re.search(r"CMD\s+python -c \"(.*)\"$", instructions[0], re.S)
        assert cmd, f"HEALTHCHECK is not a quoted `python -c` probe: {instructions[0]}"
        return cmd.group(1)

    def run_probe(self, status=None, raises=False, env=None):
        urls = []

        class Response:
            status = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            urls.append(request)
            if raises:
                raise urllib.error.HTTPError(
                    request.full_url, status, "Service Unavailable", {}, None
                )
            response = Response()
            response.status = status
            return response

        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in ("DJANGO_ALLOWED_HOSTS", "PORT")
        }
        environment["PORT"] = "8000"
        environment.update(env or {})

        code = 0
        with patch.dict(os.environ, environment, clear=True):
            with patch("urllib.request.urlopen", fake_urlopen):
                try:
                    exec(  # noqa: S102 - the committed command under test
                        compile(
                            "import os, sys, urllib.request\n"
                            f"{self.healthcheck_command()}\n",
                            "<dockerfile HEALTHCHECK>",
                            "exec",
                        ),
                        {"__name__": "__healthcheck__"},
                    )
                except SystemExit as exit_request:
                    code = exit_request.code or 0
                except Exception as error:
                    code = f"{type(error).__name__}: {error}"
        return code, urls

    def test_the_probe_targets_the_real_health_endpoint(self):
        self.assertIn("/health/", self.healthcheck_command())

    def test_a_healthy_app_exits_zero(self):
        code, urls = self.run_probe(status=200)
        self.assertEqual(code, 0)
        self.assertEqual(urls[0].full_url, "http://127.0.0.1:8000/health/")

    def test_a_degraded_app_fails_the_healthcheck(self):
        code, _ = self.run_probe(status=503)
        self.assertNotEqual(code, 0)

    def test_an_unreachable_app_fails_the_healthcheck(self):
        code, _ = self.run_probe(raises=True)
        self.assertNotEqual(code, 0)

    def test_the_probe_probes_the_port_the_container_serves_on(self):
        code, urls = self.run_probe(status=200, env={"PORT": "9123"})
        self.assertEqual(code, 0)
        self.assertEqual(urls[0].full_url, "http://127.0.0.1:9123/health/")

    def test_the_probe_identifies_itself_as_a_deployment_would(self):
        code, urls = self.run_probe(
            status=200, env={"DJANGO_ALLOWED_HOSTS": "shop.example.test, alt.test"}
        )
        self.assertEqual(code, 0)
        headers = urls[0].headers
        self.assertEqual(headers["Host"], "shop.example.test")
        self.assertEqual(headers["X-forwarded-proto"], "https")

    def test_the_compose_healthcheck_agrees_with_the_image(self):
        compose = (Path(BACKEND_DIR).parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("http://127.0.0.1:8000/health/", compose)
        self.assertIn("status == 200", compose)

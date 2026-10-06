"""SPEC-22-04 [R-22.13/R-22.12]: error tracking is env-gated, /health/ is probed.

Two independent contracts are pinned here, both of which are invisible to the
rest of the suite because neither an unconfigured app nor a test process ever
crosses the gate:

1. **Inert without a DSN.** `config.settings._init_error_tracking` must import
   the SDK *inside* the DSN gate, so an unconfigured process never loads it,
   never initialises a client and never makes an outbound call. Pinned by
   executing the real settings import in a clean subprocess (the same pattern
   the DEBUG/DATABASE_URL guards use, because these are import-time facts) and
   by asserting the SDK is absent from `sys.modules` afterwards. A sentinel
   module planted on `sys.path` is what makes that non-vacuous: if the import
   moved to module scope, the sentinel would be loaded and the pin goes red.
2. **Live with a DSN, without sending anything.** With a DSN set, the SDK is
   initialised with the DSN, the declared environment, the sample rates and no
   PII - asserted against a *stub* module, so nothing is ever sent. The
   `django.request` routing claim is pinned structurally: the integrations
   handed to `init()` must be DjangoIntegration plus a LoggingIntegration whose
   `event_level` is ERROR, which is what turns the ERROR-level `django.request`
   records `_build_logging` already pins into events.
3. **A DSN the SDK refuses must not refuse the boot.** `MalformedDsnBootTests`
   pins this against the REAL SDK, because only the real one raises
   `BadDsn`: a settings module executes `init()` at import, so an un-guarded
   raise there is a dead deployment rather than one broken worker.

The container probe is pinned by EXECUTION: the `HEALTHCHECK` command is
parsed out of the committed Dockerfile and run against a stubbed
`urllib.request`, so the 200-vs-degraded decision is proved rather than
pattern-matched. Nothing in this module touches the network or a live server.
"""

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

# Obviously not a DSN: no public key, an .invalid host (RFC 2606 guarantees it
# never resolves) and a project id of 0. Nothing here can reach a real ingest
# endpoint even if a test did try.
FAKE_DSN = "https://not-a-real-key@o0.ingest.us.example.invalid/0"

# The stub the "with a DSN" path is given instead of the real SDK. `init` is
# recorded, never called through to a transport.
STUB_INIT_CALLS = []

# The minimum a production boot must be given to get past the SPEC-22-03
# guards, so a test can vary SENTRY_* and nothing else. Spelled out here (as
# the settings module would read it) rather than imported from the settings
# tests, because a scrubbed environment plus these keys IS the deployment
# being simulated.
PROD_BOOT_ENV = {
    "DJANGO_SECRET_KEY": "x" * 50,
    "DJANGO_ENV": "production",
    "DJANGO_ALLOWED_HOSTS": "shop.example.test",
    "CSRF_TRUSTED_ORIGINS": "https://shop.example.test",
    "DATABASE_URL": "postgres://u:p@db.example.com:5432/perfume_store",
}

# Run in the child before importing settings, so a socket call made during
# the boot fails the pin instead of reaching the network (conventions.md:
# tests must not hit the network). Each entry raises with a fixed message.
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
    """Return a fake `sentry_sdk` package tree for the init path to import.

    Built as real `types.ModuleType` objects and spliced into `sys.modules` by
    the caller, so `import sentry_sdk` and the two integration imports resolve
    to them exactly as they would to the installed SDK. `init` only records
    its arguments.
    """
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
    """Make `import sentry_sdk` raise ImportError, as an uninstalled SDK would.

    `sys.modules[name] = None` is the interpreter's own "this import is
    blocked" sentinel: it raises ImportError on import without touching the
    real installation, so the not-installed branch is testable whether or not
    the SDK happens to be in the environment.
    """
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
    """(a) With no DSN the app boots and the integration does nothing at all."""

    def test_init_returns_false_and_never_reaches_the_sdk(self):
        # The stub is importable and would record an init call, so a pin that
        # passes with no recorded call is not passing because the import failed.
        with patch.dict("sys.modules", install_stub_sdk()):
            STUB_INIT_CALLS.clear()
            live = config_settings._init_error_tracking("", "production", 1.0, 0.0)
        self.assertFalse(live)
        self.assertEqual(STUB_INIT_CALLS, [])

    def test_a_missing_sdk_with_a_dsn_warns_instead_of_breaking_the_boot(self):
        # Losing error reporting must not be able to refuse the boot: an
        # operator who sets a DSN on a host whose image predates the
        # dependency still gets a storefront, plus a named warning.
        with patch.dict("sys.modules", stub_sdk_import_fails()):
            with self.assertLogs("config.settings", level="WARNING") as logs:
                live = config_settings._init_error_tracking(
                    FAKE_DSN, "production", 1.0, 0.0
                )
        self.assertFalse(live)
        self.assertIn("sentry-sdk", logs.output[0])

    def test_boot_without_a_dsn_never_imports_the_sdk(self):
        # The strong form of (a), in a clean subprocess: the sentinel module is
        # on sys.path precisely so that an import at settings-import time would
        # be observable. If the import ever moved to module scope (or the gate
        # were dropped), this prints SENTINEL_LOADED and the pin fails.
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
            # settings calls load_dotenv() at import, so the child needs a
            # scrubbed environment or an untracked local .env (or the developer's
            # shell) could hand it a DSN and satisfy a pin it never earned. The
            # non-debug shape is spelled out (config.settings refuses an
            # incomplete one), which is also the environment a real deployment
            # boots in - the one where the DSN would be set.
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
                cwd=tempfile.gettempdir(),  # neutral cwd: no .env is discovered
                env=env,
                timeout=60,
            )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ENABLED False", res.stdout)
        self.assertIn("DSN_EMPTY True", res.stdout)
        self.assertIn("SDK_IMPORTED False", res.stdout)
        self.assertIn("SENTINEL_LOADED True", res.stdout)

    def test_a_dsn_configured_at_boot_is_labelled_with_the_declared_env(self):
        # The end-to-end shape of (b) through a real settings import: a DSN in
        # the environment reaches init(), labelled with DJANGO_ENV rather than
        # a hardcoded "production", so a staging boot cannot file its errors
        # under production (SPEC-22-03 [R-22.3]).
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
                # Spelled out rather than inherited: the helper scrubs the
                # DJANGO_/RAZORPAY_/EMAIL_ families, so a SENTRY_* value in a
                # developer's shell could otherwise label this boot and make
                # the pin below assert something the repo never set.
                "SENTRY_ENVIRONMENT": "",
                "SENTRY_SAMPLE_RATE": "0.25",
            },
            snippet=snippet,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ENABLED True", res.stdout)
        self.assertIn("ENVIRONMENT staging", res.stdout)
        self.assertIn("SAMPLE 0.25", res.stdout)
        # No user data leaves the process unless an operator opts in.
        self.assertIn("PII False", res.stdout)


class MalformedDsnBootTests(SimpleTestCase):
    """A DSN the SDK refuses degrades to a warning; it never refuses the boot.

    Everything here runs against the REAL SDK, not the recorder stub above,
    because the failure being pinned is one only the real SDK produces:
    `sentry_sdk.init()` raises `sentry_sdk.parsers.BadDsn` out of its own DSN
    parser. Left un-guarded that raise happens while `config.settings` is
    being EXECUTED, so it is not one broken process but a dead deployment -
    every gunicorn worker, and the release script's migrate checkpoint that
    imports settings to find the database. The stub cannot catch it, which is
    exactly why the bug shipped.

    requirements.txt pins the package, so the real one is importable in CI and
    in a venv built from it; `test_the_pinned_sdk_is_really_installed` keeps
    that honest here rather than letting the pins below pass vacuously.
    """

    # Two operator-reachable shapes: a project name pasted into the DSN slot,
    # and a value truncated by a bad edit of the .env file.
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
                # The warning names the remedy, because this fires once at
                # boot into a log nobody is watching. It deliberately does NOT
                # echo the DSN: that value carries the project's public key.
                self.assertIn("the value in .env", logs.output[0])

    def test_the_settings_import_survives_a_malformed_dsn(self):
        # The P1 shape, at the level where it bites: the whole settings
        # import, in a clean subprocess with the real SDK. If init() ever
        # moves back outside the guard, this goes red with a BadDsn traceback
        # and a non-zero exit rather than passing quietly.
        for dsn in self.MALFORMED_DSNS:
            with self.subTest(dsn=dsn):
                res = run_settings_import(
                    {
                        **PROD_BOOT_ENV,
                        "SENTRY_DSN": dsn,
                        # Spelled out, not inherited: this helper scrubs the
                        # DJANGO_/RAZORPAY_/EMAIL_ families only, so a DSN or
                        # label in the developer's shell could otherwise decide
                        # what this boot does.
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
                # The gate still reads what was configured; only the SDK's
                # opinion of it changed.
                self.assertIn(f"DSN {dsn}", res.stdout)
                self.assertIn("rejected by the sentry-sdk", res.stderr)

    def test_a_valid_dsn_still_boots_without_touching_the_network(self):
        # The counterpart, so the fix cannot be "catch everything and do
        # nothing": a well-formed DSN really does initialise the SDK - and
        # still dials nothing at boot, because its transport is lazy and
        # sends on the first event from a worker. `socket` is blocked at the
        # call sites rather than by replacing `socket.socket`, which `ssl`
        # subclasses and which would break the import instead of the pin.
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
        # The pin in requirements.lock is a promise until the package is
        # importable: an environment that never installed it would make every
        # "against the real SDK" pin above pass vacuously, down the
        # ImportError branch. The expected version is READ OUT OF THE PIN
        # rather than restated, so bumping it cannot turn this into a lie.
        #
        # requirements.lock, not requirements.txt: the direct-dependency file
        # is now a POINTER (`-r requirements.lock`) and carries no pins of its
        # own, so the regex would find nothing there. The assertion is
        # unchanged in strength -- it still reads the shipped pin and compares
        # it to what is actually importable -- only the file holding the pin
        # moved, which is why the path is derived rather than hardcoded twice.
        import sentry_sdk

        lock = (BACKEND_DIR / "requirements.lock").read_text(encoding="utf-8")
        pinned = re.search(r"^sentry-sdk==(\S+)", lock, re.M)
        self.assertIsNotNone(pinned, "requirements.lock must pin sentry-sdk")
        self.assertEqual(sentry_sdk.VERSION, pinned.group(1))


class ErrorTrackingWithADsnTests(SimpleTestCase):
    """(b) With a DSN the SDK is initialised — against a stub, never a network."""

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
        # Request bodies, cookies and identifiers stay inside the process
        # unless an operator deliberately turns this on.
        self.assertIs(self.init_with_stub()["send_default_pii"], False)

    def test_django_request_five_hundreds_route_into_the_tracker(self):
        # The claim that makes this real rather than decorative: LOGGING pins
        # `django.request` at ERROR, and a LoggingIntegration at event_level
        # ERROR is what turns those records into events. Both integrations
        # must be handed to init(), and the logging one must be error-level.
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
        # ...and the channel it reads is the one the app configures. If
        # django.request were moved off ERROR, request 5xx would stop being
        # events while this pin still passed.
        self.assertEqual(
            config_settings.LOGGING["loggers"]["django.request"]["level"], "ERROR"
        )


class SampleRateEnvTests(SimpleTestCase):
    """The env-driven knobs fail safe, exactly like every other resolver."""

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
        # A typo in an env file must not take the app down at import.
        with patch.dict("os.environ", {"SENTRY_SAMPLE_RATE": "everything"}):
            with self.assertLogs("config.settings", level="WARNING") as logs:
                self.assertEqual(
                    config_settings._env_float("SENTRY_SAMPLE_RATE", 1.0), 1.0
                )
        self.assertIn("everything", logs.output[0])


class DockerfileHealthcheckTests(SimpleTestCase):
    """(c) The image HEALTHCHECK probes the real /health/ and fails when degraded.

    The command is parsed out of the committed Dockerfile and EXECUTED against
    a stubbed `urllib.request`, so the decision under test is the real one: a
    200 exits 0 and anything else — including the 503 ops.views.health answers
    while degraded — exits non-zero. No server is started and nothing is
    fetched.
    """

    @staticmethod
    def healthcheck_command():
        """Return the Python program the image's HEALTHCHECK runs.

        Parsed out of the committed Dockerfile rather than restated here, so a
        pin can never quietly agree with a stale copy of the line: whatever the
        image actually runs is what the tests below execute. Backslash
        continuations are joined first, because the command of a HEALTHCHECK
        sits on the last of them.
        """
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
        """Execute the committed probe with `urlopen` stubbed.

        Returns (exit_code, requests). The program is the Dockerfile's own
        HEALTHCHECK body, executed in-process with the socket it opens faked,
        so the sys.exit(...) decision under test is really taken - no server is
        started and no request leaves the process (conventions.md: tests must
        not hit the network). `urlopen` raising (the 503 path in a real
        container) surfaces as a non-zero exit exactly as it would there.
        """
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
                except Exception as error:  # a probe that cannot reach /health/
                    code = f"{type(error).__name__}: {error}"
        return code, urls

    def test_the_probe_targets_the_real_health_endpoint(self):
        self.assertIn("/health/", self.healthcheck_command())

    def test_a_healthy_app_exits_zero(self):
        code, urls = self.run_probe(status=200)
        self.assertEqual(code, 0)
        self.assertEqual(urls[0].full_url, "http://127.0.0.1:8000/health/")

    def test_a_degraded_app_fails_the_healthcheck(self):
        # The point of probing the real endpoint: ops.views.health answers 503
        # while the database or the media mount is unusable. A check that
        # accepted any response would call that healthy.
        code, _ = self.run_probe(status=503)
        self.assertNotEqual(code, 0)

    def test_an_unreachable_app_fails_the_healthcheck(self):
        # urlopen raises on a connection failure, and an uncaught raise is a
        # non-zero exit - the other half of "not just the process is up".
        code, _ = self.run_probe(raises=True)
        self.assertNotEqual(code, 0)

    def test_the_probe_probes_the_port_the_container_serves_on(self):
        # ${PORT} is what the CMD binds, so a platform injecting a different
        # port must be probed there - on the wire, not just in the text.
        code, urls = self.run_probe(status=200, env={"PORT": "9123"})
        self.assertEqual(code, 0)
        self.assertEqual(urls[0].full_url, "http://127.0.0.1:9123/health/")

    def test_the_probe_identifies_itself_as_a_deployment_would(self):
        # SPEC-22-08: with SECURE_SSL_REDIRECT on, a bare http probe is 301'd
        # to an https:// the container does not serve, and an unlisted Host is
        # a 400 - so the probe carries the first declared host and the trusted
        # forwarded scheme. Asserted on the request the probe actually built.
        code, urls = self.run_probe(
            status=200, env={"DJANGO_ALLOWED_HOSTS": "shop.example.test, alt.test"}
        )
        self.assertEqual(code, 0)
        headers = urls[0].headers
        self.assertEqual(headers["Host"], "shop.example.test")
        self.assertEqual(headers["X-forwarded-proto"], "https")

    def test_the_compose_healthcheck_agrees_with_the_image(self):
        # One endpoint, one decision: the compose healthcheck must probe the
        # same URL and accept the same answer, or `docker compose` and `docker
        # run` disagree about whether this deployment is healthy.
        compose = (Path(BACKEND_DIR).parent / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("http://127.0.0.1:8000/health/", compose)
        self.assertIn("status == 200", compose)

"""Pins for the deployment contract (SPEC-2-10b orchestration + SPEC-22-07).

The production path after SPEC-2-10a lives in files Python never imports -
docker-compose.yml, Procfile, scripts/release.sh and the deploy workflow - so
nothing in the suite would notice if a release silently grew a second migrate
path, started targeting master, probed a URL that is not the real /health/,
or baked a secret into the repo. These tests are the regression net for those
four properties, checked against the committed files themselves.

Deliberately no YAML library: PyYAML is not a declared dependency (it is not in
requirements.txt, and adding it would put a parser in the production image for
the sake of a test), so the assertions read the files as text. The compose file
is schema-validated where it matters - `docker compose config` - not here.

Gate fix cycle 1 (SPEC-2-04/SPEC-22-03) added the two classes at the bottom:
the image's build-time collectstatic layer and the compose runtime env contract
now have to SATISFY the non-debug guards, and both are proved the only way that
means anything - by booting the app with exactly the env those files commit,
and by watching a guard key being dropped and the boot still being refused.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from tests.test_settings_security import run_settings_import

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
COMPOSE = "docker-compose.yml"
PROCFILE = "Procfile"
RELEASE = "scripts/release.sh"
DEPLOY_WORKFLOW = ".github/workflows/deploy.yml"
BACKEND_TESTS_WORKFLOW = ".github/workflows/backend-tests.yml"
RUNBOOK = "backend/docs/deploy-runbook.md"

# Everything that decides how the deployable unit runs.
DEPLOYMENT_FILES = (
    COMPOSE,
    PROCFILE,
    RELEASE,
    DEPLOY_WORKFLOW,
    BACKEND_TESTS_WORKFLOW,
)

# The files this change authors. backend-tests.yml is excluded from the
# credential scan below because it has carried a deliberate dummy
# RAZORPAY_KEY_SECRET since the CI suite was written (it proves the suite never
# touches live credentials) - a pre-existing, reviewed value, not something a
# deployment file may do.
AUTHORED_DEPLOYMENT_FILES = (COMPOSE, PROCFILE, RELEASE, DEPLOY_WORKFLOW)

# An actual migration invocation - `manage migrate`, `manage.py migrate`. This
# is deliberately not a bare "migrate" search: it must match a second migrate
# PATH (a Procfile release line, a compose command, a CI step) without matching
# the release script's own prose about migrating.
MIGRATE = re.compile(r"\bmanage(\.py)?\s+migrate\b")

# An assignment whose NAME looks like a credential must resolve through the
# environment, never carry a literal. Matched names are deliberately specific:
# a bare `key=` is a temp file name, and `SECRET_SCAN_*` is a workflow name, so
# neither is treated as a secret here.
SECRET_NAME = re.compile(r"(?i)(secret|password|passwd|token|ssh_key|api_key)")
ASSIGNMENT = re.compile(r"^\s*(?:-?\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*)$")


def read(relative_path):
    """Return a checkout-independent copy of a committed file (no CRLF)."""
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"{relative_path} is missing from the repository"
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def code_lines(text):
    """Drop comment-only lines so prose can never satisfy a contract check."""
    return [line for line in text.splitlines() if not line.strip().startswith("#")]


# Every key a DJANGO_DEBUG=false boot must declare before config.settings will
# import (SPEC-22-03 [R-22.3]). Listed here because the two deployment files
# below have to carry it: a build layer and a runtime container are both
# non-debug boots, and the guard is deliberately blind to which one it is.
NON_DEBUG_REQUIRED_KEYS = (
    "DJANGO_SECRET_KEY",
    "DJANGO_ENV",
    "DATABASE_URL",
    "DJANGO_ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
)

# Run inside the container's own interpreter and settings, so this is the
# gunicorn load path (the image CMD hands it exactly this module) followed by
# the probe the compose healthcheck performs against /health/.
RUNTIME_BOOT_SNIPPET = (
    "import config.wsgi as wsgi;"
    "print('WSGI', type(wsgi.application).__name__);"
    "from django.test import Client;"
    "print('HEALTH', Client(headers={'host': 'shop.example.test'})"
    ".get('/health/').status_code)"
)


def dockerfile_build_env():
    """Return the env the image's collectstatic layer runs under, as committed.

    Parsed out of the Dockerfile instead of restated here, so a pin can never
    quietly agree with a stale copy of the RUN line: whatever the image
    actually sets is exactly what the tests below boot the app with. Backslash
    continuations are joined first, because the command of an env-setting RUN
    sits on the last of them.
    """
    logical, buffer = [], ""
    for raw in read("backend/Dockerfile").splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical.append((buffer + line).strip())
        buffer = ""
    layers = [
        line for line in logical if line.startswith("RUN ") and "collectstatic" in line
    ]
    assert len(layers) == 1, f"expected one collectstatic layer, got {layers}"
    env = {}
    for token in layers[0].split()[1:]:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            break  # the command itself, not an environment assignment
        key, _, value = token.partition("=")
        env[key] = value
    return env


def compose_backend_environment():
    """Return the backend service's compose `environment:` mapping, as text."""
    text = read(COMPOSE)
    service = re.search(r"^  backend:\s*$", text, re.M)
    assert service, "docker-compose.yml has no backend service"
    block = text[service.end() :]
    end = re.search(r"^(?:  \S|\S)", block, re.M)
    block = block[: end.start()] if end else block
    environment = re.search(r"^    environment:\s*$", block, re.M)
    assert environment, "the backend service has no environment: block"
    values = block[environment.end() :]
    sibling = re.search(r"^    \S", values, re.M)
    values = values[: sibling.start()] if sibling else values
    return {
        match.group(1): match.group(2)
        for match in re.finditer(
            r"^      ([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", values, re.M
        )
    }


def run_backend(argv, env):
    """Run a command in the backend checkout against a hermetic environment.

    Only `env` decides what config.settings sees: the inherited DJANGO_*,
    RAZORPAY_* and EMAIL_* names (plus the ones load_dotenv() may already have
    pushed into this process) are stripped, so a developer's local .env can
    neither satisfy a guard nor mask one.
    """
    scrubbed = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DJANGO_", "RAZORPAY_", "EMAIL_"))
    }
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=str(BACKEND_DIR),
        env={**scrubbed, **env},
        timeout=300,
    )


def manage(*argv, env):
    """Run `manage.py <argv>` in the hermetic environment above."""
    return run_backend([sys.executable, "manage.py", *argv], env)


class DeploymentFilesExistTests(SimpleTestCase):
    """The four files are the deliverable; a missing one must be loud."""

    def test_every_deployment_file_is_committed(self):
        for relative_path in DEPLOYMENT_FILES:
            with self.subTest(file=relative_path):
                self.assertTrue(
                    (REPO_ROOT / relative_path).is_file(),
                    f"{relative_path} is missing",
                )

    def test_release_script_starts_with_a_usable_shebang(self):
        # A BOM or a leading blank line here breaks `bash scripts/release.sh`
        # with an error that points at the wrong file entirely.
        raw = (REPO_ROOT / RELEASE).read_bytes()
        with self.subTest():
            self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\n"))

    def test_repository_does_not_force_crlf_onto_shell_scripts(self):
        # The deploy host is Linux, where a CRLF release script fails its FIRST
        # line (`set -euo pipefail\r` is an unknown option). Git stores LF and
        # checks out LF on Linux, so only an explicit eol=crlf mapping - or a
        # Windows checker's autocrlf - could reintroduce the hazard.
        gitattributes = REPO_ROOT / ".gitattributes"
        rules = (
            code_lines(gitattributes.read_text(encoding="utf-8"))
            if gitattributes.exists()
            else []
        )
        for line in rules:
            with self.subTest(line=line):
                self.assertNotRegex(
                    line,
                    r"\S+\s+eol\s*=\s*crlf",
                    "CRLF shell scripts break the release",
                )


class ComposeOrchestrationTests(SimpleTestCase):
    """SPEC-2-10b: the image is wired to a database and probed by /health/."""

    def setUp(self):
        self.text = read(COMPOSE)
        self.code = "\n".join(code_lines(self.text))

    def test_backend_service_builds_the_committed_image(self):
        self.assertIn("context: ./backend", self.code)
        self.assertIn("image: perfume-backend:latest", self.code)

    def test_database_service_is_postgres_with_a_pg_isready_healthcheck(self):
        self.assertIn("image: postgres:17-alpine", self.code)
        self.assertIn("pg_isready", self.code)
        # The data must outlive the container, or a release would "succeed"
        # against an empty database.
        self.assertIn("postgres_data:/var/lib/postgresql/data", self.code)

    def test_backend_waits_for_a_healthy_database(self):
        # `service_started` would race the first migration.
        self.assertIn("condition: service_healthy", self.code)

    def test_database_url_points_at_the_db_service_not_localhost(self):
        match = re.search(r"DATABASE_URL:\s*(.+)", self.code)
        self.assertIsNotNone(match, "compose must pin DATABASE_URL")
        value = match.group(1)
        self.assertIn("@db:5432", value)
        self.assertNotIn("localhost", value)
        # The in-network default is explicitly unencrypted; a REMOTE database
        # carries its own sslmode (settings forwards the query params).
        self.assertIn("sslmode=disable", value)

    def test_healthcheck_probes_the_real_health_endpoint(self):
        # ops.views.health answers 503 when degraded, so the probe must treat a
        # non-200 as unhealthy - asserting on the URL alone would let a check
        # that ignores the status code through.
        self.assertIn("/health/", self.code)
        self.assertIn("urlopen", self.code)
        self.assertIn("status == 200", self.code)
        self.assertIn("except Exception", self.code)

    def test_healthcheck_port_matches_the_image_default(self):
        dockerfile = read("backend/Dockerfile")
        self.assertIn("ENV PORT=8000", dockerfile)
        self.assertIn("http://127.0.0.1:8000/health/", self.code)

    def test_app_environment_comes_from_the_env_file(self):
        self.assertIn("env_file:", self.code)
        self.assertIn("required: false", self.code)
        # DJANGO_SECRET_KEY is never set here: settings owns the fail-closed
        # guard, so compose must not shadow it with a literal.
        self.assertNotIn("DJANGO_SECRET_KEY:", self.code)


class ProcfileTests(SimpleTestCase):
    """SPEC-2-10b: platform runs serve through the same gunicorn command."""

    def setUp(self):
        self.lines = code_lines(read(PROCFILE))

    def gunicorn_argv(self, command):
        tokens = command.split()
        return tokens[tokens.index("gunicorn") :]

    def test_web_line_runs_gunicorn_against_the_wsgi_app(self):
        web = [ln for ln in self.lines if ln.startswith("web:")]
        self.assertEqual(len(web), 1, "expected exactly one web: line")
        self.assertIn("config.wsgi:application", web[0])
        # The monorepo puts the Django project in backend/.
        self.assertIn("cd backend", web[0])

    def test_web_line_matches_the_container_command(self):
        # The CMD is JSON (`sh -c "exec gunicorn ..."`), so the comparison drops
        # the quoting the array syntax adds rather than comparing punctuation.
        dockerfile = code_lines(read("backend/Dockerfile"))
        cmd = next(ln for ln in dockerfile if ln.startswith("CMD "))
        argv = self.gunicorn_argv(cmd)
        argv[-1] = argv[-1].strip('"]')
        web = next(ln for ln in self.lines if ln.startswith("web:"))
        self.assertEqual(
            self.gunicorn_argv(web),
            argv,
            "the Procfile and the image CMD must stay the same gunicorn command",
        )

    def test_no_release_line_so_there_is_no_second_migrate_path(self):
        self.assertNotIn("migrate", "\n".join(self.lines))
        self.assertFalse(
            [ln for ln in self.lines if ln.split(":", 1)[0] in ("release", "worker")],
            "only a web process belongs in this Procfile",
        )


class ReleaseScriptTests(SimpleTestCase):
    """SPEC-22-07: one sequence, one migrate, and a fail-closed checkpoint."""

    def setUp(self):
        self.text = read(RELEASE)
        self.code = "\n".join(code_lines(self.text))

    def test_script_is_fail_closed(self):
        self.assertIn("set -euo pipefail", self.code)
        # It must refuse before touching anything when the secrets are absent.
        self.assertIn("[ -f .env ] || fail", self.code)

    def test_sequence_order(self):
        # Each index is the FIRST occurrence, so the order asserted is the
        # order the release actually runs in.
        positions = [
            ("compose build", self.code.index("compose build")),
            (
                "compose up db",
                self.code.index(
                    'compose up -d --wait --wait-timeout "$DB_WAIT_SECONDS"'
                ),
            ),
            ("migration check", self.code.index("makemigrations --check --dry-run")),
            ("migrate", self.code.index("manage migrate --noinput")),
            ("collectstatic", self.code.index("manage collectstatic --noinput")),
            # The last wait is the app restart, after the assets are in place.
            ("restart", self.code.rindex("compose up -d --wait")),
        ]
        self.assertEqual(
            [name for name, _ in positions],
            sorted([name for name, _ in positions], key=lambda n: dict(positions)[n]),
            f"release steps out of order: {positions}",
        )

    def test_migration_review_checkpoint_fails_clearly(self):
        # R-22.11: a model change without a committed migration stops the
        # release, and the operator is told what to do about it.
        self.assertIn("if ! manage makemigrations --check --dry-run; then", self.code)
        self.assertIn("makemigrations", self.code)
        self.assertIn("commit the generated files", self.code)
        # The checkpoint runs BEFORE migrate, or it protects nothing.
        self.assertLess(
            self.code.index("makemigrations --check --dry-run"),
            self.code.index("manage migrate --noinput"),
        )

    def test_management_commands_run_on_the_released_image(self):
        # Migrations must apply the code that is shipping, not whatever a host
        # venv happens to have checked out.
        self.assertIn('compose run --rm -T "$SERVICE" python manage.py', self.code)

    def test_uses_the_orchestration_from_spec_2_10b(self):
        self.assertIn("COMPOSE_FILE=docker-compose.yml", self.code)
        self.assertIn("SERVICE=backend", self.code)
        self.assertIn("DB_SERVICE=db", self.code)

    def test_script_never_echoes_the_environment(self):
        for forbidden in ("set -x", "cat .env", "printenv", "env |", "echo $"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, self.code)


class SingleMigratePathTests(SimpleTestCase):
    """Exactly one release-time migrate, anywhere in the deployment surface."""

    def migrate_sites_of(self, text):
        return [line.strip() for line in code_lines(text) if MIGRATE.search(line)]

    def migrate_sites(self):
        sites = []
        for relative_path in DEPLOYMENT_FILES:
            for line in code_lines(read(relative_path)):
                if MIGRATE.search(line):
                    sites.append((relative_path, line.strip()))
        return sites

    def test_release_migrate_exists_and_lives_only_in_the_release_script(self):
        sites = self.migrate_sites()
        self.assertEqual(
            len(sites),
            1,
            f"expected exactly one release-time migrate, found {sites}",
        )
        self.assertEqual(sites[0][0], RELEASE)
        self.assertIn("--noinput", sites[0][1])

    def test_pre_merge_drift_gate_is_still_separate_and_intact(self):
        # The pre-merge check (makemigrations --check, in CI) is NOT a migrate
        # and must stay: the release checkpoint is a second opinion at deploy
        # time, not a replacement for review before merge.
        workflow = "\n".join(code_lines(read(BACKEND_TESTS_WORKFLOW)))
        self.assertIn("makemigrations --check --dry-run", workflow)
        self.assertEqual(self.migrate_sites_of(workflow), [])


class DeployWorkflowTests(SimpleTestCase):
    """SPEC-22-07: the deploy depends on the existing gates and never master."""

    def setUp(self):
        self.text = read(DEPLOY_WORKFLOW)
        self.code = "\n".join(code_lines(self.text))

    def test_never_targets_master(self):
        # master is released by promotion (a merge), never by a push-triggered
        # deploy, so it must not appear in ANY branch list. The prose around it
        # is free to mention master - only configuration counts.
        for match in re.finditer(r"branches:\s*\[([^\]]*)\]", self.code):
            self.assertNotIn("master", match.group(1))
        default = re.search(r"vars\.DEPLOY_BRANCHES \|\| '([^']*)'", self.code)
        self.assertIsNotNone(default, "the release branch list must be explicit")
        self.assertNotIn("master", default.group(1))
        # And the list is not accidentally empty, which would deploy nothing.
        self.assertTrue(default.group(1).strip())

    def test_depends_on_the_existing_gates_and_invents_no_new_one(self):
        self.assertIn("workflow_run:", self.code)
        self.assertIn("workflows: [backend-tests]", self.code)
        self.assertIn("secret-scan.yml", self.code)
        # No second test run: a duplicated gate is a second opinion nobody
        # reads, and one that can disagree with backend-tests.
        for forbidden in ("manage.py test", "coverage run", "pytest", "npm run test"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, self.code)

    def test_no_literal_credentials(self):
        offenders = []
        for line in code_lines(self.text):
            match = ASSIGNMENT.match(line)
            if not match or not SECRET_NAME.search(match.group(1)):
                continue
            if "${" not in match.group(2):
                offenders.append(line.strip())
        self.assertEqual(
            offenders, [], f"literal credentials in the workflow: {offenders}"
        )

    def test_runs_the_release_script_on_the_deploy_host(self):
        self.assertIn("./scripts/release.sh", self.code)
        self.assertIn("set -euo pipefail", self.code)
        # The host checks out the exact commit the gate approved, so what
        # releases is what was tested.
        self.assertIn("needs.gate.outputs.sha", self.code)
        self.assertIn("checkout --quiet --detach", self.code)


class BackendTestsGateTests(SimpleTestCase):
    """The deploy's dependency must actually run on the pushes that deploy."""

    def test_backend_suite_gates_the_deployment_files_too(self):
        workflow = read(BACKEND_TESTS_WORKFLOW)
        for relative_path in (COMPOSE, PROCFILE, RELEASE, DEPLOY_WORKFLOW):
            with self.subTest(file=relative_path):
                self.assertIn(
                    relative_path,
                    workflow,
                    "backend-tests.yml paths must include the deployment wiring, "
                    "or a commit that only changes it can never be released",
                )


class NoHardcodedSecretsTests(SimpleTestCase):
    """Nothing in the deployment surface carries a credential literally."""

    def test_secret_shaped_assignments_are_interpolated(self):
        offenders = []
        for relative_path in AUTHORED_DEPLOYMENT_FILES:
            for line in code_lines(read(relative_path)):
                match = ASSIGNMENT.match(line)
                if not match or not SECRET_NAME.search(match.group(1)):
                    continue
                if "${" not in match.group(2):
                    offenders.append(f"{relative_path}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "credentials must come from the environment: " + "; ".join(offenders),
        )


class BuildLayerSatisfiesTheNonDebugGuardsTests(SimpleTestCase):
    """Gate fix cycle 1 (SPEC-2-04/22-03): `docker build` was dead, not
    merely unguarded.

    The image collects its assets in a build layer that runs with
    DJANGO_DEBUG=false, so SPEC-22-03's fail-closed guards apply to it exactly
    as they do to a container: a build that cannot declare an environment, a
    usable DATABASE_URL, explicit hosts/origins and a secret key does not build
    at all, and the promoted image stopped building. Every boot below uses
    EXACTLY the env the committed RUN line sets - no helper keys - so nothing
    here can be satisfied by something the Dockerfile does not actually do.
    """

    def setUp(self):
        self.build_env = dockerfile_build_env()

    def test_build_layer_declares_every_key_a_non_debug_boot_requires(self):
        self.assertEqual(self.build_env.get("DJANGO_DEBUG"), "false")
        for key in NON_DEBUG_REQUIRED_KEYS:
            with self.subTest(key=key):
                self.assertTrue(
                    self.build_env.get(key),
                    f"the collectstatic layer does not declare {key}",
                )

    def test_build_layer_env_names_a_declared_environment(self):
        # A misspelling would refuse the build, so the layer's environment
        # name has to be one the settings module actually accepts.
        self.assertIn(
            self.build_env["DJANGO_ENV"],
            ("local", "ci", "staging", "production"),
        )

    def test_build_layer_bakes_no_secret_and_names_no_real_database(self):
        # A secret in an image is readable by anyone who can pull it, so the
        # placeholder has to announce itself as one.
        self.assertIn("not-a-real-secret", self.build_env["DJANGO_SECRET_KEY"])
        database_url = self.build_env["DATABASE_URL"]
        # collectstatic opens no connection, but the guard cannot know that, so
        # the URL only has to be parseable. It must be a throwaway file under a
        # temp directory: never a Postgres host, never credentials, never the
        # application's own development database.
        self.assertTrue(database_url.startswith("sqlite:///"), database_url)
        self.assertNotIn("@", database_url)
        self.assertTrue(
            database_url.removeprefix("sqlite:///").startswith("/tmp/"),
            f"the build-layer database is not a throwaway temp path: {database_url}",
        )

    def test_build_layer_env_passes_djangos_system_checks(self):
        # The layer's env verbatim - nothing overridden, because the container
        # path it names (/app/staticfiles) is only ever read by collectstatic,
        # so a test host never has to create it to run the checks.
        res = manage("check", env=dict(self.build_env))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("no issues", res.stdout)

    def test_collectstatic_succeeds_under_the_build_layer_env(self):
        # The end-to-end proof that the image builds again. Only the OUTPUT
        # directory is redirected, to a temp dir this test owns and removes:
        # /app/staticfiles exists inside the image, not on a test host.
        with tempfile.TemporaryDirectory() as static_root:
            res = manage(
                "collectstatic",
                "--noinput",
                env={**self.build_env, "DJANGO_STATIC_ROOT": static_root},
            )
            gathered = list(Path(static_root).rglob("*.css"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(gathered, "collectstatic gathered no CSS")

    def test_the_build_layer_env_still_refuses_when_a_guard_key_is_dropped(self):
        # Why the layer needs the keys at all: drop any one of them and the
        # same boot is still refused by name. This is also the proof that the
        # layer's env SATISFIES the guard rather than routing around it, and
        # that nothing here weakened the production refusal.
        for key in NON_DEBUG_REQUIRED_KEYS:
            with self.subTest(key=key):
                res = run_settings_import(
                    {k: v for k, v in self.build_env.items() if k != key}
                )
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn(key, res.stderr)


class FrontDoorRunbookTests(SimpleTestCase):
    """SPEC-22-06: the WAF/rotation story is a documented operator contract.

    The two halves of R-22.16 (WAF) and R-22.8 (secret rotation) are
    documentation and configuration, so the risk is not a broken test but a
    story that is absent, contradicted, or quietly wrong in a way that hurts an
    operator mid-incident. These pins check the properties that make it
    usable: the WAF section complements the app-layer throttles instead of
    duplicating them (a proxy rate limit is per-IP and must stay looser than
    the strictest DRF scope), the rotation story names every secret the
    deployment actually holds and says how to verify it, and neither section
    carries a credential or a vendor token.
    """

    # Every secret the deployment really holds. A rotation table missing one of
    # these is the failure this class exists to prevent.
    ROTATED_SECRETS = (
        "DJANGO_SECRET_KEY",
        "RAZORPAY_KEY_SECRET",
        "POSTGRES_PASSWORD",
    )

    # Values that would mean a credential or a vendor account leaked into the
    # runbook. A DSN/API token there is the classic accident.
    CREDENTIAL_SHAPES = (
        re.compile(r"https://[A-Za-z0-9]{16,}@"),
        re.compile(r"(?i)\b(?:api[_-]?key|token|password)\s*[:=]\s*\S{12,}"),
        re.compile(r"\brzp_(?:live|test)_[A-Za-z0-9]{12,}\b"),
    )

    def setUp(self):
        self.text = read(RUNBOOK)

    def section(self, heading):
        """Return the body of a `## ...` section, up to the next `##`."""
        match = re.search(rf"^## {re.escape(heading)}$", self.text, re.M)
        assert match, f"the runbook has no '{heading}' section"
        rest = self.text[match.end() :]
        end = re.search(r"^## ", rest, re.M)
        return rest[: end.start()] if end else rest

    def test_the_waf_section_exists_and_does_not_duplicate_the_throttles(self):
        # The failure this prevents: an operator copies a per-IP limit equal to
        # THROTTLE_COUPON_RATE and locks out a whole NAT range, or assumes the
        # WAF replaced the app-layer budgets.
        waf = self.section("WAF ruleset for the front door (SPEC-22-06, R-22.16)")
        self.assertIn("ScopedRateThrottle", waf)
        # ...and names the actual knob, so the advice is actionable.
        self.assertIn("limit_req_zone", waf)
        # The per-IP/per-identity distinction is the reason the two layers
        # coexist; stated without it, a reader cannot tell why not to tighten.
        self.assertIn("per-IP", waf)
        self.assertIn("THROTTLE_RECOVERY_RATE", waf)
        # Authorization stays in Django - a WAF is not an access-control layer.
        self.assertIn("authorization", waf.lower())

    def test_the_waf_section_still_serves_media_and_health(self):
        # Two rules that would take the storefront or the monitoring down if
        # followed blindly: caching /health/ hides a degraded store from both
        # the container healthcheck and the uptime monitor, and a body limit
        # below the app's own upload ceiling rejects images the app accepts.
        waf = self.section("WAF ruleset for the front door (SPEC-22-06, R-22.16)")
        self.assertIn("/health/", waf)
        self.assertIn("MAX_UPLOAD_MB", waf)

    def test_the_rotation_section_names_every_secret_the_deployment_holds(self):
        rotation = self.section("Secret rotation (SPEC-22-06, R-22.8)")
        for secret in self.ROTATED_SECRETS:
            with self.subTest(secret=secret):
                self.assertIn(secret, rotation)

    def test_the_rotation_procedure_is_verifiable_and_ordered(self):
        # Mint-then-retire is what makes it a rotation rather than an outage,
        # and "the old credential no longer authenticates" is the only check
        # that proves the new one is in use.
        rotation = self.section("Secret rotation (SPEC-22-06, R-22.8)")
        self.assertIn("before retiring the old one", rotation)
        self.assertIn("no longer authenticates", rotation)
        self.assertIn("/health/", rotation)

    def test_the_secret_key_caveat_states_the_downtime_cost_honestly(self):
        # Rotating SECRET_KEY is a mass logout here, because it signs the JWTs
        # and the guest-cart sessions. A runbook that calls it transparent
        # would be sending an operator into it unprepared.
        rotation = self.section("Secret rotation (SPEC-22-06, R-22.8)")
        self.assertIn("mass logout", rotation)
        self.assertIn("SECRET_KEY", rotation)

    def test_a_committed_secret_is_treated_as_compromised(self):
        # Rotation before history cleanup: rewriting a pushed commit un-leaks
        # nothing, and the ordering is the whole point.
        rotation = self.section("Secret rotation (SPEC-22-06, R-22.8)")
        self.assertIn("rotate it", rotation)
        self.assertIn("commit", rotation.lower())

    def test_neither_section_carries_a_credential_or_a_vendor_token(self):
        for heading in (
            "WAF ruleset for the front door (SPEC-22-06, R-22.16)",
            "Secret rotation (SPEC-22-06, R-22.8)",
        ):
            body = self.section(heading)
            for pattern in self.CREDENTIAL_SHAPES:
                with self.subTest(heading=heading, pattern=pattern.pattern):
                    self.assertIsNone(
                        pattern.search(body),
                        f"a credential-shaped value is documented in {heading}",
                    )


class TlsHardeningDeployContractTests(SimpleTestCase):
    """SPEC-22-08 [R-22.6] (V-06): the hardening is turned ON, not merely
    reachable.

    settings.py has shipped the SECURE_SSL_REDIRECT / HSTS / trusted-proxy-header
    flags since SPEC-17-07, every one of them env-gated with a
    development-safe default. Until this contract existed nothing in the deploy
    path set any of them, so "HTTPS is enabled" was true of no real deployment.
    These pins assert the compose env really carries the hardened posture, and -
    just as importantly - that it is a pass-through so a plain-HTTP local run
    can turn it off, and that the DEV/DEBUG path is not forced into a redirect
    loop.
    """

    # The production posture, read out of the committed compose file rather
    # than restated, so a pin cannot agree with a stale expectation.
    HARDENING_KEYS = (
        "SECURE_SSL_REDIRECT",
        "SECURE_HSTS_SECONDS",
        "SECURE_HSTS_INCLUDE_SUBDOMAINS",
        "SECURE_HSTS_PRELOAD",
        "SECURE_PROXY_SSL_HEADER_NAME",
        "SECURE_PROXY_SSL_HEADER_VALUE",
    )

    def setUp(self):
        self.environment = compose_backend_environment()

    def compose_default(self, key):
        """Return the default compose supplies for a key, with its `${..:-..}`.

        Raises for a missing key or a `:?` (required) form - both of which
        would mean the key is not deployment-tunable the way the others are.
        """
        # Compose writes the interpolation in quotes when the value could be
        # read as YAML of another type (a bare `true` is a boolean), so the
        # quotes are part of the spelling, not of the value.
        value = self.environment[key].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        match = re.match(r"^\$\{" + key + r":-([^}]*)\}$", value)
        assert match, f"{key} is not a defaulted pass-through: {value!r}"
        return match.group(1)

    def test_every_hardening_key_is_in_the_deployed_environment(self):
        for key in self.HARDENING_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, self.environment)

    def test_https_redirect_is_on_and_hsts_has_a_real_window(self):
        # SECURE_SSL_REDIRECT=true and a non-zero HSTS window are what "HTTPS
        # enabled" means to a browser; a 0 here would ship the gap unchanged.
        self.assertEqual(self.compose_default("SECURE_SSL_REDIRECT"), "true")
        hsts = self.compose_default("SECURE_HSTS_SECONDS")
        self.assertTrue(hsts.isdigit() and int(hsts) > 0, hsts)

    def test_the_trusted_proxy_header_is_the_wsgi_environ_name(self):
        # Django reads request.META[SECURE_PROXY_SSL_HEADER_NAME], and gunicorn
        # maps the wire header X-Forwarded-Proto to HTTP_X_FORWARDED_PROTO. The
        # bare header name would never match, so every proxied request would
        # look like plain HTTP and be redirected to HTTPS forever - a redirect
        # loop, which is worse than no hardening at all.
        self.assertEqual(
            self.compose_default("SECURE_PROXY_SSL_HEADER_NAME"),
            "HTTP_X_FORWARDED_PROTO",
        )
        self.assertEqual(self.compose_default("SECURE_PROXY_SSL_HEADER_VALUE"), "https")

    def test_subdomain_and_preload_hsts_are_not_enabled_by_default(self):
        # includeSubDomains breaks any plain-HTTP subdomain and the preload
        # list is effectively irreversible: both are an operator's deliberate
        # decision once the whole host tree is HTTPS, never a default.
        self.assertEqual(
            self.compose_default("SECURE_HSTS_INCLUDE_SUBDOMAINS"), "false"
        )
        self.assertEqual(self.compose_default("SECURE_HSTS_PRELOAD"), "false")

    def test_each_key_is_a_pass_through_so_a_plain_http_run_can_opt_out(self):
        # Every one is `${KEY:-default}`, never a literal: a repo-root .env can
        # override any of them, which is how `docker compose up` over http://
        # localhost avoids the redirect and the HSTS window.
        for key in self.HARDENING_KEYS:
            with self.subTest(key=key):
                self.assertIn("${" + key + ":-", self.environment[key])

    def test_the_compose_env_boots_the_app_into_the_hardened_posture(self):
        # Proved by booting, with the values compose actually declares: the
        # settings the container runs with are the ones the pins above claim.
        defaults = {key: self.compose_default(key) for key in self.HARDENING_KEYS}
        snippet = (
            "import config.settings as s;"
            "print('REDIRECT', s.SECURE_SSL_REDIRECT);"
            "print('HSTS', s.SECURE_HSTS_SECONDS);"
            "print('SUBDOMAINS', s.SECURE_HSTS_INCLUDE_SUBDOMAINS);"
            "print('PRELOAD', s.SECURE_HSTS_PRELOAD);"
            "print('PROXY_HEADER', s.SECURE_PROXY_SSL_HEADER)"
        )
        res = run_settings_import(
            {
                "DJANGO_SECRET_KEY": "x" * 50,
                "DJANGO_ENV": "production",
                "DATABASE_URL": "postgres://u:p@db.example.com:5432/perfume_store",
                "DJANGO_ALLOWED_HOSTS": "shop.example.test",
                "CSRF_TRUSTED_ORIGINS": "https://shop.example.test",
                **defaults,
            },
            snippet=snippet,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("REDIRECT True", res.stdout)
        self.assertIn(f"HSTS {self.compose_default('SECURE_HSTS_SECONDS')}", res.stdout)
        self.assertIn("SUBDOMAINS False", res.stdout)
        self.assertIn("PRELOAD False", res.stdout)
        self.assertIn("PROXY_HEADER ('HTTP_X_FORWARDED_PROTO', 'https')", res.stdout)

    def test_the_development_path_is_not_hardened(self):
        # The other half, and the reason the flags can be on in production at
        # all: DJANGO_DEBUG=true with no hardening keys must keep the
        # development-safe defaults, or a developer would be fighting a
        # redirect loop and an HSTS lockout on localhost.
        snippet = (
            "import config.settings as s;"
            "print('DEBUG', s.DEBUG);"
            "print('REDIRECT', s.SECURE_SSL_REDIRECT);"
            "print('HSTS', s.SECURE_HSTS_SECONDS);"
            "print('PROXY_HEADER', s.SECURE_PROXY_SSL_HEADER)"
        )
        res = run_settings_import({"DJANGO_DEBUG": "true"}, snippet=snippet)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("DEBUG True", res.stdout)
        self.assertIn("REDIRECT False", res.stdout)
        self.assertIn("HSTS 0", res.stdout)
        self.assertIn("PROXY_HEADER None", res.stdout)

    def test_settings_defaults_are_untouched_by_the_deploy_layer(self):
        # This task enables the hardening at the DEPLOY layer only. If someone
        # ever "fixes" it by changing a default in settings.py, local
        # development and the test suite break - so the defaults are pinned.
        res = run_settings_import(
            {"DJANGO_DEBUG": "true"},
            snippet=(
                "import config.settings as s;"
                "print('REDIRECT', s.SECURE_SSL_REDIRECT);"
                "print('HSTS', s.SECURE_HSTS_SECONDS);"
                "print('SUBDOMAINS', s.SECURE_HSTS_INCLUDE_SUBDOMAINS);"
                "print('PRELOAD', s.SECURE_HSTS_PRELOAD);"
                "print('PROXY_HEADER', s.SECURE_PROXY_SSL_HEADER)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        for line in ("REDIRECT False", "HSTS 0", "PROXY_HEADER None"):
            with self.subTest(line=line):
                self.assertIn(line, res.stdout)

    def test_the_compose_healthcheck_identifies_itself_as_a_deployment_would(self):
        # The compose probe must carry the same Host and forwarded scheme the
        # image probe does, or the hardening this file turns on would make the
        # container report itself unhealthy forever (proved by the boot test
        # below, which shows a naive probe getting 301).
        compose = "\n".join(code_lines(read(COMPOSE)))
        self.assertIn("DJANGO_ALLOWED_HOSTS", compose)
        self.assertIn("X-Forwarded-Proto", compose)

    def test_the_env_example_documents_every_key_the_deploy_layer_sets(self):
        # An operator reading the env template has to find the same keys the
        # deploy artifact sets; a key the artifact sets but the template omits
        # is a support ticket waiting to happen.
        example = read("backend/.env.example")
        for key in self.HARDENING_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, example)

    def test_the_healthcheck_still_passes_under_the_hardening_it_enables(self):
        # SECURE_SSL_REDIRECT plus the SPEC-22-03 host guard are exactly what a
        # naive loopback probe would trip over: a bare http request is 301'd to
        # an https:// the container does not serve, and an unlisted Host is a
        # 400. So the probe must present the deployment's own host and forwarded
        # scheme - proved here by booting the app and probing /health/ the way
        # the healthcheck does.
        with tempfile.TemporaryDirectory() as workdir:
            env = {
                "DJANGO_SECRET_KEY": "x" * 50,
                "DJANGO_ENV": "production",
                "DJANGO_ALLOWED_HOSTS": "shop.example.test",
                "CSRF_TRUSTED_ORIGINS": "https://shop.example.test",
                "DATABASE_URL": "sqlite:///"
                + (Path(workdir) / "hardened.sqlite3").as_posix(),
                "DJANGO_MEDIA_ROOT": workdir,
                "DJANGO_STATIC_ROOT": workdir,
            }
            for key in self.HARDENING_KEYS:
                env[key] = self.compose_default(key)
            migrated = manage("migrate", "--noinput", env=env)
            self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
            # config.wsgi is what the image CMD hands gunicorn, and importing
            # it is what configures Django in this child process.
            snippet = (
                "import config.wsgi;"
                "from django.test import Client;"
                "probe = {'host': 'shop.example.test', "
                "'x-forwarded-proto': 'https'};"
                "print('PROBE', Client(headers=probe).get('/health/').status_code);"
                "print('NAIVE', Client(headers={'host': 'shop.example.test'})"
                ".get('/health/').status_code)"
            )
            res = run_backend([sys.executable, "-c", snippet], env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        # The probe the healthcheck performs gets the real answer...
        self.assertIn("PROBE 200", res.stdout)
        # ...while a probe without it would 301, which is why the healthcheck
        # cannot simply be "does the port answer".
        self.assertIn("NAIVE 301", res.stdout)


class ComposeRuntimeEnvContractTests(SimpleTestCase):
    """Gate fix cycle 1 (SPEC-2-04/22-03): the runtime service has to boot.

    The compose env contract listed the secret key, hosts, DEBUG and
    DATABASE_URL but never DJANGO_ENV or CSRF_TRUSTED_ORIGINS, so with exactly
    the env this file assembles the gunicorn CMD, release.sh's migration
    checkpoint and /health/ all refused at import - a crash-loop no release
    could get past, and the strongest gate in the project.
    """

    def setUp(self):
        self.environment = compose_backend_environment()

    def test_backend_service_declares_the_non_debug_guard_keys(self):
        # DJANGO_SECRET_KEY is deliberately absent (it flows in through
        # env_file, and an earlier test pins that it is never set here): these
        # are the keys whose absence made the container refuse to start.
        for key in ("DJANGO_ENV", "CSRF_TRUSTED_ORIGINS", "DATABASE_URL"):
            with self.subTest(key=key):
                self.assertIn(key, self.environment)

    def test_environment_name_and_origins_come_from_the_deployment_env(self):
        # Pass-throughs, not literals: the .env the deploy host holds is the
        # only place that knows which environment it is and which origin it
        # serves. The default names a real environment (compose IS the
        # production orchestrator); the origins are REQUIRED, because a
        # localhost default here is precisely the unconfigured deployment the
        # settings guard exists to refuse - compose says which key is missing
        # instead of letting the container crash-loop on the import.
        for key in ("DJANGO_ENV", "CSRF_TRUSTED_ORIGINS"):
            with self.subTest(key=key):
                self.assertIn("${" + key, self.environment[key])
        self.assertRegex(
            self.environment["DJANGO_ENV"],
            r"\$\{DJANGO_ENV:-(staging|production)\}",
        )
        origins = self.environment["CSRF_TRUSTED_ORIGINS"]
        self.assertIn(":?set CSRF_TRUSTED_ORIGINS", origins)

    def test_the_compose_env_contract_boots_the_app_and_serves_health(self):
        # The proof BUG-2 needed, with the env the compose contract assembles
        # from a repo-root .env: the WSGI application the image CMD hands
        # gunicorn imports, Django's checks, and /health/ answering 200 - all
        # through the settings import, with no server and no network.
        #
        # DATABASE_URL is the one substitution: the contract's Postgres runs in
        # the `db` service and cannot be reached from a test process, so the
        # contract's own documented alternative - an explicit sqlite:// URL,
        # honoured in every environment - stands in for it. MEDIA_ROOT is
        # redirected to a temp dir so /health/'s write probe never touches the
        # checkout.
        with tempfile.TemporaryDirectory() as workdir:
            env = {
                "DJANGO_SECRET_KEY": "x" * 50,
                "DJANGO_DEBUG": "false",
                "DJANGO_ENV": "production",
                "DJANGO_ALLOWED_HOSTS": "shop.example.test",
                "CSRF_TRUSTED_ORIGINS": "https://shop.example.test",
                "DATABASE_URL": "sqlite:///"
                + (Path(workdir) / "runtime.sqlite3").as_posix(),
                "DJANGO_MEDIA_ROOT": workdir,
                "DJANGO_STATIC_ROOT": workdir,
            }
            migrated = manage("migrate", "--noinput", env=env)
            self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
            res = run_backend([sys.executable, "-c", RUNTIME_BOOT_SNIPPET], env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        # What the container CMD loads, and what the compose healthcheck probes.
        self.assertIn("WSGI WSGIHandler", res.stdout)
        self.assertIn("HEALTH 200", res.stdout)

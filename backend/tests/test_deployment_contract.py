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
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = "docker-compose.yml"
PROCFILE = "Procfile"
RELEASE = "scripts/release.sh"
DEPLOY_WORKFLOW = ".github/workflows/deploy.yml"
BACKEND_TESTS_WORKFLOW = ".github/workflows/backend-tests.yml"

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

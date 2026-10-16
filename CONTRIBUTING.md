# Contributing

This describes the flow this repository actually runs, not generic advice.
Read `backend/docs/conventions.md` **before** any change — it is the single
source of truth for the coding rules (DRF class-based views, `permission_classes`
never inline staff checks, Decimal-only money, `transaction.atomic()` +
`select_for_update()` on side-effectful flows, IntegrityError-retry for unique
generation, explicit serializer `fields`, `validate_password` everywhere,
throttled public endpoints, Black 88 columns, conventional commit prefixes).

## Branch model: two permanent lanes, one integration branch

- **`lane/a`** and **`lane/b`** are the two permanent lane branches. Lane A
  is checked out in the `spec-comp-wt` worktree, lane B in the `wt-async`
  worktree. The lanes are generic and permanent: **never cut a new branch per
  task** — each task commits onto its lane branch.
- Every task ships **builder → auditor → gate**: the builder implements and
  proves it, the auditor re-verifies against the spec and pushes the audited
  tip, the gate is CI. No builder pushes; the auditor's push duty is what
  advances the lane branch and the integration PR head.
- **`spec-comp` is the integration branch.** The release-engineer reconciles
  the lanes' work into it at section boundaries, and pushes to `spec-comp`
  run the full backend gates.
- The **promotion pull request is `spec-comp` → `feat/add-frontend`**, opened
  and merged **only by the release-engineer, and only on an all-green gate**
  — backend suite and coverage, lint, doc gates, and secret scan all green on
  the PR head. Nobody else opens or merges it. Merges landing on
  `feat/add-frontend` are what trigger the storefront deploy.
- **`master` is legacy and frozen.** Never target it, merge into it, or
  deploy from it: the deploy workflow's default release branches are
  `spec-comp` and `feat/add-frontend`, with `master` deliberately absent.

## Test commands

Backend, from `backend/` (PostgreSQL is the engine CI certifies; with
`DATABASE_URL` unset, settings falls back to SQLite for a quick smoke run):

```powershell
venv\Scripts\python -m coverage run manage.py test
venv\Scripts\python -m coverage report            # gate: fail_under = 90 (backend/.coveragerc)
venv\Scripts\python manage.py makemigrations --check --dry-run   # zero migration drift
```

Frontend, from `frontend/`:

```powershell
npm run test          # vitest unit tests
npm run test:e2e      # playwright e2e (CI self-skips it until it is hermetic)
npm run build         # next build
```

Documentation gates, from the repo root — CI runs both on every backend
change, so run them before you push (both take a base SHA: the branch point
your change is measured against):

```powershell
backend\venv\Scripts\python.exe scripts\doc_claims.py --base <base-sha>
backend\venv\Scripts\python.exe scripts\changelog_figures.py check --base <base-sha>
```

## What CI checks

| Check | What it runs |
|---|---|
| `backend-tests` | Django system check, migration-drift check, PostgreSQL suite behind the coverage gate, the certified failure-set gate, the doc-claims and changelog-figure gates, the scripts' own tests — then lint: ruff check, ruff format, pre-commit, mypy |
| `secret-scan` | gitleaks on pushes and pull requests |
| `ghcr-publish` | runs on pushes **and** pull requests to `spec-comp`/`feat/add-frontend` when they touch `backend/**` (which includes `backend/docs/**`, so a backend docs-only change triggers it too), `docker-compose.yml`, `Procfile`, `scripts/**`, or the workflow file itself; every trigger builds the backend image, but the image is published only from push events (`push: ${{ github.event_name != 'pull_request' }}` — a pull request builds without publishing), and a change matching none of those paths (e.g. a frontend-only change or an edit to a root-level doc like this one) skips the run |
| `frontend-tests` | vitest and the production build; the playwright e2e job self-skips unless explicitly armed |

Deploys are separate: gated on green `backend-tests` and green `secret-scan`
on the same commit, opt-in via a repository variable.

## Changelog

Append exactly one row per task to the agent-run table in
`backend/docs/changes.md` — append-only, never rewrite rows above yours.
Figures in that row are **printed, not typed**: run
`python scripts/changelog_figures.py floor --task <id>` (it executes the
suite with coverage and saves the artifact the changelog gate requires).

## Commits

- Conventional prefixes: `feat:`, `fix:`, `docs:`, `chore:`, `test:`,
  `refactor:` — one task, one commit (fix cycles add further commits).
- Never commit `.env`, `db.sqlite3`, `media/`, or `venv/` (all git-ignored;
  CI also scans for secrets).

## Licensing

This guide is license-independent by the owner's decision: it grants no
license and requires none to follow. The repository's licensing question
remains open and owner-owned, recorded in `backend/docs/decisions.md`; no
contributor agreement stands between you and a pull request.

# Perfume Store

A monorepo with two deployables that ship independently through their own CI
paths: a Django/DRF backend API and a Next.js storefront. Everything is
configured through the environment — the `.env.example` files document every
key; no secret, URL or mode flag lives in code.

- **`backend/`** — Django 6.1 / Django REST Framework e-commerce API:
  JWT accounts, session cart, coupons, Razorpay payments, Celery async
  sweeps, ops `/health/`. Start at `backend/README.md` (API map, layout) and
  `backend/docs/architecture.md` (how the pieces fit together).
- **`frontend/`** — Next.js 16 storefront deployed to Cloudflare. Start at
  `frontend/PLAN.md` (route map, API surface, money rules).
- **`backend/docs/`** — the documentation ledger set (see
  [Documentation gates & ledgers](#documentation-gates--ledgers)).
- **`BACKEND_REQUESTS.md`** — cross-repo gaps the frontend team logged.

## Stack

| Piece | Choice | Where it is pinned |
|---|---|---|
| API server | Gunicorn serving Django 6.1 + DRF | `backend/Dockerfile` CMD, `Procfile` |
| Database | PostgreSQL 17 — the authoritative engine (SQLite was retired from CI) | `docker-compose.yml`, `.github/workflows/backend-tests.yml` |
| Async | Celery 5.6.3 worker + beat on Redis; client pinned redis 8.1.0 | `backend/requirements.lock`, `backend/config/celery.py` |
| Cache/throttle | LocMem by default; Redis when `REDIS_URL` is declared (one knob: broker, result backend, DRF throttle cache) | `backend/.env.example`, `backend/config/settings.py` |
| Static files | whitenoise, collected into the image at build time | `backend/Dockerfile` |
| Storefront | Next.js 16 + OpenNext → Cloudflare Worker `perfume-storefront` (`test.boredompeaks.me`) | `frontend/wrangler.jsonc` |

## Local development

### Backend in a venv

```powershell
# from backend/
python -m venv venv
venv\Scripts\pip install -r requirements.lock   # pinned + hash-checked; CI installs the same file
copy .env.example .env                          # app keys; settings loads .env through dotenv
# PostgreSQL is authoritative. Point DATABASE_URL at a reachable PG 17 -
# for a host run that is your own container/instance with a published port
# (the compose `db` service is reachable only on the compose network).
# Unset, settings falls back to SQLite for a quick smoke run; CI certifies
# PostgreSQL only and asserts the engine before it runs anything.
venv\Scripts\python manage.py migrate
venv\Scripts\python manage.py runserver
```

### Whole stack with compose

```powershell
# from the repo root: create a root .env from the ENV CONTRACT comment at the
# top of docker-compose.yml (placeholders only - it is git-ignored, never
# commit it; scripts/release.sh refuses to run without it)
docker compose up -d --build
curl http://localhost:8000/health/
docker compose ps
```

### Storefront

```powershell
# from frontend/
npm install
npm run dev
```

## Tests

```powershell
# Backend, from backend/ - PostgreSQL engine via DATABASE_URL, then coverage:
venv\Scripts\python -m coverage run manage.py test
venv\Scripts\python -m coverage report           # gate: fail_under = 90 (backend/.coveragerc)
venv\Scripts\python manage.py makemigrations --check --dry-run   # zero migration drift
```

CI runs exactly these on PostgreSQL 17 and judges failures as a set against
`scripts/pg_baseline.json`, alongside ruff, pre-commit, mypy and the doc
gates (`.github/workflows/backend-tests.yml`). The store pair runs in
`frontend/`:

```powershell
npm run test          # vitest unit tests
npm run test:e2e      # playwright e2e
npm run build         # next build
```

## Celery: worker, beat, Redis

The background sweeps — reservation expiry, notification-outbox drain and
purge — are Celery tasks scheduled by beat (`CELERY_BEAT_*` cadences in
`backend/.env.example`; the tasks in `backend/common/tasks.py` call the same
management commands a manual run uses). Compose ships the whole substrate:

| service | image | command |
|---|---|---|
| `redis`  | `redis:8-alpine` | image default (broker; throttle cache when `REDIS_URL` is declared) |
| `worker` | `perfume-backend:latest` | `celery -A config worker -l info` |
| `beat`   | `perfume-backend:latest` | `celery -A config beat -l info -s /tmp/celerybeat-schedule` |

`docker compose up -d --build` brings all of it up together; without beat
nothing ever schedules the sweeps, which is why beat is a service and not an
afterthought.

## Deploys

**Backend** — `.github/workflows/deploy.yml`, gated on a green `backend-tests`
run and a green `secret-scan` on the same commit, opt-in via the
`DEPLOY_ENABLED` repository variable. One ssh call executes the repo's release
script on the deploy host: `scripts/release-uat.sh` (the UAT flow over the
`perfume-net` network; tracked executable, selected by deploy.yml's `-x`
test) when present, `scripts/release.sh` otherwise. `scripts/release.sh` is
also the manual/local release path, and its sequence is: build the image →
start db and redis (health-gated) → migration review (fail-closed
`makemigrations --check`) → migrate → collectstatic → recreate the backend
and wait for `/health/` → recreate worker and beat on the released image.

**Storefront** — `.github/workflows/frontend-deploy.yml` on pushes to
`feat/add-frontend` that touch `frontend/**`: OpenNext build, then
`wrangler deploy` to the Cloudflare worker. It is not part of
`docker-compose.yml`; the backend compose stack never serves the storefront.

## Documentation gates & ledgers

- `python scripts/doc_claims.py` — scans the documentation a change touched
  (scope: `backend/docs/`) and fails on mechanically decidable claims that
  do not resolve: a quoted test name with no definition, a cited module that
  does not exist, a line reference past end of file. Undecidable prose is
  reported, never guessed at.
- `python scripts/changelog_figures.py check --base <sha>` — refuses any
  changelog floor figure that has no measured artifact behind it
  (`scripts/figures/`); the printer side is the `floor` subcommand, which
  runs the suite and prints the bullet instead of letting a number be typed.

Both run in CI (`backend-tests.yml`) and nightly over the whole corpus
(`doc-claims-nightly.yml`). The ledgers live in `backend/docs/`:
`spec-run-state.md` (status and certified-floor index), `run/` (per-section
evidence files), `changes.md` (append-only changelog, one row per task) and
`conventions.md` (the coding rules every change is held to).

## Branch model

- `lane/a`, `lane/b` — builder lanes; each task commits on a lane branch and
  is merged into integration.
- `spec-comp` — integration branch; pushes to it run the full backend gates.
- `feat/add-frontend` — promotion target: PRs from `spec-comp` land here, and
  pushes here are what trigger the storefront deploy.
- `master` — frozen legacy: `deploy.yml` never treats it as a release branch
  (the default release branches are `spec-comp` and `feat/add-frontend`) and
  promotions do not target it.

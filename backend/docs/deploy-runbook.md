# Deploy runbook

Operator-facing notes for the SPEC-2-04 / SPEC-22-03 deployment configuration.
Every value named here is an env key; the full contract is `backend/.env.example`
and the repo-root `.env` (git-ignored, never committed) that `docker compose` and
`scripts/release.sh` read.

## Serving media (SPEC-2-04, closes V-13)

Uploaded files - product images above all - are **media**, not static:

- `DJANGO_MEDIA_ROOT` is where uploads are written. Unset locally (they land in
  `backend/media`); in a container it must be the mounted volume path
  (`/app/media`, which `docker-compose.yml` already mounts from `media_data`),
  because anything on the container's own filesystem is lost when the container
  is replaced.
- `DJANGO_MEDIA_BACKEND` selects the storage backend (`settings.STORAGES`).
  Default `django.core.files.storage.FileSystemStorage`, rooted at
  `DJANGO_MEDIA_ROOT`. Any installable dotted path works if uploads should move
  to an object store; a value that is not a dotted path is refused at boot.
- `DJANGO_STATIC_ROOT` is separate and unchanged: `collectstatic` gathers
  assets there and whitenoise serves them from the app process.

**Front door (production).** nginx - or whatever terminates in front of
gunicorn - should serve `/media/` straight off the same mounted volume rather
than proxying image bytes through Python:

```nginx
location /media/ {
    alias /app/media/;
    expires 7d;
    add_header Cache-Control "public";
}
```

The Django side is not a fallback that can be switched on: `config/urls.py`
routes `/media/` through `django.views.static.serve` on **every** DEBUG value,
because Django's `static()` helper emits no route at all when `DEBUG=False` and
that silent no-op is exactly how product images started 404ing in production.
With the route in place a missing front-door rule degrades to a file served by
the app, never to a 404. The app process must therefore have read access to
`DJANGO_MEDIA_ROOT` (`/health/` already reports degraded when it is not
writable).

## Environments and the database (SPEC-22-03, closes R-22.3)

`DJANGO_ENV` names the deployment: `local`, `ci`, `staging` or `production`.
It is **required** whenever `DJANGO_DEBUG=false`, and an unknown value is
refused at boot instead of being guessed at.

A non-debug boot must be complete, or the app refuses to start by name:

| Key | Non-debug requirement |
| --- | --- |
| `DJANGO_SECRET_KEY` | required (pre-existing V-02 guard) |
| `DJANGO_ENV` | required - `staging` or `production` |
| `DATABASE_URL` | required and parseable - missing, malformed or unsupported **refuses to boot** instead of silently using `backend/db.sqlite3` |
| `DJANGO_ALLOWED_HOSTS` | required, explicit (no localhost default) |
| `CSRF_TRUSTED_ORIGINS` | required, explicit (no localhost default) |

The refusal names the problem and never echoes the URL, which carries a
password. An **explicit** `sqlite:///` URL is still honoured in any
environment: a single-box staging install is a deliberate choice, not a
silent substitution.

`DJANGO_DEBUG=true` keeps the developer conveniences: the sqlite fallback,
the localhost host/origin defaults, and an undeclared `DJANGO_ENV` (`local`).
CI runs the suite with `DJANGO_DEBUG=true`
(`.github/workflows/backend-tests.yml`), which is why the fail-closed branches
never fire there.

**Staging.** A staging deployment is an ordinary deployment of this same
image with its own env file: `DJANGO_ENV=staging`, its own
`DJANGO_SECRET_KEY`, its own `DJANGO_ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS`,
and its own Postgres. It gets exactly the production treatment - there is no
debug-shaped escape - so nothing in it can silently fall back to developer
data.

**Data / credential isolation (R-22.3).** One database and one secret key per
environment. Nothing in this app shares a database across environments, so a
staging run can neither read nor migrate production rows, and a value signed
with staging's key (session, signed cookie) is not honoured in production.
The staging database is disposable: it is restored from a sanitised dump or
seeded fresh, never pointed at production for convenience.

**Where each key is set.** The table above is the whole contract, and every one
of those keys has to be present in *both* places a non-debug boot happens -
the guards are conjunctive, so the one key you are missing is the one that
refuses the boot, whatever the others say:

- **The image's build layer** (`backend/Dockerfile`, the `collectstatic`
  `RUN`). A build layer is a `DJANGO_DEBUG=false` boot too, so it declares the
  same keys, as build-time-only values: `DJANGO_ENV=ci` (a build is not a
  deployment), the non-secret placeholder secret key, a throwaway
  `DATABASE_URL=sqlite:////tmp/build.sqlite3`, and the localhost hosts and
  origins. Nothing there is a credential or a real database - `collectstatic`
  opens no connection, but the guard cannot know that, and the URL only has to
  be parseable. `backend/.dockerignore` keeps `.env` and `db.sqlite3` out of
  the build context, so this temp path is the only database the image names.
- **The running container** (`docker-compose.yml` + the repo-root `.env`).
  `DJANGO_ENV` and `CSRF_TRUSTED_ORIGINS` are pass-throughs, because only the
  deployment knows which environment it is and which origin it serves.
  `DJANGO_ENV` defaults to `production` - this file is the production
  orchestrator - and set it to `local` for local work. `CSRF_TRUSTED_ORIGINS`
  is **required**: compose refuses to start and names the missing key, instead
  of booting a container that dies on the settings import. The secret key,
  hosts, `DJANGO_DEBUG` and the app-side keys flow in through `env_file`.

Both halves are pinned by the suite rather than only documented:
`backend/tests/test_deployment_contract.py` boots the app with exactly the
build env the committed `RUN` line declares, and with the shape the compose
contract assembles (gunicorn's `config.wsgi:application` loads and `/health/`
answers 200 in-process), and it asserts that dropping **any one** of the five
keys is still refused by name. So neither file can drift back into an
unbuildable image or a crash-looping container without a red test, and the
production guard cannot be quietly weakened from the deployment side.

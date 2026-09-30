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

**Known gap (follow-up for the Dockerfile owner, SPEC-2-10a).** The image's
build-time `collectstatic` runs with `DJANGO_DEBUG=false` and no
`DATABASE_URL`, so the SPEC-22-03 guard makes `docker build` refuse until
that `RUN` gains an explicit build-time `DATABASE_URL` (e.g.
`sqlite:////tmp/build.sqlite3`). The guard is the correct behaviour and the
build line is what has to change; `backend/Dockerfile` was out of scope for
this task.
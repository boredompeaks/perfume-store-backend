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

## Backups (SPEC-22-02, closes R-22.9)

`manage.py backup_db` takes the backup and applies the retention policy in the
same run. `manage.py restore_drill` only *rehearses* a restore; the dump it
rehearses is this command's output, and before SPEC-22-02 nothing in this
repository produced one.

```bash
# inside the app environment (never a bare `docker compose run` - see below)
venv\Scripts\python manage.py backup_db --dry-run   # report the plan, write nothing
venv\Scripts\python manage.py backup_db             # dump, then prune to BACKUP_RETENTION
venv\Scripts\python manage.py backup_db --prune-only --keep 14
```

What it does, per engine:

| Engine | Mechanism | Why |
|---|---|---|
| PostgreSQL (production) | `pg_dump --format=custom --no-owner --no-privileges --no-password`, stdout written straight to the dump file | the dump never passes through the app process's memory; `--no-password` makes a wrong password a fast failure instead of a prompt that hangs the scheduler |
| sqlite (local/dev) | the stdlib **online backup** API | consistent page-by-page copy even while the app is writing; a file copy of a live sqlite database is not |

The connection identity (host, port, user, password, database, `sslmode`)
travels to `pg_dump` in the child's environment as the libpq `PG*` variables and
**never in argv**, which is world-readable through `ps`. `sslmode` comes from
the URL's query params via `OPTIONS`, so a managed database is dumped over the
same TLS the app is served with. **No output ever contains `DATABASE_URL` or the
password**: the command names the alias, the engine and the file, and anything
`pg_dump` itself printed is redacted before it reaches the terminal, a cron mail
or a ticket. A failed dump exits non-zero, keeps no dump, and removes the
partial file - a backup that failed is loud, or the cadence silently produces
nothing.

The database is only ever **read** (the sqlite database is opened `mode=ro`;
`pg_dump` is a client-side dump), the dump always lands on a fresh filename,
and it is moved into place only after it succeeds, so a truncated dump can
never be mistaken for a good one.

### Env keys

| Key | Meaning |
|---|---|
| `BACKUP_DIR` | **required** - the directory the dumps are written to. A run without it is refused by name: a dump written into a container's writable layer is lost when the container is replaced, which is the failure a backup exists to prevent. |
| `BACKUP_RETENTION` | how many dumps to keep, newest first (default 7; a non-numeric value falls back to the default). |

`BACKUP_DIR` in `.env` names a **host** path. `scripts/backup.sh` bind-mounts it
into the one-shot container at `/backups` and overrides the key for that
container with the container-side path, so the host view and the container view
cannot drift apart. This is why the backup runs through the script: a bare
`docker compose run backend python manage.py backup_db` would take the host
path, create it inside the throwaway container, and produce a backup that
outlives nothing.

### Retention

Keeping the newest `BACKUP_RETENTION` dumps, by filename, on every run. Filenames
are `perfume-<UTC timestamp>.dump` (or `.sqlite3`); the timestamps are
fixed-width, so "newest" is a name sort rather than a filesystem-metadata sort.
Two dumps inside the same second both survive - the second is disambiguated
(`…-1.dump`) rather than overwriting the first. Pruning matches **only** this
filename scheme, so a file an operator parked in the backup directory is never
a deletion candidate, and a `BACKUP_RETENTION` below 1 is refused rather than
quietly deleting every backup the moment it finishes making one.

Retention is a **count, not an age**. With the daily cadence below and the
default of 7, that is seven daily dumps on the host. Dumps accumulate at the
database's size per day, so the backup volume needs headroom sized for
`BACKUP_RETENTION x database size`.

### Schedule

A daily `cron` entry on the deploy host, running the committed script - the
same shape as `scripts/release.sh`, so the rehearsed backup and the scheduled
one are the same command, and no credential lives in the repository or in CI:

```cron
# crontab -e on the deploy host (or: /etc/cron.d/perfume-store)
# 02:30 every day: dump, then prune to BACKUP_RETENTION.
# DEPLOY_DIR is the checkout the deploy workflow keeps (see deploy.yml).
30 2 * * * cd /srv/perfume-store && BACKUP_DIR=/srv/perfume-store/backups ./scripts/backup.sh >> /var/log/perfume-store-backup.log 2>&1
```

`BACKUP_DIR` is passed on the command line rather than read out of `.env`
because it is a host path the *script* needs before any container exists.
One-time setup on the host:

```bash
sudo install -d -o 10001 -g 10001 /srv/perfume-store/backups   # the app's uid (backend/Dockerfile)
```

The systemd-timer equivalent, if the host prefers timers, is the same script
under a `.timer` unit (`OnCalendar=daily`, `Persistent=true` so a host that was
off still runs it) with the same `Environment=BACKUP_DIR=…` and the same
non-zero-is-a-failure handling. A GitHub Actions schedule was **not** chosen:
it would have to ssh to the deploy host with the release credentials, it runs
only while the repository's Actions quota lasts, and a quota exhaustion or an
expired secret would stop backups silently - a worse failure mode than a
cron entry an operator can `crontab -l`.

### The prerequisite: the image has no `pg_dump`

`backup_db` shells out to `pg_dump`, and the backend image (SPEC-2-10a)
installs `psycopg` - the *driver* - not the *client*. Until the image carries
the PostgreSQL client the script stops with that refusal by name rather than
producing nothing quietly. Either remedy, in order of preference:

1. **Add the client to the image** (one line in `backend/Dockerfile`, next to
   `pip install`):
   `RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*`.
   Then `scripts/backup.sh` works unchanged.
2. **No image change - dump with the postgres image's own client.** The `db`
   service (`postgres:17-alpine`) already carries `pg_dump`, and inside that
   container `POSTGRES_USER`/`POSTGRES_DB` are already in the environment, so
   no credential is passed on any command line:

   ```bash
   cd /srv/perfume-store
   dump="$BACKUP_DIR/perfume-$(date -u +%Y%m%dT%H%M%SZ).dump"
   docker compose exec -T db sh -c \
     'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges --no-password' \
     > "$dump"
   # then apply the same retention policy with the same code:
   docker compose run --rm -T -v "$BACKUP_DIR:/backups" -e BACKUP_DIR=/backups \
     backend python manage.py backup_db --prune-only
   ```

   `--prune-only` is why it exists: the retention policy is the command's, so
   both routes keep exactly the newest `BACKUP_RETENTION` dumps and cannot
   disagree about the filename scheme.

### What this does and does not cover

- **Volume.** The dumps live on the deploy host's disk (or a mounted volume).
  A host loss loses them. Copy them off-host on the same cadence - object
  storage, a second region, a NAS - and treat that copy as the backup of
  record. Nothing in this repository automates the off-host copy, because a
  bucket name and credentials are deployment facts; this section does not
  claim otherwise.
- **Verification.** A backup that has never been restored is an assumption.
  `manage.py restore_drill` proves the *format* round-trips, and it is cheap -
  run it on every release and after any change to the dump command.
- **Media is not in the dump.** Uploads live on the `media_data` volume
  (`DJANGO_MEDIA_ROOT`, above). Back up that volume too, or a restore returns
  a store with every product image missing.
- **Not covered at all.** The guarantees above assume the host's own disk,
  its filesystem and its cron are healthy. None of this defends against a
  destroyed host, a wiped volume, or a credential leak.

## Rolling back a bad release (SPEC-22-02, closes R-22.23)

`scripts/release.sh` deploys a commit that CI has already gated. When the
deployed commit is the problem, the rollback is the same script pointed at an
earlier commit - the one thing that must be decided honestly is what to do
about the **database**, because the release's `migrate` already ran.

**Find the known-good commit.** The deploy workflow records every released
commit (`git log --oneline` on the deploy host's checkout; the release commit
list is the same history as `master` plus the promoted release branches).
Take the commit before the one that broke, and confirm it: `git show <sha>`,
and a green `backend-tests` run on it.

**Redeploy it.** On the deploy host:

```bash
cd /srv/perfume-store
git fetch --quiet --prune origin
git -c advice.detachedHead=false checkout --quiet --detach <known-good-sha>
./scripts/release.sh          # build -> migration-review checkpoint -> migrate -> collectstatic -> restart
```

`release.sh` is idempotent at every step, so re-running it on the older commit
is a normal release of that commit: it rebuilds the image, re-checks that no
model change is missing its migration, re-runs `migrate` (which applies
whatever migrations that older tree has that the database does not - normally
none, because migrations are applied in order), re-collects static files and
recreates the container, waiting for `/health/`.

**Then pick the data remedy.** The schema state is the decision, not the
severity of the symptom:

| Situation | Remedy | Cost |
|---|---|---|
| The release is broken in **code only** and the schema it applied is still wanted | **Forward-fix.** Roll the code back to stop the bleeding, then fix forward and release properly. This is the default. | two releases; the old code may be incompatible with the newer schema (see below) |
| The release applied a migration whose **reverse is safe** (an `AddField`/`AlterField` the old code does not need, a model the old code never reads) | **Roll the code back, then reverse the migration by hand:** `docker compose run --rm -T backend python manage.py migrate <app> <migration_before_the_bad_one>` | you must read the migration's `RunPython` and decide whether its `reverse_code` is lossless. If it has none, `migrate` raises `IrreversibleError` and nothing is reversed |
| The release **changed or destroyed data** (a data migration wrote wrong values, a bad `RunPython`, a bug deleting rows) | **Restore from a backup** (`docs/runbook-incident-recovery.md` §3a) - rolling back the code does not undo data that is already wrong | everything written since the dump is gone; stop writes first, reconcile the gap afterwards |

Three things this repository will not claim:

1. **There is no automated migration reversal.** Nothing in `release.sh`
   reverses anything, and nothing detects that a release applied a migration.
   `migrate <app> <migration>` is an explicit operator decision, and its
   safety is the migration author's `reverse_code`, not a guarantee.
2. **`migrate` backwards is frequently irreversible by design.** Django refuses
   to reverse a `RunPython` without a `reverse_code`, and reverses
   `RemoveField`/`DeleteModel` by **dropping the column or the table**. That is
   data loss, so a schema rollback is only safe when you have read the migration
   and know what its reverse does.
3. **Rolling the code back does not roll the schema back.** The database keeps
   the newer schema. That is usually fine here - extra columns and extra tables
   are ignored by older code - but a released migration that renamed a column,
   changed a type or dropped one will break the older code. In that case the
   migration must be reversed (or the forward-fix must land) before the old
   code serves traffic.

**Verify the rollback like a release**, not like an incident:
`GET /health/` answers 200, `docker compose ps` shows the app on the older
image, `docker compose logs --tail 50 backend` shows gunicorn serving (not a
migration traceback), and one real checkout path still works. If the release
also touched **data**, the rollback is not finished until
`docs/runbook-incident-recovery.md` §4 passes - money, stock and coupon
counters reconciled against the gateway.

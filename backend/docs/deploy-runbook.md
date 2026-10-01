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

## Transport hardening in the deploy artifact (SPEC-22-08, closes V-06)

SPEC-17-07 shipped the flags; SPEC-22-08 turns them on. `docker-compose.yml`
sets them, so a compose deployment is hardened with nothing to remember:

| Key | compose default | Effect |
| --- | --- | --- |
| `SECURE_SSL_REDIRECT` | `true` | plain-HTTP requests get a 301 to HTTPS |
| `SECURE_HSTS_SECONDS` | `31536000` | browsers refuse plain HTTP for a year |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `false` | **operator opt-in** - see below |
| `SECURE_HSTS_PRELOAD` | `false` | **operator opt-in** - see below |
| `SECURE_PROXY_SSL_HEADER_NAME` | `HTTP_X_FORWARDED_PROTO` | the trusted forwarded-scheme header |
| `SECURE_PROXY_SSL_HEADER_VALUE` | `https` | its value |

**The header name is the WSGI environ name, not the wire header.** Django
reads `request.META[SECURE_PROXY_SSL_HEADER_NAME]`, and gunicorn maps the wire
header `X-Forwarded-Proto` to `HTTP_X_FORWARDED_PROTO`. Setting
`X-Forwarded-Proto` would silently never match: every proxied request would
look like plain HTTP and be redirected to HTTPS, forever. Your front door must
actually send the header on every request and overwrite any client-supplied
copy - nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-Proto $scheme;   # required: the SSL redirect
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
}
```

A client-supplied `X-Forwarded-Proto` must never survive to Django. If your
proxy appends rather than overwrites, strip the inbound header first; a
forgeable pair lets a client claim HTTPS over a plain connection and bypass
the redirect.

**Plain-HTTP local run.** `docker compose up` over `http://localhost` has no
TLS terminator, so put these two in the repo-root `.env` for local work (both
are pass-throughs - nothing else needs editing):

```bash
SECURE_SSL_REDIRECT=false
SECURE_HSTS_SECONDS=0
```

Leaving them on locally is the failure mode worth knowing: the redirect sends
the browser to an `https://` nothing serves, and HSTS makes the browser refuse
plain HTTP for a year - a localhost lockout you cannot undo from the browser.

**`includeSubDomains` and `preload` are deliberately not defaults.**
`includeSubDomains` breaks every plain-HTTP subdomain (including ones outside
this app's control), and the HSTS preload list is effectively irreversible -
removal takes months. Turn them on only once every host in the tree is HTTPS
for real, and treat preload as a one-way door. Coordinate with
`CSRF_TRUSTED_ORIGINS` and `CORS_ALLOWED_ORIGINS`, which must list the
deployed `https://` storefront origin.

`SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` need no configuration: they
already default to secure whenever `DJANGO_DEBUG=false`.

Both halves are pinned: `TlsHardeningDeployContractTests` reads the defaults
out of the committed compose file, boots the app with them, and asserts that
`DJANGO_DEBUG=true` with no hardening keys keeps the development-safe
defaults - so the deploy layer can be hardened without the settings defaults
(and therefore local development and the test suite) moving.

## Error tracking and uptime (SPEC-22-04, R-22.13 / R-22.12)

### Error tracking (SPEC-22-04, R-22.13)

Optional end to end. **Without a DSN the SDK is never imported**, nothing is
initialised and nothing leaves the process; the app, the test suite and CI are
exactly as they were before this existed. With one set:

| Key | Default | Meaning |
| --- | --- | --- |
| `SENTRY_DSN` | unset | the project ingest DSN; unset = off |
| `SENTRY_ENVIRONMENT` | `DJANGO_ENV` | so staging errors are never filed as production |
| `SENTRY_SAMPLE_RATE` | `1.0` | fraction of error events kept |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0` | performance tracing; nothing instruments transactions, so leave it at 0 |

Unparseable values fall back to the documented default with a warning (a typo
in an env file cannot take the app down), and a DSN set on a host whose image
predates the `sentry-sdk` pin degrades to a named warning rather than refusing
the boot.

What gets reported: unhandled exceptions, plus the `django.request` 5xx
records the app **already** logs - SPEC-7-02 pins that channel at ERROR, and a
`LoggingIntegration` at `event_level=ERROR` is what turns those records into
events. So there is no second reporting path to keep in sync. PII is off:
request bodies, cookies, headers and identifiers stay in the process unless an
operator deliberately enables them.

The DSN carries a public ingest key: it is configuration, but still
environment-only - repo-root `.env` / your platform's secret store, never the
repository. It is the one string in this section a scanner may flag; it belongs
in `.env`, and the committed `.env.example` shows it commented out.

### Uptime: `/health/` and the container healthcheck (SPEC-22-04, R-22.12)

`/health/` is public, cheap, and answers **503** while the database is
unreachable or the media mount is unwritable. Three things consume it:

1. **The image's own `HEALTHCHECK`** (`backend/Dockerfile`): a loopback probe
   of `/health/` on `$PORT`. A 200 exits 0; anything else - including the
   degraded 503 - exits non-zero, so a broken storefront is `unhealthy` rather
   than merely "running".
2. **The compose healthcheck** (`docker-compose.yml`), with its own timings.
   Both probes identify themselves the way a real request does: `Host` is the
   first entry of `DJANGO_ALLOWED_HOSTS` (an unlisted host is a 400) and
   `X-Forwarded-Proto: https` is the trusted scheme header (so the SSL redirect
   above does not bounce the probe to an `https://` the container does not
   serve). Both are read from the app's own env, so they cannot drift from it.
3. **An external uptime monitor.** Register it against the public
   `https://<host>/health/` and treat **any non-200 as down** - a 503 is a real
   degradation signal, not a monitoring hiccup. Poll no faster than once a
   minute: the endpoint counts orders and carts on every call. Point it at the
   public URL through the TLS front door, not at `127.0.0.1` inside the
   container, or it measures nothing an outage would break.

Which monitor is a deployment fact and stays out of this repository - no
vendor token, DSN or account id belongs in a runbook. Create the check in
whichever service you already run, keep its credential in your secret store,
and record here only the contract above: URL, expected status, what a 503
means, poll interval.

If you terminate TLS somewhere other than a reverse proxy (a CDN, a managed
ingress), the same rule applies: whatever sends requests to this container must
set the forwarded-scheme header for every request, and the container's own
`8000` port must not be exposed to the internet - a direct connection would
bypass the front door and the HSTS redirect entirely.

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
filename scheme **and** the engine's own file signature (`PGDMP` for a
`--format=custom` dump, the sqlite header for a sqlite copy), so a file an
operator parked in the backup directory - a note, somebody else's copy, a
hand-taken `pg_dump`, which defaults to plain SQL - is never a deletion
candidate, and a `BACKUP_RETENTION` below 1 is refused rather than quietly
deleting every backup the moment it finishes making one. Anything the rules
cannot vouch for is left in place for a human to judge; that is the safe
direction for a deletion loop.

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

## WAF ruleset for the front door (SPEC-22-06, R-22.16)

The WAF lives in the reverse proxy in front of gunicorn, not in this
application. The division of labour, and the reason the two do not fight:

| Layer | Owns | Knows about |
| --- | --- | --- |
| WAF / proxy | connection floods, protocol abuse, known-bad payloads, request-size and method abuse, bot scans of `/admin` | IP, connection rate, bytes, raw bytes |
| This app | per-account and per-scope abuse | DRF throttle scopes, `request.user`, session, JWT, cart identity |

**The WAF must NOT duplicate the app-layer throttles.** DRF's `ScopedRateThrottle`
already budgets the sensitive public endpoints per identity, and it is the layer
that can distinguish one account hammering login from one IP behind a shared
NAT - which a proxy cannot. A proxy rate limit is per-IP, so a tight one
locks out an entire office, a school, or a mobile carrier's CGNAT range; and
`THROTTLE_RECOVERY_RATE` is already tighter than the generic auth budget
because each accepted request sends an email. So the proxy limits are
**deliberately looser** than the strictest app budget and exist only to stop
volumetric floods before they cost a worker:

- `limit_req_zone` at the edge, ~**60 req/min per IP** with a small burst -
  comfortably above `THROTTLE_COUPON_RATE` (10/min) and far below a
  volumetric flood. If you set it near the app budgets you will 429 real
  customers whose app-layer budget would have been per-account and generous.
- `limit_conn` per IP (e.g. 20) to cap keep-alive abuse, not per account.
- Connection and request-body ceilings, which the app cannot enforce before
  reading: `client_max_body_size` should match or slightly exceed
  `MAX_UPLOAD_MB` (`backend/.env.example`) so the proxy does not reject a
  product image the app would have accepted.

The proxy is also the right place for the rules the app must never see:

- **Method allow-listing** per location: the API is `GET/POST/PATCH/DELETE`;
  anything else (or `TRACE`) is a `405` at the edge.
- **Path hygiene**: deny dotfiles, `/.env`, `/.git`, `*.bak`, `*.sql`, backup
  dumps, and the `.env`-shaped paths a scanner probes. Nothing in this
  application serves them, and the app's own 404s are cheap enough to be a
  free hit-list for a scanner.
- **Payload inspection**: the OWASP ModSecurity Core Rule Set, or the
  equivalent rules your platform already provides, in **detection-only** mode
  first. This app's legitimate traffic includes long free-text fields (product
  copy, addresses, order notes) that SQLi/XSS heuristics false-positive on;
  enabling blocking mode before you have read the false positives will reject
  real checkouts. Exclusions that are correct and not negotiable: the
  Razorpay webhook path (signature-verified body the rules will flag), the
  admin's rich-text product fields, and `/health/`.
- **A challenge/deny list** for repeated `401`/`403` bursts, so credential
  stuffing is cheap at the edge. Alert on it; that pattern is the earliest
  signal of a targeted attack.

Belt and braces, and the reason the app keeps its own gates:

- **Admin**: keep `/admin/` off the public internet if you can (VPN, IP
  allow-list, or the platform's own admin-protection feature). The app's MFA
  door (SPEC-17-05) is the real control; the proxy restriction is only a
  reduction in exposure.
- **Do not cache authenticated or error responses**, and never cache
  `/health/` at the edge - a cached 200 would hide a degraded storefront from
  both the healthcheck and the uptime monitor.
- **Set `X-Forwarded-Proto` on every proxied request** (see "Transport
  hardening in the deploy artifact" above). Without it the SSL redirect
  loops, and the container healthcheck cannot reach `/health/`.

A managed WAF (CDN or platform edge) is a legitimate choice for this - it
replaces the proxy rules above and terminates TLS - and it is a **deployment
fact**: its account, zone id and API token belong in your secret store, never
in this repository or in CI. Configure it, then point this runbook's
"Transport hardening in the deploy artifact" section at what it actually
sends, and keep
`DJANGO_ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` listing its origin.

**What a WAF is not.** It is not a substitute for the app's authorization: a
rule cannot know whether *this* order belongs to *this* session. Every
authorization decision stays in Django, where SPEC-17-03's CSRF gate, the
ownership scopes and the capability decorators live.

## Secret rotation (SPEC-22-06, R-22.8)

Where the secrets live today: the repo-root `.env` on the deploy host
(git-ignored, chmod 600, owned by the deploying user), GitHub Actions secrets
for CI (dummy Razorpay values only - the suite never uses live credentials),
and each provider's own console for Razorpay / SMTP / Sentry. `.env.example`
documents the names; no value is in the repository. The scans that protect
this are real and already wired: `gitleaks.toml`, `.gitguardian.yml`, and
`.github/workflows/secret-scan.yml`. **None of them rotates a secret** - they
find a leak after the fact. Rotation is the procedure below, and it is manual
by design.

### Rotation schedule

| Secret | Rotate | Why / cost of delay |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | **quarterly**, or on any suspicion of exposure | signs sessions and cookies; a leak is session forgery until rotated. Rotating logs everyone out (see the caveat) |
| `RAZORPAY_KEY_SECRET` | **quarterly**, or on any staff-offboarding event | holds money-movement authority; the key id alone is not secret |
| `POSTGRES_PASSWORD` / `DATABASE_URL` | **quarterly** | full read/write on the store, including customer data |
| SMTP password / app password | **every 6 months** (provider policy) | outbound mail from your domain; a leak enables spoofed mail, not account access |
| `SENTRY_DSN` | **on request from the provider**, and if a former employee's project access is revoked | an ingest DSN is a write-only public key: a leak lets an outsider submit noise, not read your errors |
| Deploy host SSH key | **on staff offboarding**, immediately | the key that can deploy |
| Third-party tokens (CDN, WAF, error tracker API) | on offboarding, and when a vendor's key is past its expiry | outside this repository; see each provider's policy |

Quarterly means a calendar reminder with an owner, not "when someone
remembers". Put the dates in the same place as the deploy log.

### Rotating without downtime

Order matters: **mint the new credential before retiring the old one**, then
roll, then retire. Each step below is reversible until the last.

**1. Database password.** Postgres has no dual-password state, so this is the
one rotation with a genuine (short) window:

```bash
# a. change the role's password in place (existing sessions keep working)
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "ALTER ROLE \"$POSTGRES_USER\" WITH PASSWORD '<new>';"

# b. update the repo-root .env (DATABASE_URL if set, else POSTGRES_PASSWORD)
# c. recreate the app so it connects with the new password
./scripts/release.sh          # build -> migrate check -> migrate -> collectstatic -> restart

# d. verify before touching anything else
curl -fsS https://<host>/health/ | grep '"status": *"ok"'
```

Because `docker-compose.yml` assembles `DATABASE_URL` from the same
`POSTGRES_*` variables the `db` service reads, the password has exactly one
home (`.env`) and step (b) cannot drift out of sync with the database it
names. Old connections drain as gunicorn workers recycle; `SIGTERM` on the
restart is graceful, so in-flight requests finish. If the release script ever
runs with a password that no longer matches, `/health/` answers **503** and
`release.sh` fails at its `--wait` checkpoint - fail-closed, and the fix is the
correct `.env`.

**2. Razorpay.** Create the new key pair in the Razorpay dashboard **while the
old one is live**, update `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`, run
`./scripts/release.sh`, and verify a real checkout end to end
(`docs/runbook-incident-recovery.md` §4 reconciles the counters). Only then
revoke the old pair. The app reads both values from the environment and signs
gateway requests with them, so this needs no code change and no deploy beyond
the normal release.

**3. SMTP.** Update the password/app password at the provider, put it in
`.env`, restart the app, and verify by triggering one real send - the
password-reset flow, or `manage.py` with `ALERT_RECIPIENTS` set. Nothing else
in the app caches the credential, so the restart is sufficient.

**4. Sentry DSN.** Replace the value in `.env` and restart. The SDK is
initialised once at boot (SPEC-22-04), so a restart is the whole procedure;
with no DSN the app is simply unmonitored, never broken.

**5. SSH deploy key.** Add the new public key to the host's `authorized_keys`
**before** removing the old one, then re-run a deploy to prove it works, then
remove the old key. GitHub Actions' deploy job uses
`core.ssh_command`/`ssh-key` from repository secrets - update the secret, run
one deploy, then revoke the old key in the provider.

### The `SECRET_KEY` caveat (read before rotating it)

`DJANGO_SECRET_KEY` signs sessions, the signed cookies, and - because
simplejwt signs with it - **every outstanding access and refresh token**.
Rotating it is therefore a **mass logout plus a mass token invalidation**, not
a transparent operation:

- Decide the window deliberately (an announced low-traffic period). There is
  no way to keep existing sessions across a rotation in this codebase.
- Outstanding refresh tokens die with it. Customers are asked to sign in
  again; carts are **guest/session-owned**, so a customer cart that was not
  checked out is lost for that customer. Say so in advance if it matters.
- Per-environment isolation is why this is safe to do at all: a rotation in
  one environment does not affect the others, because each has its own key
  (SPEC-22-03 [R-22.3]).
- Rotate **one environment at a time**, and never rotate staging and
  production in the same window - you want to know which one broke.

### Verify a rotation actually happened

A rotation nobody checked is an assumption:

1. `curl -fsS https://<host>/health/` answers 200 (the app boots with the new
   configuration).
2. The old credential no longer authenticates - the honest test, and the only
   one that proves anything: try the **previous** `DATABASE_URL` password and
   the **previous** Razorpay secret against the live services and expect
   refusal. Do this from a throwaway shell, never from a machine whose `.env`
   has been updated.
3. One real flow per rotated credential: a login, a checkout, a password-reset
   email.
4. `git log -p -- backend/.env.example docker-compose.yml Procfile scripts/` and
   a `gitleaks detect` sweep show no value was introduced into the repository
   while rotating.
5. The deployment still passes its own gates: a release, a green
   `docker compose ps` (healthy), and the uptime monitor seeing 200.

**If a secret was ever committed, pushed, or pasted into a ticket:** rotate it
first and treat it as compromised from that moment - then remove it from
history. Rewriting history does not un-leak anything that was pushed; only
rotation does. Order is rotation, then cleanup, then the audit trail.

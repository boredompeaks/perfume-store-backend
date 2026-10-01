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

## Non-production data: the controlled process (SPEC-22-09, closes R-22.4)

The staging contract above says the staging database is "restored from a
sanitised dump or seeded fresh". This section is that sentence made
concrete, because *how a non-production environment gets data* is a privacy
decision, not a convenience one, and "just copy the prod database to my
laptop" is the failure this closes.

Companion register: `backend/docs/retention.md` says which fields are
personal, why each is collected and how long it lives. This section says who
may hold a copy of them outside production.

### The rule

1. **Production customer data stays in production.** No production order,
   address, phone, email, username, MFA secret, audit payload or gateway
   identifier is copied to a laptop, a staging host, a CI job, a shared drive
   or a ticket.
2. **There is no ad-hoc production dump for a developer machine.** A dump is
   for *restore* (`docs/runbook-incident-recovery.md` §3a) and
   `manage.py restore_drill` refuses unsafe targets - that refusal protects a
   restore from landing on a live database, and it is **not** a licence to
   make a second copy somewhere new. `manage.py backup_db` exists for the
   restore path and for nothing else.
3. **A non-production environment that needs realistic-looking rows gets
   synthetic rows** (next subsection). If something can only be reproduced
   against real data, that is a request for the approved exception path below,
   not an ad-hoc dump.

### Default path: synthetic data

This project's realistic dataset already exists and it is synthetic: the
committed factories in `backend/common/testing.py` (`make_product`,
`make_coupon`, `make_user`, `make_staff`) are what the suite exercises every
list, detail, checkout, admin and health surface against. Anything a
developer needs locally that those factories do not already express is a
**shape** requirement - volume, key distribution, pagination depth, stock
out-of-stock edges - and shape is what synthetic data is for.

Recipe (no new code, synthetic values only, safe to run repeatedly):

```bash
venv\Scripts\python manage.py shell
```

```python
from decimal import Decimal
from django.contrib.auth.models import User
from orders.models import Coupon
from products.models import products

# A synthetic catalogue. Invented names/prices; `category` is a CharField.
for i, (name, price) in enumerate(
    [("Rose Aurum", "1499.00"), ("Amber Nocturne", "1899.50"), ("Cedre Blanc", "1299.00")]
):
    products.objects.get_or_create(
        slug=f"sample-{i}",
        defaults=dict(
            name=name,
            description="Synthetic demo row (SPEC-22-09). No customer data.",
            price=Decimal(price),
            size=50,
            stock=25,
            category="Floral",
        ),
    )

# A live coupon, so the discount path is exercisable end to end.
Coupon.objects.get_or_create(
    code="DEMO10",
    defaults=dict(
        discount_type="percentage",
        discount_value="10",
        minimum_order_amount="0",
        maximum_discount=Decimal("500.00"),
        active=True,
        usage_limit=100,
    ),
)

# A demo account with NO usable password: it cannot authenticate until the
# developer sets one locally (`manage.py changepassword demo-buyer`), so no
# shared credential exists to leak. The address is on a reserved
# documentation domain, so mail to it can never reach a person.
User.objects.get_or_create(
    username="demo-buyer",
    defaults={"email": "demo-buyer@example.com"},
)
```

Rules that make this the safe default rather than a convenient one:

- **Addresses live on reserved documentation domains** - `example.com`,
  `example.net`, `example.org`, `example.invalid` (RFC 2606). That keeps
  synthetic rows out of real inboxes and out of the operator allowlist that
  `manage.py email_send_probe` (below) sends to.
- **Invented names, addresses and phone numbers only.** A synthetic row that
  accidentally carries a real person's details is a production-data copy, and
  it is indistinguishable from one once it is in a dump.
- **No production media.** Product images come from a placeholder file; a
  customer's uploaded photo is personal data too and lives on the
  `media_data` volume, not in your checkout.
- **CI needs no data step at all**: `.github/workflows/backend-tests.yml` runs
  the suite against the database the test runner creates, and the factories
  fill it. Never add a step that loads data into CI.
- **Staging is seeded fresh** from this recipe (or an approved artefact,
  below). Staging's own `DATABASE_URL` and secret key keep it a separate
  environment (SPEC-22-03 [R-22.3]) - a seeded staging database still never
  contains a production row.

The production catalogue seed is **SPEC-14-1's** (section 14,
"Recommended backend project structure") and is owned there, not here: this
section does not build it and does not describe its contents. What it does
say is the boundary - a seed builds the *catalogue*, and the catalogue is
never a vehicle for customer rows.

### Exception path: an approved anonymised or derived dataset

Sometimes synthetic data genuinely cannot express the thing being worked on
(a specific unicode/locale mix, a specific legacy value shape). Then the
answer is an **approved, sanitised artefact**, not a dump:

| Rule | Why |
|---|---|
| Written approval from the privacy owner **and** the deployment owner, recorded before any production row is read (who, when, purpose, which fields, which environment, who holds the artefact) | a data copy without a named owner and an end date is how personal data goes missing |
| The transformation runs **inside the production perimeter** (the deploy host or a controlled copy there); the raw dump never leaves it, and the artefact is the sanitised output only | a sanitiser on a laptop has already copied the raw data |
| Every field in the `retention.md` register is dropped or replaced, by **data class**, not by a hand-picked column list: `User.email`, `User.username`, `Order.full_name` / `.phone` / `.address` / `.city` / `.state` / `.pincode` / guest `.email`, `TOTPDevice.secret` (MFA secrets never leave, transformed or not), `AuditEvent.detail` (it carries usernames - [R-17.32]) | a field-level mapping that misses one column ships a customer's name |
| **Free text is the trap**: `products.description`, order notes, `StockMovement.note`, admin `LogEntry` messages and any string inside `AuditEvent.detail` can hold a human-typed name, so the artefact is **reviewed by a human** before it is loaded anywhere | redaction rules over structured columns cannot see a name typed into a text area |
| Keep shape, drop values: volumes, key distributions, index selectivity and pagination depth survive sanitisation, which is what a non-prod environment is for | the point of the artefact is realism of *behaviour*, not of *people* |
| The artefact is named with its approval, loaded into a throwaway database, and deleted when the work ends - never committed, never uploaded to a shared drive, never attached to a ticket | it is still personal data until proven otherwise |
| If it reaches a developer machine, that machine is a production-data machine: full-disk encryption, no cloud sync, no backup, and it is wiped at the end | the copy outlives the ticket that justified it otherwise |
| The decision and the artefact's fate are recorded in `retention.md`'s register and in `docs/changes.md` | an unrecorded copy is an unmanageable copy |

### Why the answer is not "just dump production"

The failure modes, in the order they actually bite:

- **It is a second, unmanaged store of personal data.** It inherits none of
  the retention, access-control or deletion machinery that
  `backend/docs/retention.md` describes, so it is precisely what that
  register exists to prevent.
- **Erasure stops being complete.** A deletion request processed in
  production leaves the copy intact, and nobody knows who holds it - the right
  is only honoured if every copy is known.
- **Laptops multiply it.** The dump lands in a cloud-sync folder, in the
  laptop's own backups, and in a screenshot pasted into a chat thread.
- **It scales the wrong way.** Every developer who asks gets a copy, so the
  count of uncontrolled stores grows with headcount while the register
  documents none of them.
- **It is the wrong default to teach.** The safe answer has to be the short
  one, which is why synthetic data is the default and this is the exception.

### Self-check before any dataset leaves your machine

```python
# 1. every account address is on a reserved documentation domain
from django.contrib.auth.models import User
list(User.objects.exclude(email__endswith="@example.com").values_list("email", flat=True))  # must be []

# 2. eyeball the first rows: every value must be obviously invented
from orders.models import Order
list(Order.objects.values_list("full_name", "city", "phone", flat=True)[:10])  # synthetic only
```

Both empty/obviously-invented means the dataset is clean. A non-empty result
in a non-production environment means real customer data is present: delete
the database, and if the rows came from anywhere but a test fixture, treat it
as the disclosure it is and follow `docs/runbook-incident-recovery.md` §5.

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

## Email delivery verification (SPEC-22-10, closes R-22.18)

### What `/health/` proves, and what it cannot

`/health/`'s `smtp_configured` check is a **configuration** check: it is true
when `EMAIL_HOST_USER` and `EMAIL_HOST_PASSWORD` are non-empty
(`ops/services.py`, `get_health`). It cannot tell you the difference between

- the app can *attempt* a send,
- the server *accepted* the message, and
- the message *arrived* in an inbox,

and it is blind to the two failures that hurt customers: a **wrong password
(accepted at boot, `535` at send time)** and **spam-folder delivery** (the send
succeeds and the mail is silently filed). Delivery is the provider's and the
receiver's business, so it is verified from the **receiving** end - by a human
confirming that one message actually arrived. That is what this section is
for.

### The probe: `manage.py email_send_probe`

```bash
# 1. the plan, and the default: NOTHING is sent
docker compose run --rm -T backend python manage.py email_send_probe

# 2. deliver one probe to every allowlisted operator mailbox
docker compose run --rm -T backend python manage.py email_send_probe --send

# 3. or to one of them (must already be allowlisted)
docker compose run --rm -T backend python manage.py email_send_probe --send \
    --recipient ops@your-domain.example
```

Run it with the deployment's own environment (inside the compose project, or
over the same `ssh` the release script uses), because the probe reports and
uses exactly the `EMAIL_*` configuration the deployed container runs with. It
goes through `common.notifications.send_email` (SPEC-19-1's single send path),
so the sender address and the backend are the app's real ones - a probe on a
private code path could pass while the app's own mail failed.

Gates, all of which refuse by name rather than guess:

| Gate | Behaviour | Why it is structural |
|---|---|---|
| **Allowlisted recipients only** | the recipient must be in `settings.ALERT_RECIPIENTS` - your own staff mailboxes, which already receive every admin alert. Empty allowlist = refused. `--recipient` naming anything else = refused, naming nothing = every allowlisted entry | the probe cannot be aimed at a customer, and it never reads a `User.email`, so it cannot leak another person's address because it never looks at one |
| **Dry run by default** | nothing is sent without `--send` | the safe answer to "what would this do?" is the answer you get |
| **A backend that never speaks SMTP is refused** | `console`, `locmem` and `dummy` backends accept the message and report success | probing one would report a delivery that never happened - and it is why the test suite, which runs on `locmem`, can never send real mail through this command |
| **Deployment facts only** | the body carries environment, server, sender and timestamp; no order, no username, no product data | the probe is filed in mailboxes that may be forwarded |
| **A refused delivery is loud** | an SMTP rejection exits non-zero with the server's own status text (`535` auth, `550` relay denied, connection refused) and never echoes the credential | an accepted-looking probe that quietly failed is the failure this replaces |

### The operator procedure

1. **Dry run.** Read the plan: the environment name, the backend, the server,
   the sender address and the recipients. Two things are worth catching here
   before any mail leaves: a sender address on the *wrong* domain (see
   alignment below) and a recipient list that is empty or stale.
2. **Send**, then **confirm on the receiving end**: inbox *and* spam folder. A
   probe that is "sent" and never seen is a failure, not a slow mail server.
3. **Keep the receipt.** In the recipient's mail client, "show original" /
   "view source" and keep the `Authentication-Results` header (below) with the
   deploy log entry. That header is the evidence; the command's own output is
   only the claim that the server accepted the message.
4. **When to run it:** after any SMTP credential change (the SMTP step in
   "Secret rotation" above), after a DNS or mail-provider change, after a
   restore to a new host (the new host's outbound IP is not in SPF until it
   is), and whenever a customer reports an email that never arrived
   (`docs/runbook-incident-recovery.md` §1, "Order exists but customer says
   they never got it" is the order-side twin of this).

### Reading the receipt: the three headers that matter

| Header | Good | What a failure means |
|---|---|---|
| `Authentication-Results` - `spf=pass` | the sending host is authorized for the envelope sender's domain | the provider's SPF record is missing, or the sending host is not in it, or the record exceeds 10 DNS lookups (which makes SPF fail with `permerror`) |
| `Authentication-Results` - `dkim=pass` | the message was cryptographically signed and the public key is published | signing is off at the provider, or the selector's `_domainkey` TXT record is unpublished/rotated away. Note a *forwarded* copy legitimately fails this - it is not evidence about the original send |
| `Authentication-Results` - `dmarc=pass` | an authenticated (SPF or DKIM) identifier **aligned** with the visible `From` domain | neither SPF nor DKIM aligned with the `From:` domain, so the provider applies its DMARC policy (usually quarantine or reject) |
| `Return-Path` vs `From` | same domain, or the provider's aligned bounce domain | **the single most common cause of spam-folder delivery**: `DEFAULT_FROM_EMAIL` on a domain the SPF/DKIM records do not authorize. `DEFAULT_FROM_EMAIL` defaults to `EMAIL_HOST_USER` (`config/settings.py`), so the mailbox you send from must live on the sending domain |

### SPF, DKIM and DMARC for the sending domain

This application does **not** sign its mail itself: there is no DKIM library in
`backend/requirements.txt`, and signing is the provider's job. What this
repository owes is the configuration contract, and `DEFAULT_FROM_EMAIL` /
`EMAIL_HOST_USER` landing on the sending domain.

| Record | What to publish | Rules that matter |
|---|---|---|
| **SPF** (TXT on the sending domain, or its mail subdomain) | `v=spf1 include:<your provider's selector> -all` | the selector is a **provider fact** - copy it from the provider's dashboard, never invent it. `-all` (hard fail), never `+all`: `+all` authorizes the whole internet to mail as your domain. Keep the record under the **10-DNS-lookup limit** (`include:` costs lookups; flatten when you approach it). One SPF record per domain - a second one is a permanent error |
| **DKIM** | enable signing at the provider; publish its `_domainkey.<selector>` TXT record (2048-bit RSA) in DNS | the selector must be in the SPF record too if the provider relies on SPF alignment. Rotate the key when you decommission a provider, and delete the old `_domainkey` TXT record with it, or a leaked signing key stays usable |
| **DMARC** | `_dmarc.<domain>` TXT, published in **three stages**: `v=DMARC1; p=none; rua=mailto:<dmarc-reports-address>` -> read the aggregate reports for a full reporting cycle -> `p=quarantine; pct=...` -> `p=reject` | start at `p=none`: jumping straight to `p=reject` with misaligned `From` silently breaks every transactional mail this store sends. Review the aggregate reports monthly; they are the only source that says who is failing alignment and why. Keep the DMARC domain aligned with whatever `DEFAULT_FROM_EMAIL` is |
| **Sending host hygiene** | PTR/reverse DNS matching the hostname, submission on 587 with STARTTLS (`EMAIL_USE_TLS=true`), never unauthenticated port 25 | a sending host with no matching PTR is unrouteable-looking to most receivers, which is the spam folder by another route |
| **No marketing envelope** | n/a | this store sends **transactional mail only** - there is no marketing surface (`retention.md`), so no consent/unsubscribe machinery applies. The SPF/DKIM/DMARC discipline above still does: bulk-sender rules are not the reason to get it right, deliverability of password resets is |

### When mail lands in spam: symptom -> cause -> fix

| Symptom | Likely cause | Fix |
|---|---|---|
| `spf=pass dkim=pass dmarc=fail` | the visible `From` domain is not the authenticated one | point `DEFAULT_FROM_EMAIL` / `EMAIL_HOST_USER` at the domain the provider signs, or publish records for the domain actually used in `From` |
| `spf=fail` (or `permerror`) | the sending host is not in the SPF record, or the record exceeds 10 lookups | fix the provider's record; flatten `include:` chains |
| `dkim=none` | signing is off at the provider | enable it; until then SPF alignment alone must carry DMARC |
| `dkim=fail` on a *forwarded* copy | forwarding breaks the signature; it is not evidence about the original send | check the original in the sending mailbox, not the forwarded copy |
| `550 relay denied` / `535` at send time | the account may not be permitted to send for that domain, or the password/app-password was rotated (SMTP step in "Secret rotation") | re-check `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` at the provider, then re-probe. A stale password still shows `smtp_configured: true` on `/health/` - this is exactly the case the probe exists for |
| Delivered to junk at one provider only | reputation of that provider's shared sending IP | use a dedicated sending subdomain or a dedicated provider; do not "fix" it with the app |
| Nothing arrives and **no bounce** | the envelope sender (`Return-Path`) is not a monitored mailbox, or the bounce was discarded upstream | make `Return-Path` a mailbox somebody reads, and watch it: an SMTP acceptance is not a delivery |
| Bounces arrive | invalid or unreachable addresses | this store has **no bounce-processing code** - handle bounces as an operator task on the provider's suppression list. Never set the envelope sender to a customer's address; it is a data-protection problem, not just a deliverability one |

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
`.env`, restart the app, and verify **by delivering a probe**:
`manage.py email_send_probe --send` against an `ALERT_RECIPIENTS` mailbox, and
confirm it in the receiving inbox (and check its `Authentication-Results`
header). A restart alone is not the verification: a stale credential still
reports `smtp_configured: true` on `/health/`, and a password-reset flow would
fail for real customers instead. See "Email delivery verification" above.
Nothing else in the app caches the credential, so the restart is sufficient
for the *code*, and the probe is what verifies the *delivery*.

**4. Sentry DSN.** Replace the value in `.env` and restart. The SDK is
initialised once at boot (SPEC-22-04), so a restart is the whole procedure;
with no DSN the app is simply unmonitored, never broken.

**5. SSH deploy key.** Add the new public key to the host's `authorized_keys`
**before** removing the old one, then re-run a deploy to prove it works, then
remove the old key. The deploy job runs **no** ssh-agent and sets no
`core.ssh_command`: it writes the `DEPLOY_SSH_KEY` repository secret to a
`mktemp` file under `umask 077` - with `DEPLOY_KNOWN_HOSTS` beside it (empty
means `StrictHostKeyChecking=accept-new`), both removed by an `EXIT` trap,
nothing echoed and no `set -x` - and calls `ssh -i "$key" -o
IdentitiesOnly=yes`. Rotation is therefore: replace the `DEPLOY_SSH_KEY`
secret, run one deploy to prove the new key authenticates, then delete the old
public key from the host.

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

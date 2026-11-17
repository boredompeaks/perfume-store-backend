# Data retention & erasure (SPEC-17-09, R-17.29/31/32/33)

Source requirements (spec 17.4, "Privacy and data retention"):

- [R-17.29] "Document why each sensitive field is collected." (spec line 4411)
- [R-17.31] "Establish retention and deletion policies." (spec line 4415)
- [R-17.32] "Avoid putting personal information in logs." (spec line 4417)
- [R-17.33] "Support applicable customer data access/deletion rights." (spec line 4419)

This document is the field-purpose/retention register and the decision
record the spec asks for. It enumerates only fields that actually exist
in the models (grep-verified against the migrations and model sources at
the time of writing); "not collected" is stated explicitly where the
spec's caution would expect a field, so a future field cannot silently
appear without this register being updated.

## Scope of collection (R-17.29)

The store's PII surface is deliberately minimal. What is collected, and
why:

### accounts (Django `auth.User` + `accounts.TOTPDevice`)

| Field | Purpose | Retention | Erasure |
|---|---|---|---|
| `User.username` | Account identity + login credential. Chosen over email as login key so the primary contact channel is not also the enumeration surface. | Life of account. Audit `detail.username` copies: see the [R-17.32] decision below. | Replaced with a unique tombstone (`erased-<pk>`) by `accounts.services.erase_account`; the row is retained (see erasure path below). |
| `User.email` | Order correspondence, email verification, password reset, username reminder. The single contact channel; no marketing mail exists. | Life of account. | Replaced with a non-routable unique tombstone (`erased-<pk>@invalid`) by the same service (below). |
| `User.password` | Authentication (PBKDF2 hash only; plaintext never stored or logged). | Life of account; rotated by every password reset. | Set unusable by the tombstone; only the hash ever existed. |
| `TOTPDevice.secret` | Staff MFA second factor (SPEC-17-05, R-17.9). Staff-only; returned once at enrollment, never logged. | Life of enrollment; re-enrollment overwrites. | **Not deleted by erasure.** `accounts.services.erase_account` never deletes the account row, so the device row survives the tombstone; a tombstoned account cannot authenticate (inactive + unusable password), and the device row goes only if the account row is finally deleted (`CASCADE`). |

Not collected: legal name, date of birth, gender, profile photo,
third-party identifiers, marketing preferences (no marketing surface
exists), device fingerprints. No analytics beacons run.

### saved delivery addresses (`accounts.Address`)

| Field | Purpose | Retention | Erasure |
|---|---|---|---|
| `Address.line1` / `.line2` / `.city` / `.state` / `.postal_code` / `.country` | Where a saved address delivers: reused at checkout so the customer does not retype it, and the address a courier hands over to. | Until the customer deletes the row, or the owning account is erased. | **Hard-deleted** by `accounts.services.erase_account` in the same transaction as the tombstone (`Address.objects.filter(user_id=...).delete()`); the row is never blanked and kept. |
| `Address.phone` | Courier contact for deliveries to this address. | Same as the row. | Deleted with the row, as above. |
| `Address.label` | The customer's own name for the address ("Home", "Work"). | Same as the row. | Deleted with the row, as above. |
| `Address.is_default` | Which saved address checkout preselects. A view-level per-user invariant, not a database one. | Same as the row. | Deleted with the row, as above. |
| `Address.user` (FK) | Ownership: the authorization boundary every address read/write is scoped by. `CASCADE`. | Same as the row. | The owner's rows are deleted by the erasure service; the cascade itself is never exercised, because erasure never deletes the account row. |

No order, payment or refund row references an `Address` (the delivery fields
on `Order` are their own columns), so deleting these rows cannot touch a
financial record.

### orders (`Order`, `OrderItem`, `OrderStatusEvent`, `Payment`-fields)

| Field | Purpose | Retention | Erasure |
|---|---|---|---|
| `Order.full_name` | Shipping label + courier handover; the courier cannot deliver to a username. | Life of account + legal/invoice window (see below). | **Not anonymized by erasure**: retained unchanged by `accounts.services.erase_account` (scope below). |
| `Order.phone` | Delivery coordination (courier contact before handover). | Same as `full_name`. | Not anonymized by erasure; retained unchanged. |
| `Order.address` / `.city` / `.state` / `.pincode` | Fulfilment + invoice address. | Same as `full_name`. | Not anonymized by erasure; retained unchanged. |
| `Order.user` (FK) | Ownership: whose order is this (IDOR scoping). | Life of account + legal window. | Retained; the FK keeps pointing at the tombstoned account row, which erasure never deletes (see erasure path below). |
| `Order.razorpay_order_id` / `.razorpay_payment_id` | Gateway reconciliation, refunds, dispute evidence. Gateway identifiers only — **no card data, CVV, or card numbers are ever stored** (the gateway holds them; the spec's "avoid storing unnecessary payment information" is met by design). | Same as the order row. | Retained with the order; this service does not change it. |
| `Order.email`? | **Not collected** — correspondence rides the account email. Guest checkout (SPEC-3-02) MUST add its contact email here and to this register. |

### cart (`Cart`, `CartItem`)

| Field | Purpose | Retention | Erasure |
|---|---|---|---|
| `Cart.session_id` | Anonymous browser cart correlation. Contains no self-declared PII (opaque session token, not a user identifier). | Client-side session lifetime; an abandoned cart is inert commerce state — unless it was created while signed in, where `Cart.user` (below) links it to the account. | Not subject to erasure. Erasure does not touch cart rows: a signed-in cart keeps its `Cart.user` link to the tombstoned row, so the correlation ends when the cart itself is deleted at session expiry, or on a final account-row deletion. |
| `Cart.user` (FK) | Links a signed-in cart to its account so the cart follows the login. `CASCADE`. | Session lifetime. | **Survives erasure**: the FK keeps pointing at the tombstoned row, which erasure never deletes; the cascade runs only if the account row is finally deleted. |
| `Cart.coupon`, `CartItem.product/quantity` | Non-personal commerce state. | Session lifetime. | n/a |

### audit trails (`common.AuditEvent`, admin `LogEntry`, app logs)

| Field | Purpose | Retention | Erasure |
|---|---|---|---|
| `AuditEvent.detail` | JSON payload: gateway ids, amounts, coupon ids, attempted usernames (auth events). Accountability evidence for business events (R-7.20). | Append-only; retained with the store's records. | Immutable by design (append-only ledger, SPEC-6-02 pattern); see the [R-17.32] decision for the username content. |
| `AuditEvent.actor` (FK) | Who did it. `SET_NULL` so the trail outlives the user. | Append-only. | `SET_NULL` applies only if the user row is eventually deleted; after a tombstone the FK still names the retained row, so the trail keeps its actor. |
| `LogEntry` (Django admin) | Privileged-action trail (SPEC-7-01): which staff member changed which row. | Append-only. | Staff accounts are not customer erasure subjects; customer users never write LogEntry rows. |
| App/log output (`common.audit` logger, SPEC-7-02) | Ops observability of the business trail. | Deployment log rotation policy. | See the [R-17.32] decision below. |

## Retention & deletion policies (R-17.31)

1. **Authentication state**: verification/reset tokens are single-use,
   stateless (signed-token scheme) and expire by construction — a stale
   token is a 400, not data. JWT refresh tokens rotate and blacklist
   (SPEC-17-01); the blacklist rows are the only persisted auth state.
2. **Orders** are financial records: the retention window is the local
   tax/invoicing law's requirement (in India, the record-keeping period
   under the CGST Act — 72 months from the annual return deadline — is
   the governing benchmark). **The number is a legal process input, not
   a code constant**; the code provides the erasure mechanism below, and
   the deployment sets the clock.
3. **Carts** are transient. An anonymous cart is non-personal; a cart
   created while signed in is linked to its account by `Cart.user` alone,
   and erasure leaves that link pointing at the tombstone — the cart itself
   expires with the session, which is not an erasure mechanism.
4. **Audit trails** are append-only by design and are the store's
   accountability record. Erasure never deletes the account row, so
   `AuditEvent.actor` keeps naming the retained tombstone rather than a
   live person; `SET_NULL` applies only if a user row is finally deleted.

## Deletion-request flow & the SPEC-6-10 boundary (R-17.33)

Reading of the ledger (section-19 note + section-06 row): **"R-19.36
data-deletion request processing → SPEC-6-10 (customer data export/
deletion workflow owner, S9/S17)"**. SPEC-6-10 is PENDING and owns the
*operational* deletion workflow: the customer-facing request intake, the
staff processing queue, and the export half of the access right.

This task therefore does **not** build a parallel request endpoint —
that would duplicate 6-10's surface. What R-17.33 demands *today* is
that a deletion right exists and is documented; both hold:

- **Existing erasure path (manual):** a customer writes to the support
  address (the storefront privacy page names it; staff contact settings
  env `NEXT_PUBLIC_SUPPORT_EMAIL` / admin Site settings) and support
  performs the erasure. The account half is a **tombstone in place**, not
  a deletion: `accounts.services.erase_account(user, actor=...,
  reason=...)` replaces `email` with a non-routable per-account
  placeholder (`erased-<pk>@invalid`), replaces `username` with a unique
  tombstone, empties `first_name`/`last_name`, sets `is_active=False`,
  makes the password unusable, deletes the `Address` rows the account
  owns, and appends one `AuditEvent` naming the actor and the validated
  reason without copying the old identity. The row and its primary key
  survive, so every order keeps its owner.
  - **The service's complete scope** is the account row's identifying
    fields, its address rows, and the one audit entry. A second call raises
    `AccountAlreadyErased` (a `ValueError`) before changing anything, so a
    retry cannot rewrite the tombstone or append a second audit row. An
    operator `reason` that repeats the account's old email, username or
    names, or that contains any email address or a phone-shaped run of ten
    or more digits, raises `UnsafeErasureReason` (also a `ValueError`)
    before the first write, because the reason is stored in an append-only
    row and a refused one must leave nothing behind.
  - **What it does NOT touch**: the account row itself, `Order` and
    `OrderItem`, `Order.full_name`/`phone`/`address`/`city`/`state`/
    `pincode`, the gateway identifiers, `PaymentEvent`, `Refund`,
    `TOTPDevice`, `Cart`/`CartItem`, and every audit row. The service
    never calls `delete()` on the account, so no on-delete policy is
    exercised and no cascade runs from that row. Orders are retained
    unchanged because they are financial records: the order, its gateway
    payment references, its `PaymentEvent` deliveries and its `Refund`
    rows all outlive the login. An account with no orders is tombstoned
    the same way.
  - Orders outside the retention window may still be deleted as a
    separate order-retention operation; that is not the account-erasure
    mechanism and does not apply while an order is inside the window.
  - Blanking `Order.full_name`/`phone`/address fields is a separate
    order-deidentification operation, not part of `erase_account`: this
    service leaves the retained order's delivery fields untouched, and any
    later step must keep the financial columns reconcilable and only run
    once the legal window allows.
- **SPEC-6-10's owed surface** (when it lands): a self-service request
  endpoint (throttled, authenticated, uniform responses), a staff
  processing queue, and the data-export half of the access right. This
  document is the field register that workflow must operate against.

## Copies outside production (SPEC-22-09, R-22.4)

This register governs **what may ever be copied** out of production; the
procedure that says how a developer or a staging host obtains a realistic
dataset lives in `deploy-runbook.md` ("Non-production data: the controlled
process"). The two halves of that rule, restated here so neither is read
without the other:

- The default is **synthetic data** - this project's own committed test
  factories (`backend/common/testing.py`), on reserved documentation
  addresses. No production customer row is copied to a laptop, a staging
  host, a CI job or a shared drive, and **no ad-hoc production dump is ever
  taken for a developer machine**.
- An **anonymised/derived** dataset is an exception, not a shortcut: it needs
  recorded approval, runs inside the production perimeter, is sanitised by the
  data classes in the register above (every `AuditEvent.detail` payload and
  every free-text field included, since a name typed into a description is
  still personal data), is human-reviewed before use, and is deleted when the
  work ends.

Why the register has to say this at all: a copy made outside production
carries none of the retention or erasure machinery described above, so an
erasure request honoured in production is not honoured in that copy - and a
copy nobody recorded is a copy nobody can delete. `manage.py backup_db` and
`manage.py restore_drill` exist for the restore path only
(`deploy-runbook.md`, "Backups"); `restore_drill`'s refusal of unsafe
restore targets is a restore guard, not permission to duplicate a dump.

## Log-hygiene decision: usernames in the audit mirror logger (R-17.32)

**Decision: RETAIN the username in AuditEvent rows and the `common.audit`
log mirror, with justification. Not pseudonymized.**

Rationale:

1. The audit trail exists for accountability and dispute resolution
   (R-7.20, SPEC-7-01): failed-login analysis, credential-stuffing
   forensics (SPEC-7-01/SPEC-1-06 abuse stream), and payment dispute
   evidence all need a *stable human-meaningful* identifier. A numeric
   user id is stable but unreadable in a log stream; a username is the
   identifier the actor chose and is already public-adjacent (login is
   username-based, so it is the credential, not a secret).
2. What R-17.32 targets is *sensitive personal information* in logs —
   passwords, hashes, tokens, card data, addresses. None of those
   appear in any log line: the no-secrets pin
   (`tests/test_logging_baseline.py`, `NoSecretsInLogTests`) guards the
   payment-credentials surface, `AuditEvent.detail` carries only
   gateway ids/amounts/coupon ids/usernames, and the settings-log pin
   (`test_logging_baseline.py`) keeps config values out of output.
3. The erasure interplay is bounded: `detail.username` is the *login
   identifier*, not contact data — it cannot be re-associated with a
   person the way an email or a saved address can once the account is
   erased. Retaining it therefore keeps the accountability value while
   the account's own PII is removed from the live surface: the email, the
   names and every `Address` row. What this service does not remove is the
   delivery fields and phone on a retained `Order` — a financial record it
   does not touch; blanking those is the separate order-deidentification
   step the register above names.
4. Scope guard going forward: **email, phone, address, and full name
   are FORBIDDEN in log output** — any future log/audit addition that
   would carry them must pseudonymize to the user id first. That rule
   is this document's job; the code comment at the emission site
   (`common/models.py`, `AuditEvent.record`) pins the same decision
   where a future contributor will actually see it.

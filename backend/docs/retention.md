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
| `User.username` | Account identity + login credential. Chosen over email as login key so the primary contact channel is not also the enumeration surface. | Life of account. Audit `detail.username` copies: see the [R-17.32] decision below. | Deleted with the account row (see erasure path below). |
| `User.email` | Order correspondence, email verification, password reset, username reminder. The single contact channel; no marketing mail exists. | Life of account. | Nulled/anonymized with the account (below). |
| `User.password` | Authentication (PBKDF2 hash only; plaintext never stored or logged). | Life of account; rotated by every password reset. | Dies with the account row. |
| `TOTPDevice.secret` | Staff MFA second factor (SPEC-17-05, R-17.9). Staff-only; returned once at enrollment, never logged. | Life of enrollment; re-enrollment overwrites. | Dies with the user row (`on_delete=CASCADE`). |

Not collected: legal name, date of birth, gender, profile photo,
third-party identifiers, marketing preferences (no marketing surface
exists), device fingerprints. No analytics beacons run.

### orders (`Order`, `OrderItem`, `OrderStatusEvent`, `Payment`-fields)

| Field | Purpose | Retention | Erasure |
|---|---|---|---|
| `Order.full_name` | Shipping label + courier handover; the courier cannot deliver to a username. | Life of account + legal/invoice window (see below). | Anonymized on erasure. |
| `Order.phone` | Delivery coordination (courier contact before handover). | Same as `full_name`. | Anonymized on erasure. |
| `Order.address` / `.city` / `.state` / `.pincode` | Fulfilment + invoice address. | Same as `full_name`. | Anonymized on erasure. |
| `Order.user` (FK) | Ownership: whose order is this (IDOR scoping). | Life of account + legal window. | On erasure, orders are either retained-deidentified or deleted per the erasure path below. |
| `Order.razorpay_order_id` / `.razorpay_payment_id` | Gateway reconciliation, refunds, dispute evidence. Gateway identifiers only — **no card data, CVV, or card numbers are ever stored** (the gateway holds them; the spec's "avoid storing unnecessary payment information" is met by design). | Same as the order row. | Retained with the deidentified order for audit/reconciliation. |
| `Order.email`? | **Not collected** — correspondence rides the account email. Guest checkout (SPEC-3-02) MUST add its contact email here and to this register. |

### cart (`Cart`, `CartItem`)

| Field | Purpose | Retention | Erasure |
|---|---|---|---|
| `Cart.session_id` | Anonymous browser cart correlation. Contains no self-declared PII (opaque session token, not a user identifier). | Client-side session lifetime; abandoned carts are inert data, not PII (no link back to a person). | Not subject to erasure; deleting the session or the account severs any correlation. |
| `Cart.coupon`, `CartItem.product/quantity` | Non-personal commerce state. | Session lifetime. | n/a |

### audit trails (`common.AuditEvent`, admin `LogEntry`, app logs)

| Field | Purpose | Retention | Erasure |
|---|---|---|---|
| `AuditEvent.detail` | JSON payload: gateway ids, amounts, coupon ids, attempted usernames (auth events). Accountability evidence for business events (R-7.20). | Append-only; retained with the store's records. | Immutable by design (append-only ledger, SPEC-6-02 pattern); see the [R-17.32] decision for the username content. |
| `AuditEvent.actor` (FK) | Who did it. `SET_NULL` so the trail outlives the user. | Append-only. | On account deletion the FK nulls; the row stays. |
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
3. **Carts** are transient and non-personal; no policy needed beyond
   session expiry.
4. **Audit trails** are append-only by design and are the store's
   accountability record; they retain *deidentified* references (FKs go
   `SET_NULL`), never a live link to an erased person.

## Deletion-request flow & the SPEC-6-10 boundary (R-17.33)

Reading of the ledger (section-19 note + section-06 row): **"R-19.36
data-deletion request processing → SPEC-6-10 (customer data export/
deletion workflow owner, S9/S17)"**. SPEC-6-10 is PENDING and owns the
*operational* deletion workflow: the customer-facing request intake, the
staff processing queue, and the export half of the access right.

This task therefore does **not** build a parallel request endpoint —
that would duplicate 6-10's surface. What R-17.33 demands *today* is
that a deletion right exists and is documented; both hold:

- **Existing erasure path (manual, working today):** a customer writes
  to the support address (the storefront privacy page names it; staff
  contact settings env `NEXT_PUBLIC_SUPPORT_EMAIL` / admin Site
  settings) and support performs the erasure through the admin. The
  mechanics support relies on, per data class:
  - account: delete the `User` row — cascades `TOTPDevice` and cart
    rows, nulls `AuditEvent.actor`/`OrderStatusEvent.actor`, nulls
    order FK only if orders are deleted with it;
  - orders to be erased outright (outside the legal window): delete the
    `Order` rows (`OrderItem`/`OrderStatusEvent` cascade with them);
  - orders inside the legal window: blank the six address fields on
    the `Order` (`full_name`, `phone`, `address`, `city`, `state`,
    `pincode`) and mark `user` for deletion — the financial columns
    (amounts, gateway ids, timestamps) stay reconcilable, the person is
    gone;
  - `email` cannot simply be nulled while the row lives: set it to a
    non-identifying placeholder per the same action.
- **SPEC-6-10's owed surface** (when it lands): a self-service request
  endpoint (throttled, authenticated, uniform responses), a staff
  processing queue, and the data-export half of the access right. This
  document is the field register that workflow must operate against.

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
   person the way an email or address can once the account row (the
   only place email/address live) is erased. Retaining it therefore
   keeps the accountability value while the erased person's PII
   (email, addresses, phone) is fully removed.
4. Scope guard going forward: **email, phone, address, and full name
   are FORBIDDEN in log output** — any future log/audit addition that
   would carry them must pseudonymize to the user id first. That rule
   is this document's job; the code comment at the emission site
   (`common/models.py`, `AuditEvent.record`) pins the same decision
   where a future contributor will actually see it.

# Security Policy

How to report a vulnerability in this repository, what is in scope, and what
"test mode" means for this project's payments. This policy is
license-independent: it is in force regardless of how the repository's
licensing question is settled.

## Reporting channel

Use **GitHub Security Advisories on this repository** — the "Report a
vulnerability" form on the Security tab. That private, issue-based advisory is
the whole channel: this project has **no bug-bounty program** and **no
published security mailbox**, and the owner's decision record has not
decreed any other contact, so no address is invented here. Open a draft
advisory rather than a public issue when a finding is exploitable.

In the report, include:

- the endpoint, flow, or component affected;
- exact reproduction steps (against the local stack, see the README);
- the impact you believe an attacker gains;
- whether this has been disclosed anywhere else, and when.

No response-time SLA is published; the repository owner triages advisories
directly. Do not run aggressive or sustained testing against the live
deployment — reproduce findings locally with the compose or venv flow the
README documents.

## Scope

**In scope** — anything whose security is decided by code in this repository:

- the Django/DRF API under `backend/` (accounts and auth, cart, orders and
  checkout, payments and webhooks, coupons, stock, the admin surface, ops
  endpoints);
- the Next.js storefront under `frontend/`;
- the deployment and CI definitions in this repository (compose file, release
  scripts, GitHub workflows, dependency locks) insofar as they affect what
  ships.

**Out of scope**:

- third-party services this code merely talks to — the payment gateway
  itself, the CDN/hosting edge, GitHub's own infrastructure — report those
  to those vendors;
- volumetric denial of service, and physical or social-engineering attacks
  against people;
- findings that require a live (non-test) payment credential or production
  secrets to demonstrate;
- an upstream dependency advisory with no demonstrated path through this
  codebase — a concrete exploit chain through this application is in scope
  and is welcome.

## Test mode and payments

- **Payment keys are environment configuration, never code.** The example
  environment file documents `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and
  `RAZORPAY_WEBHOOK_SECRET` with empty values; real values live only in the
  git-ignored `.env`, and no secret or deployment URL is written into source
  or docs.
- **The backend test suite never moves real money.** The suite is required
  to be network-free (a standing rule in `backend/docs/conventions.md`), and
  the Razorpay client is always mocked with test-mode dummy credentials, so
  tests exercise the payment flows without contacting the gateway and
  without real keys.
- **CI holds no payment credentials.** Workflow policy forbids secrets in
  CI; the one end-to-end job that would drive the real payment widget
  self-skips unless it is deliberately armed with a repository variable, and
  even then it runs against Razorpay test mode only. Continuous integration
  therefore never performs live payments.
- **If you test a payment-related finding, use Razorpay test mode only.**
  Never test against live keys or real money. If you discover a credential
  committed to the repository, report it through the advisory channel above;
  pushes and pull requests are also scanned for secrets in CI (gitleaks),
  but a scan is not a disclosure.

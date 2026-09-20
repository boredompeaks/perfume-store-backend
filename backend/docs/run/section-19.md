# Section 19 — Notifications and background jobs (task ledger)

Created 2026-09-19 from the S19 compliance prefetch. Spec lines: 4603–4777. Prefetch result: 38 rows — 2 IMPLEMENTED / 6 PARTIAL / 28 MISSING / 1 DEVIATES / 1 N-A (search-index rebuild: no index exists, ORM-only search verified).

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-19-1 | 19 | P2 — DEVIATES [R-19.0]: notifications are scattered manual send_mail calls inside controllers (accounts/views.py:75-76 et al; zero order emails) — build an in-process notification/email service + AuditEvent-subscribing dispatcher (single send path, no queue — explicitly NOT SPEC-2-03's async work), prerequisite substrate for SPEC-1-12/SPEC-3-14 email content | 4603–4777 [R-19.0] + S1[1.12] | SHIPPED (audit cycle-1 SHIP @ 8c05e47: independent suite 388 = 384 pass + 4 xf, cov 100.00% @ 1639; migrated templates hexdump-verified byte-identical incl. literal-& link params; rollback-together probe — send raises → payment 200/confirmed, ORDER_PAID audit committed, outbox empty, ERROR logged; record() diff empty, no signals/queue/migrations; accounts/tests.py = 2 patch-target hunks only, .env.example = 1 comment line; pushed b9f153d..50e0803; SHIP row written by orchestrator — auditor commit denied by env rules, tree pristine) | 1 |
| SPEC-19-2 | 19 | P2 — Admin alert dispatch: low-stock breach, payment-failure spikes, security-sensitive account changes, integration outages — reusing existing detection (ops/services.py:124-131, /health/ checks) + the AuditEvent trail; covers the alerting halves of R-19.15/20/21/27 | 4603–4777 [R-19.15],[R-19.20],[R-19.21],[R-19.27] | PENDING (P2 — ride the S19 build loop) | 0 |
| SPEC-19-4 | 19 | P3 — Back-in-stock opt-in: customer notification-preference model + restock trigger hook on stock edits | 4603–4777 [R-19.11] | PENDING (P3 — ride the S19 build loop) | 0 |

Owner attributions (no duplicate tasks; IDs 19-3 and 19-5 deduped by the orchestrator):
- R-19.30 webhook ingestion job → **SPEC-1-06** (ledgered webhook subsystem owner: HMAC verification, idempotent processing, event-ID store; second S-prefetch proposal deduped to it — S17's SPEC-17-04 slot was retired for the same reason).
- R-19.36 data-deletion request processing → **SPEC-6-10** (customer data export/deletion workflow owner, S9/S17).
- R-19.3..R-19.14 (order-lifecycle emails: placed/paid/shipped/delivered/cancelled/returns/refunds + admin new-order/reconciliation/failure alerts) → **SPEC-1-12** (notification halves) + **SPEC-3-14** (confirmation content halves); returns/refunds domains unbuilt → SPEC-3-18/SPEC-1-05.
- R-19.22..R-19.29 (job capabilities: retry/backoff, idempotency, dead-letter, observability, manual retry, alerting, transactional-email job, reservation-expiry job) → **SPEC-2-03** (infra, Owner S19) + **SPEC-12-03** (expire_reservations) + **SPEC-5-08** (jobs admin surface) + **SPEC-7-02** (failure logging delivered).
- R-19.31 tracking-update job: email half → SPEC-3-14; tracking data model → SPEC-1-08/SPEC-6-11. R-19.18 failed-delivery visibility + R-19.25 job observability → ride SPEC-2-03/SPEC-5-08. R-19.37 job-failure ambiguity → rides SPEC-2-03 (vacuously satisfied today: sync paths atomic + fully branched).

Grandfathered (capability exists synchronously): R-19.35 analytics aggregation (ops/services.py), sync CSV export half of R-19.32 (orders/admin.py) — job-ization is an infrastructure preference riding SPEC-2-03.

Convention findings: none violated (env-driven email config, locmem test backend, atomic AuditEvent writes, throttled email endpoints); the one spec-conformance issue is the scattered-call email pattern itself → SPEC-2-03/SPEC-19-1. Tests for S19 work must run against locmem (common/testing.py:35 already guarantees).

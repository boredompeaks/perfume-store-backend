# Section 11 — Payment architecture (task ledger)

Created 2026-09-19 from the S11 compliance prefetch. Spec lines: 3523–3578. Prefetch result: 19 rows — 6 IMPLEMENTED / 4 PARTIAL / 6 MISSING / 1 DEVIATES / 2 N-A (verified absences).

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-11-01 | 11 | DEVIATES [R-11.1]: create_payment writes unique razorpay_order_id check-then-act OUTSIDE transaction.atomic/select_for_update, no IntegrityError retry, no @throttle_scope (orders/views.py:450-512; conventions.md:16,17,24) — wrap generation in atomic + select_for_update on the order, retry on unique-violation instead of check-then-act, add a payment throttle scope + env-driven rate key | 3523–3578 [R-11.1] + conventions.md:16,17,24 | PENDING (from S11 compliance prefetch) | 0 |

Owner attributions (no duplicate tasks):
- R-11.3 (webhook signature verification), R-11.5 (duplicate webhook deliveries), R-11.6 (delayed payment confirmation), R-11.19 (provider event-ID store with unique constraints + idempotent processing) → **SPEC-1-06**; the webhook handler must carry R-11.16's no-duplicate-dispatch and R-11.18's unique-payment-id upsert properties, and respect SPEC-9-02's /api/v1/ namespace.
- R-11.9 (refund initiation vs provider confirmation tracked separately) + R-11.17 (single-refund idempotency as a built-in constraint) → **SPEC-1-05**.
- R-11.10 (provider-transaction reconciliation incl. verify-time fetch-and-assert amount==order.total) + R-11.13 (exception workflow behind the existing manual-reconciliation admin guards) + the R-11.11 hardening note → **SPEC-1-18**.
- R-11.7 (failure/cancellation → no failed/retryable payment state) → **SPEC-10-04**.
- R-11.14 (duplicate order prevention — checkout side has no idempotency key; no test covers duplicate POST /checkout/) → **SPEC-9-01**.
- R-11.8 (authorization vs capture tracking) → **SPEC-6-12**.

NOT-APPLICABLE (verified, not dodges): R-11.16 (no fulfilment-command pathway exists to duplicate; re-entry gate views.py:585-589 is the future hook) and R-11.17 (no refund capability exists; constraint recorded as a requirement of SPEC-1-05).

Convention findings: create_payment atomicity/throttle breaches (→ SPEC-11-01); Decimal end-to-end COMPLIANT (paise conversion exact for 2-dp DecimalField — section-10's truncation precaution verified non-issue on this path); verify-path atomic+select_for_update COMPLIANT; env-driven config COMPLIANT (no RAZORPAY_WEBHOOK_SECRET key yet — required by SPEC-1-06); serializers explicit COMPLIANT; Razorpay fully mocked COMPLIANT. Cosmetic P3: Black would reflow orders/urls.py:38-42 and config/urls.py:30-38 — align when touched (conventions.md:3).

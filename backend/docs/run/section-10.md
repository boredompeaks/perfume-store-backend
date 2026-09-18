# Section 10 — Order lifecycle and state machines (task ledger)

Migrated verbatim from the monolithic ledger 2026-09-19. Spec lines: 3405–3522. Compliance prefetch 2026-09-18: 23 rows — 6 IMPLEMENTED / 8 PARTIAL / 8 MISSING / 1 DEVIATES / 0 N-A. Build-now queue SPEC-10-01..05. DEVIATES R-10.1: single status field vs multi-dimension lifecycle.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-10-01 | 10 | P1 — Split lifecycle into explicit dimensions: payment_status + fulfilment_status fields, orders/state.py as single source of truth, wire verify_payment + admin actions, serializer read-only expose | 3405–3522 [R-10.1 DEVIATES],[R-10.3],[R-10.4 half] | PENDING (from S10 compliance prefetch; return/refund/shipment machines stay SPEC-3-18/1-05+6-12/1-08) | 0 |
| SPEC-10-02 | 10 | P1 — Immutable transition audit trail: OrderStatusEvent model (from→to, actor, trigger, at) written in save_model/_bulk_set_status/verify_payment; bulk path → per-row saves in atomic (fixes auto_now staleness) | 3405–3522 [R-10.12],[R-10.18],[R-10.17] | PENDING (from S10 compliance prefetch; complements SPEC-7-01 business-event audit) | 0 |
| SPEC-10-03 | 10 | P2 — mark_shipped preconditions: payment-captured + items present before shipped; extension hooks for SPEC-1-08 shipment checks | 3405–3522 [R-10.19],[R-10.14] | PENDING (from S10 compliance prefetch) | 0 |
| SPEC-10-04 | 10 | P2 — COD branch + failed/retryable payment state in the machine (depends on 10-01); payment-method input itself is checkout section's row | 3405–3522 [R-10.2],[R-10.4],[R-10.17] | PENDING (from S10 compliance prefetch) | 0 |
| SPEC-10-05 | 10 | P3 — Side-effect contract per transition: minimal notification hooks for shipped/delivered/cancelled (full notifications = SPEC-1-12/S19) | 3405–3522 [R-10.16] | PENDING (from S10 compliance prefetch) | 0 |

Missing dimensions attribute: shipment→SPEC-1-08, returns→SPEC-3-18, refunds→SPEC-1-05/6-12, item-level depth→SPEC-6-11. Machine is currently transition-safe on all 3 mutation surfaces — no illegal transitions possible today. Convention note: bulk .update() skips auto_now → SPEC-10-02; paise int() truncation → S11 note.

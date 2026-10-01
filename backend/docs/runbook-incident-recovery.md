# Runbook — incident response and recovery

Owner: on-call engineer. Scope: this store's Django backend (`backend/`).

This runbook documents what the code **already enforces**, so an operator
acts on the shipped contract instead of inventing one under pressure. Every
step names the file that pins it. Where something is deliberately manual
(there is no refund flow), that is stated as a decision, not a gap to be
worked around.

Related: `docs/vulnerabilities.md` (V-03 = no refund flow),
`docs/architecture.md`, `docs/conventions.md`, `docs/test-gaps.md`.

---

## 0. The restore drill (run this first, and on every release)

```bash
# 1. what would it do? reads settings only, writes nothing, always exit 0
venv\Scripts\python manage.py restore_drill --dry-run

# 2. the rehearsal: dump -> migrate an empty throwaway database -> load ->
#    compare per-model row counts. exit 0 = the backup format round-trips.
venv\Scripts\python manage.py restore_drill

# 3. the failure path, for CI (proves a bad restore is reported, not passed)
venv\Scripts\python manage.py restore_drill --simulate-failure   # exits 1
```

Safety contract (`ops/management/commands/restore_drill.py`, pinned by
`tests/test_restore_drill.py`):

- it **refuses** any target that is not an in-memory database, not a file
  under the OS temp directory, and not the configured production database;
  the refusal happens before a single query and exits non-zero;
- the source is only ever **read** (`dumpdata`) — no `flush`, no `DROP`, no
  `migrate` against it;
- every write lands in a temp directory the command creates and deletes,
  including on the failure path;
- the restored database is reached through a scratch connection alias that is
  registered on the connection handler's thread-local store only, so it
  cannot outlive the command or be resolved by anything else.

A passing drill does **not** prove your backups are complete — it proves the
mechanism round-trips. Taking an actual backup is `manage.py backup_db`
(`ops/management/commands/backup_db.py`), whose retention/volume/schedule
story is in `docs/deploy-runbook.md` ("Backups") — the S22 concern this section
used to defer to is now built. §3a below is how you consume one of those dumps.

---

## 1. Detection

| Signal | Where it shows up | First move |
|---|---|---|
| Health probe degraded (non-`ok`, HTTP 503) | `GET /health/` (`ops/views.py:24`, checks from `ops/services.get_health`) — `database`, `media_writable`, `smtp_configured`, `razorpay_mode` | read the failing check name; `database: false` is a DB incident |
| Payment failures spiking | `ops.alerts.notify_payment_failure_spike` (mail to `ALERT_RECIPIENTS`, one per `ALERT_COOLDOWN_SECONDS`) + audit rows `payment.signature_rejected` | check the gateway dashboard before touching data |
| Stock-out / low-stock alert | `ops.alerts.notify_out_of_stock` / `notify_low_stock` | usually a symptom, not the incident |
| Suspicious admin activity | `ops.alerts.notify_security_change` + audit log `/admin/audit-log/` | §2 triage, then §4 escalation |
| "Order exists but customer says they never got it" | support ticket | §3 manual reconciliation |

Every alert is best-effort and deduplicated by cooldown: a failing alert path
can never block the operation that triggered it. Absence of an alert is not
evidence of health — `GET /health/` is the authority.

## 2. Triage (first 15 minutes, no writes)

1. `GET /health/` — which check is red, and is the database reachable?
2. `manage.py showmigrations` — pending migrations mean the schema on disk is
   behind the code; do **not** run `migrate` on an incident's database
   without a restore rehearsal (§0) and a second pair of eyes.
3. Read, do not write:
   - `/admin/audit-log/` — filter by `request_id` or by the order number.
     The trail is append-only; entries carry actor, target and before/after.
   - `OrderStatusEvent` rows for the order (`/admin/orders/<id>/`) — the
     lifecycle trail is separate from the audit log and never editable.
   - `manage.py shell` read-only queries, e.g.
     `Order.objects.filter(razorpay_payment_id__isnull=False, status="pending")`
     — orders that took money but never settled.
4. Decide which of the three situations you are in:
   - **A. data loss** (rows missing, wrong values) → §3 restore;
   - **B. money moved but state did not** → §3 manual reconciliation;
   - **C. code/infra defect** → §5 escalation, no data edits.

## 3. Recovery procedures

### 3a. Restore from a snapshot

0. Pick the dump: `ls -1t <BACKUP_DIR>/perfume-*.dump` — the schedule keeps the
   newest `BACKUP_RETENTION` of them (deploy runbook, "Backups"). `backup_db`
   writes PostgreSQL dumps in pg_dump's **custom** format, so they restore with
   `pg_restore` (the plain-SQL `psql` route does not apply):
   `pg_restore --no-owner --no-privileges -d <fresh-database> <dump>`. A
   `.sqlite3` dump from the same command is a complete sqlite file: copy it
   into place as the database file while the app is stopped.
1. Stop writes (read-only maintenance mode / scale the web tier to zero). A
   restore over a live database produces two divergent histories.
2. Take a copy of the CURRENT database first — you need it to diff against.
3. Restore into a **fresh** database file/instance; never `DROP` in place.
4. Verify with the drill's own measure, model by model, before pointing
   traffic at it:
   - per-model row counts vs the snapshot;
   - the newest `Order.created_at` vs the expected last order;
   - `auth_user` count vs the expected customer count;
   - the last `AuditEvent` timestamp (the trail must not be *ahead* of the
     data it describes).
5. Re-point `DATABASE_URL`, `manage.py check`, then `GET /health/`.
6. Re-open writes, then reconcile: §3b.

### 3b. Manual reconciliation — the contract that already exists

There is **no refund flow** (V-03). Cancelling an order that has taken money
is deliberately impossible in code, not merely discouraged:

- `orders/state.py:30` — `ALLOWED_TRANSITIONS` gives `pending -> {confirmed,
  cancelled}` and no edge out of a paid state; the comment states
  reconciliation is manual by design.
- `orders/admin.py` (`save_model`, and the bulk `cancel_pending` action) —
  an illegal edge aborts the save, the status is left unchanged, and the
  operator is told *"Cancelling a paid order needs a refund — reconcile
  manually."* / *"paid orders cannot be cancelled (no refund flow; reconcile
  manually)."*
- `orders/views.py` `admin_order_cancel` — the JSON twin answers the same
  situation with HTTP 409 and the same wording. Both surfaces refuse; neither
  can be talked into a silent edit.

**Therefore, for a paid order, reconcile outside the application:**

1. Record the truth first: order id, `razorpay_order_id`,
   `razorpay_payment_id`, amount, customer, and the reason (duplicate
   charge, fraud, out-of-stock, goodwill).
2. Refund/chargeback **at the gateway**, out of band. The store has no refund
   API — the gateway dashboard (or its API with a separate credential) is the
   system of record for money movement.
3. Record the outcome **through the surface that keeps the trail**, never with
   raw SQL: the order's admin change form is the only surface that can carry a
   payment reference or a refund note, and it enforces the state machine,
   re-derives `fulfilment_status`, and commits the transition plus its
   `OrderStatusEvent` in one transaction (`orders/admin.py` `save_model`).
   The JSON seam (`/api/admin/orders/<id>/fulfill/`, `/cancel/`) is
   deliberately narrower — it performs machine edges only and refuses
   everything else with a 409 — so it is not a reconciliation tool.
4. If the order must be voided rather than fulfilled, leave the status where
   the machine allows it and record the refund in the ticket: the trail must
   show what happened, even when the machine cannot express it.
5. Reconcile stock deliberately: `StockReservation` holds are released by the
   cancel paths; a paid order that is refunded does not auto-return stock, so
   adjust it through the inventory surface (`products` admin / stock-adjustment
   action) so the `StockMovement` trail records it.
6. Coupon usage: `Coupon.used_count` is incremented at payment verification.
   A refunded order's increment is **not** undone automatically — correct it
   deliberately and note it in the ticket.
7. Free capacity back to the customer through a manual goodwill order or an
   external credit; a new order is the only thing this store can create.

What you must never do: edit `Order.status` or payment columns with direct SQL
(bypasses `transition_allowed`, the preconditions and the audit rows), or
delete an `OrderStatusEvent`/`AuditEvent` row (both are append-only by
construction, and the admin refuses even the superuser).

### 3c. Duplicate charge / replayed checkout

Checkout is protected by the SPEC-21-1 pending-order fingerprint guard and the
SPEC-9-01 `Idempotency-Key` replay layer (`orders/views.py`), so a refresh or
a double submit collapses onto one order rather than minting a second charge
target. `backend/tests/test_checkout_refresh.py` pins the server-side half. If
you *see* two payable orders for one cart, treat it as an incident: capture
both ids, keep the one with the payment intent, refund the other per §3b.

## 4. Verification (before declaring the incident closed)

1. `GET /health/` → `status: ok`.
2. The order in question: status, payment reference, timestamps and the
   `OrderStatusEvent` trail read consistently in `/admin/orders/<id>/`.
3. Audit log contains an entry for every write you made, with the right actor.
4. Money reconciles: gateway dashboard total vs the store's order totals for
   the affected window.
5. Stock and coupon counters match the physical/gateway truth.
6. The regression is written down as a test — an incident that ships no pin
   will recur.

## 5. Escalation

| Situation | Escalate to |
|---|---|
| Suspected data breach or credential exposure | security owner immediately; rotate the affected secret, then §3a |
| Money movement that cannot be reconciled | finance owner + gateway support; freeze the affected order (do not delete) |
| Schema/migration problem | the section owner for that app; rehearse the migration on a restored copy first |
| Restore drill failing | deployment owner — the backup format itself is suspect |
| Anything requiring a refund | finance owner. Engineering does not authorise money movement |

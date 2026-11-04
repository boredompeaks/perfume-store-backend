# Pristine program — task ledger

Separate from the spec-compliance run (`spec-run-state.md`). Every task here is an architecture/toolchain remediation, not a spec section. Same evidence rules: every status change cites a SHA or verdict.

| Task ID | Requirement (<=12 words) | Spec lines | Status | Attempts | Evidence (sha/verdict) |
|---|---|---|---|---|---|
| PRISTINE-00 | Fix wall-clock-expiring return-window test (freeze clock) | n/a | SHIPPED | 1 | d7d400e; 2538 OK both engines, cov 99.83%; defect reproduced with freeze removed |
| PRISTINE-01 | Explicit AllowAny on 14 public views (zero behavior change) | n/a | SHIPPED | 1 | 43cf740; 14/14 ['AllowAny'] runtime-proved; pushed origin/spec-comp |
| PRISTINE-02 | Flip DEFAULT_PERMISSION_CLASSES to IsAuthenticated + allowlist test | n/a | SHIPPED | 2 | a27f637; 2548 OK both engines cov 99.82% (branch artifact); pushed origin/lane/a |
| PRISTINE-03 | Move common/testing.py out of product path (batched, 6 subtasks) | n/a | PENDING | 0 | scope >5 files, split |
| PRISTINE-04 | Import-linter ratchet (baseline violation count) | n/a | PENDING | 0 | config draft only |
| PRISTINE-05 | Ruff S,B,C90,DJ with corrected globs | n/a | PENDING | 0 | config draft only |
| PRISTINE-06 | Mypy phase 1 (common+orders+accounts+cart strict) | n/a | PENDING | 0 | config draft only |
| PRISTINE-07 | SCOPE check wired into CI | n/a | PENDING | 0 | script drafted |
| PRISTINE-08 | Package-marker guard (fail if a package dir lacks its marker) | n/a | BUILDING(partial) | 1 | 3 untracked files in wt-async; quota aborted before commit |
| PRISTINE-09 | ErrorEnvelope: inventory wrapped sites, classify view-fix vs cross-cutting | n/a | BUILDING(partial) | 1 | return view + envelope test modified; temp inventory script in wt-d; quota aborted |
| PRISTINE-10 | create_order CCN 48 -> <=10 decomposition | n/a | PENDING | 1 | quota aborted clean; no files changed |
| PRISTINE-11 | Split config/settings.py 1720 LOC | n/a | PENDING | 0 | audit §5 |
| PRISTINE-12 | tmpfs + fsync=off test compose profile | n/a | BUGS-FOUND | 1 | infra RAM-proved (mounts+fsync=off); container suite RED 59F/7E sqlite, 59F/25E pg |
| PRISTINE-13 | MFA code fixes (finance/support/inventory, encrypt secret, atomic counter, backup codes) | n/a | PENDING | 0 | audit §2; no spec edits |
| PRISTINE-14 | Docs cut: remove tautology + generated JSON; ADR template | n/a | PENDING | 0 | draft §8 |
| PRISTINE-15 | Agent prompt shrinkage + anti-shortcut clause | n/a | PENDING | 0 | draft §10 |
| PRISTINE-16 | Rebuild lane venvs (spec-comp-wt, wt-async) after wipe | n/a | SHIPPED | 1 | 4 lanes 3.14.2 venvs, manage.py check pass, 0 dirty |
| PRISTINE-17 | Permission allowlist: allow bare `python`/`py` (L1 class) | n/a | PENDING | 0 | `python --version` denied; venv path works |
| PRISTINE-18 | V-07: wrap Razorpay/network exceptions -> 503, no raw 500 | n/a | PENDING | 0 | only SignatureVerificationError caught today |
| PRISTINE-19 | Diagnose tmpfs container suite red (59F/7E) | n/a | PENDING | 0 | infra proven; env/tooling cause unknown |

## CI hygiene (separate from the pristine queue)

| Task ID | Requirement (<=12 words) | Status | Evidence |
|---|---|---|---|
| CI-HYGIENE-1 | mutation-weekly: sandbox working-dir bug | IN-AUDIT | 7567ce1; new always() mkdir step; static trace 1->0 violations |
| CI-HYGIENE-2 | Dependabot pip graph cannot resolve `-r requirements.lock` | PENDING(decision) | Graph Update run 41144517675 |
| CI-HYGIENE-3 | AI security scan: external monthly LLM quota exceeded | BLOCKED(external) | run 41145581700; not a repo file |
| CI-HYGIENE-4 | sqlite-nightly clock-bomb | FIXED-BY-PROMOTION | d7d400e on spec-comp, unpromoted |
| CI-HYGIENE-5 | Renovate issue #51 | FIXED-BY-PROMOTION | 067b0ce on spec-comp; packageRules[4] clean |

## 4 HIGH (V-04..V-07) fold check

| ID | Status | Evidence |
|---|---|---|
| V-04 rate limiting | FOLDED | DEFAULT_THROTTLE_CLASSES + scoped rates in settings.py |
| V-05 password policy | FOLDED | validate_password in accounts/serializers.py:82 |
| V-06 HTTPS/HSTS/cookies | FOLDED | SECURE_HSTS_*, SSL_REDIRECT, cookie secure flags present |
| V-07 Razorpay exceptions | OPEN -> PRISTINE-18 | only SignatureVerificationError at orders/views/payment.py:220 |

## GitHub + quality-scan fold (2026-11-04, re-derived @ c9af370)

Triage counts: 16 open / 6 fixed / 7 partial / 4 duplicates. P2 and P3 are OUT of scope by owner instruction.
Verified FIXED and therefore not queued: F-01 containment, F-02 DEBUG, F-03 manifest, F-05 tests, F-06/V-04 throttles, F-07/V-05 password policy, fix-plan 1.1 webhook.
Duplicates mapped (not rebuilt): #52/#62 -> PRISTINE-13, #58 -> PRISTINE-08, F-09/V-07 -> PRISTINE-18.

| Task ID | Requirement (<=12 words) | Source | Status | Evidence / scope |
|---|---|---|---|---|
| NEW-P0-1 | Refund: commit PENDING + idempotency key, gateway outside txn | #54 | PENDING | orders/refunds.py:189-300, backend/orders/models.py:631, orders/webhooks.py |
| NEW-P0-2 | Order.user PROTECT + deidentification, constraint rework | #53 | PENDING | orders/models.py:127-129,386-391 + migration |
| NEW-P1-1 | Payment reconciliation job + refund captured-then-refused | #86, F-04, fp1.3 | PENDING | new ops/cmd, ops/alerts.py, payment.py:498-528 |
| NEW-P1-14 | Fetch payment; assert amount==total*100 and captured | fix-plan 1.2 | PENDING | orders/views/payment.py:211-220 |
| NEW-P1-2 | Guest-checkout single source of truth (drop flag/branches) | #82 | PENDING | template.config.ts:112, CheckoutView/CartView + test |
| NEW-P1-3 | /api/v1 cutover then remove legacy mount | #79 | PENDING(split2) | 90 frontend call sites; config/urls.py:34-60 |
| NEW-P1-4 | Hide inert ProductVariant.stock in admin | #65 | PENDING | products/admin.py:465-470 + test |
| NEW-P1-5 | gunicorn gthread + timeouts | #64 | PENDING(INFRA) | Dockerfile:134, Procfile:16, compose |
| NEW-P1-6 | CONN_MAX_AGE=60 + CONN_HEALTH_CHECKS + docs | #63 | PENDING | settings.py:397,329-331 |
| NEW-P1-7 | Per-account backoff + breach check + pending cap | #61 | PENDING(split3) | settings.py:1283-1323, accounts/views.py:122 |
| NEW-P1-8 | Deploy frontend from protected main, not push to feat | #60 | PENDING(INFRA) | frontend-deploy.yml:4-6,20 + env protection |
| NEW-P1-9 | CODEOWNERS + branch protection with required checks | #59 | PENDING(INFRA) | .github/CODEOWNERS new; spec-comp unprotected (404) |
| NEW-P1-10 | Revenue net of refunds (or relabel gross) | #57 | PENDING | ops/services.py:20,53-63 |
| NEW-P1-11 | Off-host encrypted backup + missing-backup alert | #56 | PENDING(INFRA) | backend/ops/management/commands/backup_db.py:68-77, scripts/backup.sh:7,12 |
| NEW-P1-12 | Real pg_restore drill + manifest | #55 | PENDING | restore_drill.py:148-174 |
| NEW-P1-13 | Prod security defaults (HSTS/SSL/cookies) + deploy check | V-06 remainder | PENDING | settings.py:1670-1713 |

## Queue order (P0 first, then money, security, ops, arch; max 2 suites)

1. PRISTINE-02 SHIPPED (a27f637) · PRISTINE-08 in audit
2. NEW-P0-1a (running, lane/a) · PRISTINE-13 (after the lane frees; both touch accounts/views.py)
3. NEW-P0-2 (after NEW-P0-1; both touch orders/models.py)
4. NEW-P1-1 · NEW-P1-14 · PRISTINE-18 (one at a time; all touch payment.py)
5. NEW-P1-10 · PRISTINE-09 · PRISTINE-10
6. NEW-P1-7 · NEW-P1-6 · NEW-P1-13
7. NEW-P1-11 · NEW-P1-12 · NEW-P1-5 · NEW-P1-8 · NEW-P1-9
8. NEW-P1-3 (split) · NEW-P1-2 · NEW-P1-4
9. PRISTINE-12 (tmpfs green) · PRISTINE-19 (tmpfs diagnosis)
10. Promotions + CI-HYGIENE-2/3

## Escalation (2026-11-04)

- 7 subagent runs failed with "Free usage limit reached": PRISTINE-02 builder (x2), PRISTINE-08 builder/resume (x2), FULL auditor (x1), reduced PRISTINE-08 auditor (x1); earlier four-builder batch (x4).
- Intermittent successes: RE (7567ce1), triage, one general finish pass (b35e2de). The window is short, not lifted.
- Preserved, not lost: PRISTINE-08 committed b35e2de (unpushed); CI-HYGIENE-1 committed 7567ce1 (unpushed); PRISTINE-02 edits+figure on disk (uncommitted, test_mfa.py scope finding open); ledger c090576.
- Remote state unchanged: origin/spec-comp = 43cf740; origin/lane/b = 94859d1.
- No self-audit, no fake SHIP, no unverified commit was performed.

**Decision needed:** name a non-free model for subagents (or top up the quota). Until then the queue is paused at the audit gate.

**Resolved 2026-11-04:** subagents now run on `opencode-go/deepseek-v4.1-flash#max` (auditors) / `#high` (builders), the same identifier as the orchestrator. No further free-tier failures after the switch.

## Carry-forward P3 (do not lose)

- `backend/docs/api-contract.md:57` still says `MFASetupView` declares `permission_classes = []`; the code now declares `[AllowAny]`. Doc-only, outside PRISTINE-02's scope. Fix in the next accounts/docs task.
- PRISTINE-08 changelog row: dotted `scripts.tests.test_guards` trips `doc_claims`; and "one absolute path per line" should read "one path per line" (`guard_packages` prints relative). Fix in a one-file commit, then delta re-audit.

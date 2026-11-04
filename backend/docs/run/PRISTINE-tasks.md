# Pristine program — task ledger

Separate from the spec-compliance run (`spec-run-state.md`). Every task here is an architecture/toolchain remediation, not a spec section. Same evidence rules: every status change cites a SHA or verdict.

| Task ID | Requirement (<=12 words) | Spec lines | Status | Attempts | Evidence (sha/verdict) |
|---|---|---|---|---|---|
| PRISTINE-00 | Fix wall-clock-expiring return-window test (freeze clock) | n/a | SHIPPED | 1 | d7d400e; 2538 OK both engines, cov 99.83%; defect reproduced with freeze removed |
| PRISTINE-01 | Explicit AllowAny on 14 public views (zero behavior change) | n/a | SHIPPED | 1 | 43cf740; 14/14 ['AllowAny'] runtime-proved; pushed origin/spec-comp |
| PRISTINE-02 | Flip DEFAULT_PERMISSION_CLASSES to IsAuthenticated + allowlist test | n/a | PENDING | 0 | depends on 01 |
| PRISTINE-03 | Move common/testing.py out of product path (batched, 6 subtasks) | n/a | PENDING | 0 | scope >5 files, split |
| PRISTINE-04 | Import-linter ratchet (baseline violation count) | n/a | PENDING | 0 | config draft only |
| PRISTINE-05 | Ruff S,B,C90,DJ with corrected globs | n/a | PENDING | 0 | config draft only |
| PRISTINE-06 | Mypy phase 1 (common+orders+accounts+cart strict) | n/a | PENDING | 0 | config draft only |
| PRISTINE-07 | SCOPE check wired into CI | n/a | PENDING | 0 | script drafted |
| PRISTINE-08 | Restore __init__.py CI guard (fail if package dir lacks one) | n/a | BUILDING(partial) | 1 | 3 untracked files in wt-async; quota aborted before commit |
| PRISTINE-09 | ErrorEnvelope: inventory wrapped sites, classify view-fix vs cross-cutting | n/a | BUILDING(partial) | 1 | return.py+test modified, _inventory_tmp.py in wt-d; quota aborted |
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

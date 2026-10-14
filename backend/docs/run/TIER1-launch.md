# Tier 1 launch-blocker ledger — MVP punch list

Tasks here are the owner's Tier 1 hard-launch blockers (Zero-to-Launch triage,
2026-10-09), plus the P1 findings the launch audit surfaced. NOT spec-section rows.

**Owner decisions recorded 2026-10-09:** guest checkout DEFERRED (SPEC-3-02 off the
Tier-1 list; the dead-end guest order path is hardening work, not a feature); Razorpay
stays, more gateway integrations deferred ("that's just ux"); Cloudflare R2 or AWS S3
for media (S3-compatible, one implementation); the merchant delists via admin, not SQL.

**Tier-1 verification pass (auditor, TIER1-VERIFICATION.md in comp-wt, untracked):**
4 of 8 owner items were ALREADY-BUILT and came off the list — Sentry (SPEC-2-08,
initialized settings.py:1124), mark-shipped + tracking (SPEC-10-03, fields + admin
exist), admin product CRUD (SPEC-6-08, all four named axes), S3 seam (SPEC-2-04,
env-dotted-path STORAGES, no boto3). The launch audit's biggest find was NOT on the
list: the webhook reconciler.

**Engine policy, 2026-10-09:** PostgreSQL is the AUTHORITATIVE engine for this queue;
SQLite is being retired and its CI job removed (tracked below). Builders may skip the
SQLite leg; PostgreSQL must be green. Floor rule unchanged: PG passing count never
falls, xf never grows, coverage never drops.

| Task | Requirement (<=12 words) | Lane | Status | Attempts |
|---|---|---|---|---|
| REFUND-SEAM | Razorpay refund client positional call kills every refund | A | SHIPPED @ fbf4e0b (auditor SHIP cycle-1; pushed origin/lane/a; SDK signature re-derived from installed package; 1976 tests OK xf 3, cov 99.80%) | 1 |
| ASYNC-INFRA | Celery + Redis + beat schedule the three unscheduled sweeps | A | SHIPPED @ aab22db (auditor SHIP cycle-1; pushed; call_command shims — reimplementation fails CALL-side pins; no live broker needed; enqueue conversion deliberately next) | 1 |
| WEBHOOK-RECONCILE | Webhook-confirmed order never decremented stock (P1) | B | SHIPPED @ 333ddaf (auditor SHIP cycle-1 combined with WEBHOOK-COUPON; 1972 OK xf 3 both engines, cov 99.77%) | 1 |
| WEBHOOK-COUPON | Webhook never consumed coupon usage; single-use coupon burnable | B | SHIPPED @ cf3c65e (same combined audit; coupon consumption in the shared service, idempotent both directions) | 1 |
| MERCHANT-DELIST | Merchant cannot take a product off sale | B | SHIPPED @ df146f2 (auditor FIX cycle-1: P2 restock surface leaked delisted existence + mailed 404 links; RE-AUDIT cycle-2 SHIP; 2003 OK xf 3, cov 99.77%; pushed origin/lane/b) | 2 |
| ASYNC-CONVERT | Business notification call sites still send synchronously | A | PR-MERGED @ ece53b8 (PR #25; SHIP c1, pushed origin/lane/a; 3 sites route through enqueue, in-transaction outbox row, log-and-swallow isolation pinned per site; merged-tree floor 2044 OK xf 3 cov 99.77% both engines; P3: changelog files column 13 of 15) | 1 |
| DOC-CLAIMS-SCAN | Nightly full-corpus claim scan red 5+ days (245 stale-path errors) | B | PR-MERGED @ ece53b8 (PR #25; SHIP c1 + fix cycle SHIP c2 + format fix SHIP c3; explicit restructure-map RESOLVES not suppresses; 245->0 post-integration, exit 0; 3 successor entries added by fix 250ae45; ruff format red fixed by 8e0f50f; 296 unit tests; P3: pre-existing \x0c pair changes.md:682/686, map token-keyed) | 2 |
| GUEST-DEADEND | Guest checkout minted unpayable dead-end orders holding stock | A | PR-MERGED @ a669b45 (PR #26; SHIP c1; uniform 403 refusal before idempotency/cart/coupon/atomic; no order/reservation/stock/coupon/no burnt number; 14-reason byte-identity both mounts; auth e2e green; promoted-tree floor 2058 OK xf3 cov 99.77% both engines; post-integration P2 fixed by DOCLAIMS-FIX) | 1 |
| S3-MEDIA | Media storage seam unwired; no real S3/R2 object storage | B | PR-MERGED @ a669b45 (PR #26; SHIP c1; env-driven STORAGES, R2 endpoint set / AWS unset, bucket-required else ImproperlyConfigured, no-env=FileSystemStorage dev-safe; boto3+django-storages 1.14.6, lock additive-only zero drift; zero tenant literals; wt-async venv REBUILT py3.14.2; floor 2058 OK xf3 cov 99.77% both engines; no bugs) | 1 |
| DOCLAIMS-FIX | 10 deleted guest test defs cited by append-only history reddened integrated scan | A | PR-MERGED @ a669b45 (PR #26; SHIP c2; 10 restructure-map entries + 1 external-path entry for bare handlers/base.py; RESOLVES-not-suppresses; acceptance bar doc_claims --base 84c8fa7 exits 0 / 0 errors / 4383 claims at 20517a4, chain 16->1->0; same composition pattern as PR#25) | 1 |
| ENVELOPE-WIRE | SPEC-9-03 envelope never reached the wire for any DRF error | A | SHIPPED @ ae28894 (auditor SHIP cycle-1; EXCEPTION_HANDLER at the source + byte-rewriting idempotent middleware; wire pins assert res.content BYTES not .data; 401/400/403/guest-refusal all enveloped once, no double-wrap; lane A floor 2062 OK xf3 cov 99.77% 10176 stmts PG17.11; venv repaired lock-exact, no requirements touched) | 1 |
| THROTTLE-CACHE | DRF throttle cache per-process; coupon limit ~3x across 3 workers | B | SHIPPED @ 901219f (auditor SHIP cycle-1; CACHES default alias -> RedisCache when REDIS_URL declared, LocMem fallback; shared-cache pinned via 2 independent connections AND a spawned OS process spending one 30-budget, mutation-controlled, enforces 30 not 90; skip-loud when no Redis; lane B floor 2067 OK xf3 cov 99.77% 10166 stmts; Redis pins ran 0 skips) | 1 |
| MERGED-PAIR | Two lanes edited settings.py (disjoint); merged tree never measured as one | A+B | SHIPPED @ 4daa32a (integration auditor SHIP c1; merged PG floor 2071 OK xf3 cov 99.77% 10177 stmts = exact union 2058+4wire+9throttle; floor UP no regression; settings.py dual-integrity PASS EXCEPTION_HANDLER:1125 + CACHES:905-925 both intact; throttle Redis pins ran 0 skips; zero deletions, all __init__.py present; changelog both rows doc_claims exit 0) | 1 |
| P3-TRIO | Three P3s: changelog control bytes, map token-keyed, delist scope pins | A+B | SHIPPED @ 70459aa (auditor FIX c1 then orchestrator DOC-ONLY close; 4 control bytes + 9 mangled tokens restored via python byte-splice with containment proof; map cites_in scoping mutation-proven both directions, resolution set identical 280/280; delist 6 byte-pins zero production change; floor 2077 OK xf3 cov 99.77% 10235 stmts; audit BUG-1=2 missing rows closed by 70459aa) | 1 |
| FIX-TPL-COMMENT-EXTENDS | f8168a1 put {% comment %} above {% extends %} in 2 templates (Django 6.1.1 illegal, 114 errors) | A | SHIPPED @ 842c296 (auditor SHIP c1; ungated commit by ANOTHER SESSION merged via PR #34 over red checks — gate-invariant breach; repair relocates comment block below extends, text verbatim, other 4 f8168a1 templates untouched; parse probe proves violation existed+gone; floor 2077 OK xf3 cov 99.77% 10235 stmts PG17.11; changelog+inventory appended; NOT YET PROMOTED — feat/add-frontend still red from f8168a1 until RE promotes) | 1 |

## Open items (tracked, deliberately not built)

| Item | What | Owner lane | Status |
|---|---|---|---|
| PG-ONLY-CI | Remove the SQLite CI job; PostgreSQL becomes the single engine (saves builder/auditor time; SQLite is the unsound oracle) | B | PENDING — touches settings, CI workflow, pg_gate/baseline, engine-conditional tests |
| WEBHOOK-CART | Cart cleanup on webhook capture IMPOSSIBLE — Cart has no user FK, only session_id; schema gap, not a bug | A | PENDING — needs a schema decision (cart-to-order link) if wanted at all; stale line is a nuisance, not money |
| COUPON-AUDIT | Over-limit coupon redemption on webhook path is RECORDED not PREVENTED (deliberate: money captured, discount granted); audit row would make it visible | B | PENDING — cheap addition, owner call |
| STOCK-UNAVAILABLE-PIN | payment.py:492-517 StockUnavailable handler reachable but unpinned (5 missed stmts, pre-existing) | A | PENDING — worth a pin when the twin-line fixture is next touched |
| CONFIRM-INTERSTITIAL-SCOPE | DELETE on a delisted product still answers 200 {"active":false} vs 404 (P3, BUG-5) — uniformity is POST-only | B | PENDING — scope-the-claim + pin, not a behaviour change |
| CI-REDIS-SERVICE | THROTTLE-CACHE's two live-Redis pins skip loudly (named REDIS_URL) when no Redis in CI — arm them with a Redis service + REDIS_URL env in the backend workflow | B | PENDING — CI-ENGINEER NOTE from lane B; `.github/**` is release-engineer-owned, not edited by the builder |

## Remaining Tier-1 from the owner's list (not started)

| Item | Verdict from verification |
|---|---|
| S3/R2 media (SPEC-2-04) | DONE via S3-MEDIA @ cff17c5 (lane/b, SHIPPED) — env-driven STORAGES, R2 endpoint set / AWS unset, bucket-required boot guard, no-env FileSystemStorage dev-safe, boto3+django-storages 1.14.6 lock additive-only, zero tenant literals; floor 2058 OK xf3 cov 99.78% |
| Track-order page (SPEC-3-26) | FRONTEND — owner handles the frontend themselves; not this pipeline |

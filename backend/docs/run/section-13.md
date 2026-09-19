# Section 13 — Recommended frontend project structure (task ledger)

Created 2026-09-19 from the S13 compliance prefetch. Spec lines: 3634–3759. Prefetch result: 25 rows — 11 IMPLEMENTED / 7 PARTIAL / 2 MISSING / 4 DEVIATES / 0 N-A. Spec line 3738 disclaims the illustrative tree ("Route groups, server/client component boundaries and file names will depend on the framework you choose") — structural-preference deviations are grandfathered per run precedent.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-13-1 | 13 | DEVIATES [R-13.22]: display-only price arithmetic duplicated in 3 components (CartLineItem.tsx:91, CartView.tsx:79-82, CheckoutView.tsx:106,200 — `Number(item.product.price) * qty`) — extract a single helper into src/lib/money.ts and consume it from all three | 3634–3759 [R-13.22] | PENDING (P3 defer-to-touch — align when any of the three files is next modified, per run precedent) | 0 |

Owner attributions (no duplicate tasks):
- R-13.2, R-13.14 (shared UI primitives / component taxonomy) → **SPEC-2-02** (component library, Owner S15).
- R-13.12 (account/profile/addresses/wishlist/reviews surfaces) → **SPEC-3-*** feature tasks (3-15/3-16/3-17/3-19).
- Non-hermetic e2e (helpers.ts/checkout.spec.ts hit http://localhost:8000, zero route interceptions, no webServer mock layer; seeded credentials are fixtures, should move behind env when touched) → **SPEC-3-01**.

Grandfathered (no task): flat routes/no route groups (R-13.10), no features/ dirs — domain logic in lib/*-api.ts + domain component folders (R-13.13), Tailwind globals.css vs styles/ (R-13.16), missing infrastructure/ + root docs/ tree + docker-compose.yml + root README.md (R-13.7–13.9, P3 tooling/doc preferences), manual per-form validation vs shared schema package (R-13.4/13.18 — "where appropriate", deliberate and consistent).

Verified conformant (highlights): typed API client `apiFetch<T>` + ApiError + silent JWT refresh (lib/api.ts:89-140); shared domain types (lib/types.ts); env-driven config, secrets confined to NEXT_PUBLIC_*/ADMIN_URL, .env* git-ignored (R-13.21); server-first catalogue fetching with revalidate:60 + metadata/canonical/JSON-LD (R-13.19); only 23 of ~80 files "use client" (R-13.25); uniform loading/error/empty states (R-13.24); hermetic unit tests (fetch stubbed) — only e2e violates, owned above. No P1/P2 findings; nothing build-now worthy.

# Section 4 — Complete route map — what is public, gated or restricted? (task ledger)

Migrated verbatim from the monolithic ledger 2026-09-19. Spec lines: 1231–1268.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-4-01 | 4 | Regression test pinning `order_list` returns only the requester's orders (username-available rate-limit half already delivered by SPEC-1-22 `auth` scope) | 1231–1268 [4.6],[4.29],P9 | PENDING | 0 |
| SPEC-4-02 | 4 | /profile + /account/* routes MISSING from code (matrix rows) — Owner: S3/S9 via SPEC-3-15 | 1231–1268 [4.20],[4.21] | PENDING | 0 |

Note: §4 route matrix decoded by orchestrator via PowerShell slicing (line 1247, 8,856 chars, 22 rows — badge divs + plain `data-d-size="xs"` text cells). Corrections to the §4 compliance report: (1) NO "Server-only" row exists — [4.24] was a phantom from the agent's probe constraints, resolved NOT-APPLICABLE; (2) the matrix classifies `/checkout` as **"Guest or authenticated"** (not "Authenticated") — guest checkout is required by §4's own map, strengthening SPEC-1-13/SPEC-3-02 (tracked); (3) full matrix rows all map to tracked tasks: /categories//collections//search → SPEC-3-05/SPEC-3-20; /account/addresses+returns → SPEC-3-16/SPEC-3-18; /track-order "Limited public access" → SPEC-3-26; /api/webhooks/* "Verified provider only" → SPEC-1-05/SPEC-1-06 (webhook must verify provider signatures); /account/wishlist "Authenticated or local" → SPEC-3-17; /reset-password "Token-gated" → IMPLEMENTED (verified).

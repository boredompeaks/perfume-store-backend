# Section 3 — Frontend — customer-facing website (task ledger)

Migrated verbatim from the monolithic ledger 2026-09-19. Spec lines: 279–1230.

| Task ID | Section | Requirement summary | Spec lines | Status | Attempts |
|---|---|---|---|---|---|
| SPEC-3-01 | 3 | Playwright e2e requires live backend + real Razorpay widget (e2e/helpers.ts:3, checkout.spec.ts) — violates "tests must not hit the network"; make e2e hermetic via route interception/mock gateway | 279–1230 + conventions.md:32 | PENDING | 0 |
| SPEC-3-02 | 3 | Guest checkout end-to-end: anonymous order creation keyed to email with same server-total/verify guards — fulfils SPEC-1-13 | 279–1230 [3.7.2] | PENDING | 0 |
| SPEC-3-03 | 3 | Checkout UX: visible progress indicator (3.7.1) + terms acknowledgement recorded server-side (3.7.8) | 279–1230 [3.7.1],[3.7.8] | PENDING | 0 |
| SPEC-3-04 | 3 | PDP enrichment (frontend-only): policy-summary block linking /returns + /shipping (3.4.12), low-stock badge with documented default threshold (3.4.5) | 279–1230 [3.4.5],[3.4.12] | PENDING | 0 |
| SPEC-3-05 | 3 | /search route reusing listing component + recent searches in localStorage (zero-result term tracking deferred to S5) | 279–1230 [3.3.1],[3.5.5] | PENDING | 0 |
| SPEC-3-06 | 3 | Static pages /about, /faq, /maintenance + footer links (/offers deferred to S5/S6) | 279–1230 [3.9.1],[3.9.3],[3.9.11] | PENDING | 0 |
| SPEC-3-07 | 3 | Product compare end-to-end (client-side, existing /api/products) — fulfils SPEC-1-04 | 279–1230 + S1[1.3] | PENDING | 0 |
| SPEC-3-08 | 3 | Docs-ledger sweep per SPEC-1-01 auditor note: audit.md F-20/F-24 → resolved; vulnerabilities.md V-19 split fixed/open; architecture.md:21 → permission_classes | follow-up | PENDING | 0 |
| SPEC-3-09 | 3 | Announcement bar + free-shipping threshold display — Owner: S16 (config schema) cross S3 | 279–1230 [3.1.1],[3.6.9] | PENDING | 0 |
| SPEC-3-10 | 3 | Newsletter signup end-to-end (consent-aware) — Owner: S9 cross S19 | 279–1230 [3.2.8] | PENDING | 0 |
| SPEC-3-11 | 3 | Breadcrumbs beyond PDP + cookie-consent preference UI + mini-cart (S3 P3 queue, after P2 builds) | 279–1230 [3.1.4],[3.1.7],[3.1.9] | PENDING | 0 |
| SPEC-3-12 | 3 | Delivery stage: methods, estimates, serviceability, shipping/tax breakdown at cart+checkout — Owner: S9/S16 (cross SPEC-1-07) | 279–1230 [3.6.6],[3.6.8],[3.7.5],[3.7.6] | PENDING | 0 |
| SPEC-3-13 | 3 | Payment-method choice at checkout — Owner: S11 | 279–1230 [3.7.7] | PENDING | 0 |
| SPEC-3-14 | 3 | Confirmation completeness: order email/SMS + estimated delivery + tracking link — Owner: S19/S10 (cross SPEC-1-08, SPEC-1-12) | 279–1230 [3.7.10],[3.8.5] | PENDING | 0 |
| SPEC-3-15 | 3 | Account area: /account/* routes, profile view/edit, password change, session mgmt, account deletion — Owner: S9 (cross SPEC-1-14) | 279–1230 [3.8.1],[3.8.2],[3.8.11] | PENDING | 0 |
| SPEC-3-16 | 3 | Saved addresses CRUD + default selection at checkout — Owner: S9 (cross SPEC-1-14) | 279–1230 [3.7.4],[3.8.3] | PENDING | 0 |
| SPEC-3-17 | 3 | Wishlist end-to-end (header icon, page, backend model/endpoints) — Owner: S9 | 279–1230 [3.1.2],[3.8.10] | PENDING | 0 |
| SPEC-3-18 | 3 | Customer cancellation request + return/refund request + reorder + invoice download — Owner: S10/S9 | 279–1230 [3.8.6]–[3.8.9] | PENDING | 0 |
| SPEC-3-19 | 3 | Reviews/social proof + rating sort/filters — Owner: S9 (cross SPEC-1-14) | 279–1230 [3.2.7],[3.3.5],[3.3.6],[3.4.11] | PENDING | 0 |
| SPEC-3-20 | 3 | Merchandising: featured/best-selling, category resource + shortcuts, campaigns, /offers, search-term tracking, admin-editable homepage — Owner: S5/S6 cross S9 | 279–1230 [3.2.2],[3.2.3],[3.2.6],[3.2.9],[3.5.9],[3.9.9] | PENDING | 0 |
| SPEC-3-21 | 3 | Product schema gaps: multi-image gallery, variants, SKU, compare-at price, brand, structured specs — Owner: S8 cross S9 | 279–1230 [3.4.1]–[3.4.4],[3.4.10] | PENDING | 0 |
| SPEC-3-22 | 3 | Search depth: fuzzy/tolerance, ranking, suggestions, availability filter, related products — Owner: S9 (blocked by SPEC-2-05 FTS) | 279–1230 [3.4.13],[3.5.1]–[3.5.4],[3.5.6] | PENDING | 0 |
| SPEC-3-23 | 3 | Cart resilience: explicit merge/conflict rules on sign-in, unavailable-item messaging — Owner: S9/S10 | 279–1230 [3.6.11],[3.6.13] | PENDING | 0 |
| SPEC-3-24 | 3 | Contact form endpoint + submission flow — Owner: S9 cross S3 | 279–1230 [3.9.2] | PENDING | 0 |
| SPEC-3-25 | 3 | Admin-editable policies with review/publication workflow — Owner: S6 cross S16 | 279–1230 [3.9.13] | PENDING | 0 |
| SPEC-3-26 | 3 | /track-order token-based secure public tracking — Owner: S9/S10 (cross SPEC-1-08) | 279–1230 [3.9.8] | PENDING | 0 |

Note: Section 3 compliance ran concurrently with the SPEC-1-02 build (commit 854396a) and audited the pre-854396a tree; its "no throttle config anywhere" convention finding is resolved by SPEC-1-02 — auditor confirms.

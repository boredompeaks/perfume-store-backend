# Conventions

Rules for all future changes to this repo. Existing code predates some of these; align files when touching them (see fix plan Phase 0).

## Naming

- Models: singular, `CapWords` (`Product`, not `products`). `products.models.products` is legacy and will be renamed in a later phase with a migration.
- Views: verb-first (`product_list_create`, not `product_list` handling 5 methods).
- Private helpers prefixed `_` (existing pattern: `_send_email`).

## Django / DRF

- Prefer DRF class-based views (`generics.ListCreateAPIView`, `ModelViewSet`) over multi-method function views.
- Authorization via `permission_classes` (`IsAuthenticated`, `IsAdminUser`, custom object perms) — never inline `request.user.is_staff` checks.
- Money: `DecimalField(max_digits=10, decimal_places=2)` only; always `quantize` before comparing/serializing; never `float`.
- Side-effectful request flows (payments, stock, coupons) must run inside `transaction.atomic()` with `select_for_update()` on rows that gate concurrency.
- New unique/slug generation must handle `IntegrityError` retry, not check-then-act.
- Serializers: explicit `fields`, never `'__all__'`.
- Registration and password reset must run `validate_password` — the same policy everywhere.

## Security

- No secrets in code or docs. `.env` is git-ignored; `.env.example` documents keys.
- Every public mutating endpoint gets a throttle scope (see fix plan Phase 2).
- Uniform responses on anonymous flows (no existence leaks).
- New endpoints default to `IsAuthenticated`; open them deliberately.

## Testing

- Every view gets at least one unit test + one permission test before being modified.
- New features ship with tests in the same commit.
- Tests must not hit the network: Razorpay client gets faked/mocked in tests.

## Git

- Branch naming: `fix/<topic>`, `feat/<topic>`, `docs/<topic>`.
- Conventional commits: `feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`.
- `master` stays deployable; feature work on branches.
- Never commit: `.env`, `db.sqlite3`, `media/`, `venv/` (all git-ignored).

## Style

- Black-compatible formatting (88 cols), no tabs.
- No commented-out code blocks; remove dead code (e.g. `orders/views.py:280-282`).
- Comments explain *why*; the code explains *how*.

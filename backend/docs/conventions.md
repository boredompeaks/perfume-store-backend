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

## Evidence claims

This repo has an oracle for code (the suite) and, until recently, none at all
for prose. Every drift finding in the SPEC-1 run was a claim that no machine
could check: an unmeasured mutation count, a quoted test name that does not
exist, a byte count that moved because a shell write ate characters. Prose is
therefore held to the same standard as code.

- **A number is a claim until a command produced it.** If you write a figure
  into a report or into `docs/changes.md`, that figure came from a command you
  ran in this session. If you did not run it, do not write it. "Measured 11"
  with no command behind it is a fabrication, not a measurement.
- **In the changelog, the figure is printed, not typed.**
  `scripts/changelog_figures.py floor --task <id>` runs the suite with coverage
  on every engine it can resolve, saves what it measured to a per-task JSON
  artifact under `scripts/figures/`, and prints the bullet to paste;
  `mutation --task <id>` prints the same for one replayed mutation's verdict,
  failure count and test count. `check --base <sha>` then exits 1 on any
  changelog row whose floor figure or `failures=N` mutation figure has no
  artifact behind it, naming the row. It holds itself to the rules above: a
  green run's failure count is absent rather than zero, a floor measured on one
  engine is labelled `SINGLE-ENGINE`, and a figure it cannot measure is an
  error rather than a number. Two decisions about what it judges are worth
  stating here, because both were arrived at by making it red on correct prose:
  it reads an **agent-run row** as a table line whose first cell is a date, so
  the floors tables and before/after engine comparisons embedded in a section's
  prose are reported and counted rather than failed — failing them demanded
  artifacts named after table cells, which nothing can produce; and a figure that
  **no artifact can ever back** is exempted only by the committed inventory at
  `scripts/changelog_figures_baseline.json`, one entry per task carrying a
  written reason. The inventory is written only by an explicit
  `check --update-baseline`, never by the gate, and an entry whose reason does
  not clear a length floor is a hard error — so regenerating the file cannot
  turn a red gate green, and a **new** hand-typed figure is still red.
- **Never transcribe a number you were given.** When a review hands you the
  correct value, that value is a *hypothesis*, not the answer. Reproduce it or
  contradict it; either outcome is a fine report. This is not a formality — in
  SPEC-1-B07d cycle 2 a builder was handed a table of correct mutation counts
  and restated four of six without re-deriving them, and two of the five the
  brief supplied were themselves wrong.
- **A recomputed oracle is not an oracle.** A pin whose expected value is
  derived from the constant under test agrees with a wrong constant from both
  sides. Use hand-written literals.
- **Coverage measures lines executed, not states reasoned about.** A gate at
  100% can still be wrong about a value nothing drives it with. Enumerate every
  value a table admits and drive each one.
- **Count failures from the `FAILED (failures=N)` line**, never from a run
  summary: on a green run the summary reports nothing, and nothing is not zero.
- **Measure the instrument, not just the reading.** `black --quiet` prints no
  summary while still exiting 1, which is how "0 dirty everywhere" gets
  believed. Cross-check the exit code. Black's changed lines must be
  intersected with the diff's *added* lines — hunk granularity over-counts.
- **A file cannot count its own occurrences.** Any claim a document makes about
  its own contents is false the moment it writes the citation. Describe rather
  than count.
- **Docs are edited with the editor, never through a shell.** PowerShell
  `ReadAllText`/`WriteAllText`/`Set-Content`/`Out-File` and shell redirects
  have corrupted `docs/changes.md` by eating bytes invisibly. Measure bytes on
  the committed blob (`git cat-file blob`), never the worktree: `core.autocrlf`
  is on with no `.gitattributes`, so the checkout is CRLF and the commit is
  LF-only.

`scripts/doc_claims.py` enforces the mechanically decidable half of this in CI
(missing test names, missing paths, out-of-range line refs, byte integrity).
`scripts/mutation_evidence.py` replays recorded mutations so an inflated
figure cannot be published. Neither judges prose; both refuse to let a checkable
claim pass unlisted.

## Style

- Black-compatible formatting (88 cols), no tabs.
- No commented-out code blocks; remove dead code (e.g. `orders/views.py:280-282`).
- Comments explain *why*; the code explains *how*.

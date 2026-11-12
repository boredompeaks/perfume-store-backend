# ADR NNNN — short decision title

<!-- Copy this file to NNNN-short-slug.md. The template itself is not an ADR. -->

- **Status:** proposed
- **Date:** YYYY-MM-DD
- **Deciders:** who owns the call
- **Supersedes:** ADR NNNN, or "none"

## Context

The forces at play, stated as facts a reader cannot recover from the code: the
constraint, the failure mode, the measurement. Link the issue, or the section
of `docs/run/` that carries the investigation, and keep the history that
matters — not a narration of the diff.

## Decision

"We will ..." in one paragraph, active voice. Name the boundary the decision
creates: what callers may now rely on, and what they may not.

## Consequences

What becomes easier, what becomes harder, and the cost accepted. Name the
follow-up that would retire or revisit the decision, if there is one.

## Alternatives considered

One line each: what was rejected and why. "Do nothing" is an alternative and
must say what it costs.

---

## Rules for this directory

- One decision per file, `NNNN-short-slug.md`, numbered from 0001 upward;
  never renumber or reuse a number.
- An accepted ADR is immutable: supersede it with a new ADR, do not rewrite
  the decision it records. Fixing a typo or a broken link is not a rewrite.
- Keep it short. The investigation belongs in `docs/run/`, the ledger in
  `docs/changes.md`; an ADR is the decision and its reasons.
- Where two ADRs conflict, the later one wins; the earlier keeps its place in
  the tree with its status changed to "superseded by ADR NNNN".

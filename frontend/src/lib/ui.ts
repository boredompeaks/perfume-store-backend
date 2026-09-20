/**
 * Shared field styling for text inputs and selects — the single source for
 * the form-control look so accessibility fixes land everywhere at once.
 *
 * WCAG 2.2 AA (spec §15.4): the placeholder uses full-strength ink-muted
 * (≈7.6:1 on surface; an opacity modifier previously composited it to
 * ≈3.6:1, under the 4.5:1 text minimum) and the border uses line-strong
 * (≈4.9:1; the decorative `line` token is ≈1.25:1, under the 3:1 non-text
 * minimum of 1.4.11).
 *
 * Width is deliberately not part of the base class: consumers append
 * w-full / w-24 / min-w-0 flex-1 as needed, keeping conflicting width
 * utilities out of the same class list.
 */
export const inputClass =
  "border border-line-strong bg-surface px-3 py-2 text-sm placeholder:text-ink-muted focus:outline-none";

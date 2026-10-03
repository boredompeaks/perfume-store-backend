/**
 * Shared field styling for text inputs and selects — the single source for
 * the form-control look so accessibility fixes land everywhere at once.
 *
 * WCAG 2.2 AA (spec §2.1.2 / §2.1.3): the placeholder uses full-strength
 * `--color-ink-secondary` (#78716A, **4.81:1** on `--color-surface`; an
 * opacity modifier previously composited it below the 4.5:1 text minimum) and
 * the border uses `--color-field-border` (#857C72, **4.10:1** on surface). The
 * decorative `--color-hairline` (#E5E2DC) is 1.29:1 there and is therefore
 * never a control boundary — under 3:1 non-text minimum of 1.4.11, and
 * forbidden by §2.1.5 exemption E2's own boundary clause.
 *
 * Width is deliberately not part of the base class: consumers append
 * w-full / w-24 / min-w-0 flex-1 as needed, keeping conflicting width
 * utilities out of the same class list.
 */
export const inputClass =
  "border border-field-border bg-surface px-3 py-2 text-sm placeholder:text-ink-secondary focus:outline-none";

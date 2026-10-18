/**
 * White-label theme seed (U-W1-T2; palette extended U-W14-T13).
 *
 * A merchant theme is a small seed object that maps onto the EXISTING token
 * slots in `app/globals.css`. The stylesheet stays the single source of
 * truth: the seed never introduces new custom properties, never edits the
 * stylesheet, and at DEFAULT_SEED re-states today's canonical values so the
 * default storefront has a zero visual delta.
 *
 * Slot map — which --token each seed field drives:
 *
 *   seed field      token slot             canonical value in globals.css
 *   --------------  ---------------------  ----------------------------------
 *   brand           --color-bronze         #8c5338 (§2.1.1: the single
 *                                          chromatic accent)
 *   accent          --color-bronze-alt     #885035 (the accent where it needs
 *                                          more weight on white)
 *   paper           --color-paper          #fcf9f3 (the page canvas)
 *   surface         --color-surface        #ffffff (sheets, field boxes)
 *   ink             --color-ink            #1c1c18 (titles, body copy)
 *   inkSecondary    --color-ink-secondary  #78716a (notes, captions, labels)
 *   hairline        --color-hairline       #e5e2dc (structural rules —
 *                                          decorative, §2.1.5 E2)
 *   danger          --color-danger         #9e2a2b (validation/failed only)
 *   success         --color-success        #2a5a3b (stock/secure/completed)
 *   displayFont     --font-display         var(--font-playfair), Georgia,
 *                                          "Times New Roman", serif
 *   bodyFont        --font-sans            var(--font-jakarta), ui-sans-serif,
 *                                          system-ui, sans-serif
 *
 * WHY THE DERIVED COLOURS BECAME SEEDABLE (the U-W14-T13 change of doctrine).
 * Wave 1 kept the ink family, danger and success stylesheet-owned on one
 * ground only: they are contrast-audited as a set per §2.1.2/§7.3, and with
 * no runnable guard a merchant hex could silently re-derive them below the
 * AA floors with nothing to catch it. That gap is now closed — the restriction
 * was the absence of a guard, not a principle. `theme.test.ts` computes WCAG
 * 2.x ratios FROM THE SEED VALUES THEMSELVES (hex -> sRGB -> relative
 * luminance -> ratio) and fails any seed that breaks a floor:
 *
 *   - the fixed focus indicator (--color-focus #9c6a2e, §2.1.4 D1 / §7.3)
 *     keeps >= 3:1 against the seeded paper AND against the seeded ink;
 *   - ink on paper >= 4.5:1 (body text, §2.1.2);
 *   - ink-secondary on paper >= 4.5:1 (the tightest-passing text token);
 *   - danger on paper >= 4.5:1;
 *   - paper on ink >= 4.5:1 (the solid-button label).
 *
 * A merchant edit of DEFAULT_SEED is therefore checked by `npx vitest run`
 * before it can ship, and the failure message prints the measured ratio.
 *
 * What stays deliberately UN-seedable, and why: --color-focus,
 * --color-field-border and --color-hairline-hover are control boundaries
 * audited against every surface as a set (§2.1.3/§7.3) — the guard checks
 * the seeded surfaces against the fixed focus token rather than re-deriving
 * the indicator from a merchant hex. --color-ink-deep is coupled to that
 * indicator at the 3.68:1 worst case on the solid buttons; seeding it would
 * invalidate the coupling the same way.
 *
 * Font allowlist: unchanged from U-W1-T2 — no new webfonts may be loaded
 * (§2.2.5 — next/font/google resolves at BUILD time, CSP permits only
 * self-hosted faces), so the allowlist offers the two next/font stacks the
 * layout already serves plus two system stacks. An unknown key falls back to
 * the default stack rather than emitting an invalid `font-family` list.
 */

export type FontKey =
  | "playfair-display"
  | "plus-jakarta-sans"
  | "georgia"
  | "system-sans";

export interface ThemeSeed {
  /** Brand hex driving `--color-bronze`. */
  brand: string;
  /** Accent hex driving `--color-bronze-alt`. */
  accent: string;
  /** Page-canvas hex driving `--color-paper`. */
  paper: string;
  /** Card/field-plate hex driving `--color-surface`. */
  surface: string;
  /** Body/title text hex driving `--color-ink`. Guarded >= 4.5:1 on paper. */
  ink: string;
  /** Caption/label hex driving `--color-ink-secondary`. Guarded >= 4.5:1 on paper. */
  inkSecondary: string;
  /** Decorative hairline hex driving `--color-hairline` (§2.1.5 E2). */
  hairline: string;
  /** Validation/failed-state hex driving `--color-danger`. Guarded >= 4.5:1 on paper. */
  danger: string;
  /** Stock/secure/completed hex driving `--color-success`. */
  success: string;
  /** Allowlisted display face driving `--font-display`. */
  displayFont: FontKey;
  /** Allowlisted body face driving `--font-sans`. */
  bodyFont: FontKey;
}

const FONT_STACKS: Record<FontKey, string> = {
  // The two next/font stacks exactly as globals.css composes them: the
  // self-hosted variable face first (written by layout.tsx onto the <html>
  // element), then the serif/sans fallbacks §2.2 specifies.
  "playfair-display": 'var(--font-playfair), Georgia, "Times New Roman", serif',
  "plus-jakarta-sans":
    "var(--font-jakarta), ui-sans-serif, system-ui, sans-serif",
  georgia: 'Georgia, "Times New Roman", serif',
  "system-sans": "ui-sans-serif, system-ui, sans-serif",
};

/**
 * The store's own theme — every value identical to the globals.css tokens,
 * so applying it changes nothing a user can see.
 */
export const DEFAULT_SEED: ThemeSeed = {
  brand: "#8c5338",
  accent: "#885035",
  paper: "#fcf9f3",
  surface: "#ffffff",
  ink: "#1c1c18",
  inkSecondary: "#78716a",
  hairline: "#e5e2dc",
  danger: "#9e2a2b",
  success: "#2a5a3b",
  displayFont: "playfair-display",
  bodyFont: "plus-jakarta-sans",
};

/**
 * Project a seed onto the token custom properties listed in the slot map
 * above. Safe to serialize into an inline style or a <style> block: the
 * keys are plain CSS custom-property names and the values are literal CSS.
 */
export function seedToCssVars(seed: ThemeSeed): Record<string, string> {
  return {
    "--color-bronze": seed.brand,
    "--color-bronze-alt": seed.accent,
    "--color-paper": seed.paper,
    "--color-surface": seed.surface,
    "--color-ink": seed.ink,
    "--color-ink-secondary": seed.inkSecondary,
    "--color-hairline": seed.hairline,
    "--color-danger": seed.danger,
    "--color-success": seed.success,
    "--font-display":
      FONT_STACKS[seed.displayFont] ?? FONT_STACKS[DEFAULT_SEED.displayFont],
    "--font-sans":
      FONT_STACKS[seed.bodyFont] ?? FONT_STACKS[DEFAULT_SEED.bodyFont],
  };
}

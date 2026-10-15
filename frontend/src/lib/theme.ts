/**
 * White-label theme seed (U-W1-T2).
 *
 * A merchant theme is a small seed object that maps onto the EXISTING token
 * slots in `app/globals.css`. The stylesheet stays the single source of
 * truth: the seed never introduces new custom properties, never edits the
 * stylesheet, and at DEFAULT_SEED re-states today's canonical values so the
 * default storefront has a zero visual delta.
 *
 * Slot map — which --token each seed field drives:
 *
 *   seed field    token slot           canonical value in globals.css
 *   ------------  -------------------  ----------------------------------
 *   brand         --color-bronze       #8c5338 (§2.1.1: the single
 *                                      chromatic accent)
 *   accent        --color-bronze-alt   #885035 (the accent where it needs
 *                                      more weight on white)
 *   displayFont   --font-display       var(--font-playfair), Georgia,
 *                                      "Times New Roman", serif
 *   bodyFont      --font-sans          var(--font-jakarta), ui-sans-serif,
 *                                      system-ui, sans-serif
 *
 * Only these four slots are seeded in wave 1. Derived colours (focus ring,
 * danger/success, ink family) stay stylesheet-owned: they are
 * contrast-audited per §2.1.2/§7.3 and a merchant hex must not silently
 * re-derive them.
 *
 * Font allowlist: no new webfonts may be loaded (§2.2.5 — next/font/google
 * resolves at BUILD time, CSP permits only self-hosted faces), so the
 * allowlist offers the two next/font stacks the layout already serves plus
 * two system stacks. An unknown key falls back to the default stack rather
 * than emitting an invalid `font-family` list.
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
    "--font-display":
      FONT_STACKS[seed.displayFont] ?? FONT_STACKS[DEFAULT_SEED.displayFont],
    "--font-sans":
      FONT_STACKS[seed.bodyFont] ?? FONT_STACKS[DEFAULT_SEED.bodyFont],
  };
}

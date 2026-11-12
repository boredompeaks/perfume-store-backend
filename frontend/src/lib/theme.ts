/**
 * Seed mapped onto the existing token slots in `app/globals.css`, which stays
 * the source of truth (no new custom properties). `theme.test.ts` computes
 * WCAG ratios from the seed itself and fails any seed below the AA floors.
 */

export type FontKey =
  | "playfair-display"
  | "plus-jakarta-sans"
  | "georgia"
  | "system-sans";

export interface ThemeSeed {
  brand: string;
  accent: string;
  paper: string;
  surface: string;
  /** Guarded >= 4.5:1 on paper. */
  ink: string;
  /** Guarded >= 4.5:1 on paper. */
  inkSecondary: string;
  hairline: string;
  /** Guarded >= 4.5:1 on paper. */
  danger: string;
  success: string;
  displayFont: FontKey;
  bodyFont: FontKey;
}

const FONT_STACKS: Record<FontKey, string> = {
  // The next/font variables layout.tsx writes onto <html>, plus fallbacks.
  "playfair-display": 'var(--font-playfair), Georgia, "Times New Roman", serif',
  "plus-jakarta-sans":
    "var(--font-jakarta), ui-sans-serif, system-ui, sans-serif",
  georgia: 'Georgia, "Times New Roman", serif',
  "system-sans": "ui-sans-serif, system-ui, sans-serif",
};

/**
 * The store's own theme: every value matches globals.css, so applying it
 * changes nothing visible.
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
 * Safe to serialize into an inline style or `<style>` block: plain custom
 * property names with literal CSS values.
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

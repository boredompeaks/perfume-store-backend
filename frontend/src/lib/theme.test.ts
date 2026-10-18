import { describe, expect, it } from "vitest";
import { DEFAULT_SEED, seedToCssVars, type FontKey, type ThemeSeed } from "./theme";

/**
 * U-W1-T2 contract: the default seed re-states the canonical token values
 * shipped in globals.css (zero visual delta), a custom seed touches only the
 * slots its fields drive, and an unknown font key falls back to the default
 * stack rather than emitting an invalid family list.
 *
 * U-W14-T13 contract: the seed also drives the palette (paper, surface, ink,
 * ink-secondary, hairline, danger, success) and a runnable contrast guard
 * fails any seed — default or merchant-supplied — that breaks a §2.1.2/§7.3
 * floor. The guard computes WCAG ratios from the seed hexes themselves.
 *
 * Expected values are hand-written literals copied from globals.css's @theme
 * block — never derived from the module under test — so a wrong default in
 * theme.ts fails here rather than agreeing with itself.
 */
describe("theme seed maps onto token slots (U-W1-T2)", () => {
  it("DEFAULT_SEED produces the canonical current token values", () => {
    expect(seedToCssVars(DEFAULT_SEED)).toEqual({
      "--color-bronze": "#8c5338",
      "--color-bronze-alt": "#885035",
      "--color-paper": "#fcf9f3",
      "--color-surface": "#ffffff",
      "--color-ink": "#1c1c18",
      "--color-ink-secondary": "#78716a",
      "--color-hairline": "#e5e2dc",
      "--color-danger": "#9e2a2b",
      "--color-success": "#2a5a3b",
      "--font-display":
        'var(--font-playfair), Georgia, "Times New Roman", serif',
      "--font-sans": "var(--font-jakarta), ui-sans-serif, system-ui, sans-serif",
    });
  });

  it("a custom brand overrides only the brand slot", () => {
    const vars = seedToCssVars({ ...DEFAULT_SEED, brand: "#123456" });

    expect(vars["--color-bronze"]).toBe("#123456");
    // Every other slot keeps its default value.
    expect(vars["--color-bronze-alt"]).toBe("#885035");
    expect(vars["--color-paper"]).toBe("#fcf9f3");
    expect(vars["--color-ink"]).toBe("#1c1c18");
    expect(vars["--color-danger"]).toBe("#9e2a2b");
    expect(vars["--font-display"]).toBe(
      'var(--font-playfair), Georgia, "Times New Roman", serif',
    );
    expect(vars["--font-sans"]).toBe(
      "var(--font-jakarta), ui-sans-serif, system-ui, sans-serif",
    );
  });

  it("a custom accent overrides only the accent slot", () => {
    const vars = seedToCssVars({ ...DEFAULT_SEED, accent: "#0a3d2e" });

    expect(vars["--color-bronze-alt"]).toBe("#0a3d2e");
    expect(vars["--color-bronze"]).toBe("#8c5338");
  });

  it("a custom palette field overrides only its own slot", () => {
    const vars = seedToCssVars({ ...DEFAULT_SEED, danger: "#7a1f20" });

    expect(vars["--color-danger"]).toBe("#7a1f20");
    expect(vars["--color-paper"]).toBe("#fcf9f3");
    expect(vars["--color-surface"]).toBe("#ffffff");
    expect(vars["--color-ink"]).toBe("#1c1c18");
    expect(vars["--color-ink-secondary"]).toBe("#78716a");
    expect(vars["--color-hairline"]).toBe("#e5e2dc");
    expect(vars["--color-success"]).toBe("#2a5a3b");
    expect(vars["--color-bronze"]).toBe("#8c5338");
  });

  it("allowlisted non-default fonts map onto the font slots", () => {
    const vars = seedToCssVars({
      ...DEFAULT_SEED,
      displayFont: "georgia",
      bodyFont: "system-sans",
    });

    expect(vars["--font-display"]).toBe('Georgia, "Times New Roman", serif');
    expect(vars["--font-sans"]).toBe("ui-sans-serif, system-ui, sans-serif");
  });

  it("an unknown font key falls back to the default stack", () => {
    const vars = seedToCssVars({
      ...DEFAULT_SEED,
      displayFont: "comic-sans" as FontKey,
      bodyFont: "papyrus" as FontKey,
    });

    expect(vars["--font-display"]).toBe(
      'var(--font-playfair), Georgia, "Times New Roman", serif',
    );
    expect(vars["--font-sans"]).toBe(
      "var(--font-jakarta), ui-sans-serif, system-ui, sans-serif",
    );
  });
});

/* ==========================================================================
 * Contrast guard (U-W14-T13).
 *
 * The wave-1 doctrine kept derived colours stylesheet-owned because no
 * runnable guard existed — a merchant hex could silently drop the §2.1.2
 * text floors. This block IS that guard: WCAG 2.x ratios computed from the
 * seed values themselves (6-digit hex -> sRGB -> linearised relative
 * luminance -> (L1+0.05)/(L2+0.05)), so it guards ANY seed, not just the
 * defaults. A failing floor throws with the measured ratio printed.
 *
 * The focus ring is the one constant: --color-focus #9c6a2e (§2.1.4 D1 /
 * §7.3), stylesheet-owned and NOT seedable. The guard checks the seeded
 * paper and ink against this fixed ring rather than re-deriving the ring
 * from a merchant hex.
 * ========================================================================== */

const FOCUS_RING = "#9c6a2e"; // globals.css --color-focus (§2.1.4 D1 / §7.3)

function hexToRgb(hex: string): [number, number, number] {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) throw new Error(`contrast guard: not a 6-digit hex: "${hex}"`);
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function relativeLuminance(hex: string): number {
  const [r, g, b] = hexToRgb(hex).map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * Every floor the seed must clear. All failures are COLLECTED (not thrown on
 * first contact) so a merchant seed that breaks several floors reports each
 * measured ratio at once — and so no floor can hide behind an earlier one.
 */
function seedContrastFailures(seed: ThemeSeed): string[] {
  const checks: ReadonlyArray<readonly [string, number, number]> = [
    ["focus ring on paper", contrastRatio(FOCUS_RING, seed.paper), 3],
    ["focus ring on ink", contrastRatio(FOCUS_RING, seed.ink), 3],
    ["ink on paper (body text)", contrastRatio(seed.ink, seed.paper), 4.5],
    [
      "ink-secondary on paper",
      contrastRatio(seed.inkSecondary, seed.paper),
      4.5,
    ],
    ["danger on paper", contrastRatio(seed.danger, seed.paper), 4.5],
    ["paper on ink (button label)", contrastRatio(seed.paper, seed.ink), 4.5],
  ];
  const failures: string[] = [];
  for (const [label, ratio, floor] of checks) {
    if (ratio < floor) {
      failures.push(`${label}: ${ratio.toFixed(2)}:1 (floor ${floor}:1) — seed rejected`);
    }
  }
  return failures;
}

/** The guard: a seed either clears every floor or fails with its ratios printed. */
function expectSeedContrast(seed: ThemeSeed): void {
  expect(seedContrastFailures(seed)).toEqual([]);
}

describe("theme seed contrast guard (U-W14-T13)", () => {
  it("DEFAULT_SEED clears every floor (the guard agrees with the canonical palette)", () => {
    expectSeedContrast(DEFAULT_SEED);
  });

  it("rejects a seed whose ink drops body contrast below 4.5:1, printing the ratio", () => {
    // #b8b4ac on the canonical paper measures ~1.97:1 — far under AA. A
    // light ink also buries the fixed focus ring on ink, so both floors
    // report; the body-contrast line must be among them with its ratio.
    const failures = seedContrastFailures({ ...DEFAULT_SEED, ink: "#b8b4ac" });
    expect(failures.length).toBeGreaterThan(0);
    expect(failures.join("\n")).toMatch(
      /ink on paper \(body text\): [0-9.]+:1 \(floor 4\.5:1\)/,
    );
  });

  it("rejects a seed whose danger drops below 4.5:1 on paper, printing the ratio", () => {
    const failures = seedContrastFailures({ ...DEFAULT_SEED, danger: "#e3a0a0" });
    expect(failures.join("\n")).toMatch(/danger on paper: [0-9.]+:1 \(floor 4\.5:1\)/);
  });

  it("rejects a seed whose paper darkens the fixed focus ring below 3:1", () => {
    // A near-ink paper buries #9c6a2e — only the focus-floor line catches
    // this shape of merchant seed; the text floors report too, but the
    // focus line is the one that names the 3:1 control boundary.
    const failures = seedContrastFailures({ ...DEFAULT_SEED, paper: "#3d3830" });
    expect(failures.join("\n")).toMatch(/focus ring on paper: [0-9.]+:1 \(floor 3:1\)/);
  });

  it("rejects a seed whose ink-secondary drops below 4.5:1 on paper", () => {
    const failures = seedContrastFailures({
      ...DEFAULT_SEED,
      inkSecondary: "#b5b0a8",
    });
    expect(failures.join("\n")).toMatch(
      /ink-secondary on paper: [0-9.]+:1 \(floor 4\.5:1\)/,
    );
  });

  it("reports the paper-on-ink button-label floor with its own measured ratio", () => {
    // paper on ink is the same pair as ink on paper, so any seed that breaks
    // one breaks both — assert BOTH lines are present for a light-ink seed
    // so neither floor can be silently dropped from the check list.
    const failures = seedContrastFailures({ ...DEFAULT_SEED, ink: "#a39e96" });
    const joined = failures.join("\n");
    expect(joined).toMatch(/ink on paper \(body text\): [0-9.]+:1/);
    expect(joined).toMatch(/paper on ink \(button label\): [0-9.]+:1/);
  });
});

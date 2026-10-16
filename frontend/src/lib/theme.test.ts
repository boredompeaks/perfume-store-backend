import { describe, expect, it } from "vitest";
import { DEFAULT_SEED, seedToCssVars, type FontKey } from "./theme";

/**
 * U-W1-T2 contract: the default seed re-states the canonical token values
 * shipped in globals.css (zero visual delta), a custom seed touches only the
 * slots its fields drive, and an unknown font key falls back to the default
 * stack rather than emitting an invalid family list.
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

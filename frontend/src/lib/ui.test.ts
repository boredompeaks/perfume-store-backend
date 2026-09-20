import { describe, expect, it } from "vitest";
import { inputClass } from "./ui";

/**
 * Pins the WCAG 2.2 AA fix at the token level (SPEC-15-1): the placeholder
 * must be full-strength ink-muted (the old /70 modifier composited to
 * ≈3.6:1, under the 4.5:1 text minimum) and field borders must use the
 * line-strong token (the decorative `line` token is ≈1.25:1, under the 3:1
 * non-text minimum of 1.4.11). Deliberately not a whole-string snapshot:
 * unrelated layout classes may evolve, but these token classes must not.
 */
describe("inputClass", () => {
  it("uses the AA-compliant field tokens", () => {
    expect(inputClass).toContain("border-line-strong");
    expect(inputClass).toContain("placeholder:text-ink-muted");
  });

  it("never applies an opacity modifier to the placeholder", () => {
    // Any `placeholder:text-*/NN` composite lands below the 4.5:1 floor.
    expect(inputClass).not.toMatch(/placeholder:[^\s]*\/\d+/);
  });
});

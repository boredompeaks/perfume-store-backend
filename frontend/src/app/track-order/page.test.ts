import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const pageSource = readFileSync(
  fileURLToPath(new URL("./page.tsx", import.meta.url)),
  "utf8",
);
const layoutSource = readFileSync(
  fileURLToPath(new URL("./layout.tsx", import.meta.url)),
  "utf8",
);

// Behaviour pins: uniform-miss anti-oracle, credential hygiene, explicit-submit
// lookup, label association, and the single token-driven settle.
describe("/track-order lookup contract (§5.6)", () => {
  it("keeps the uniform miss: one backend message, no case branching", () => {
    expect(pageSource).toContain("err instanceof TrackingMissError");
    expect(pageSource).toContain("{state.message}");
    expect(pageSource).not.toMatch(
      /message\.(includes|toLowerCase|toUpperCase|startsWith|match)/,
    );
  });

  it("keeps the token out of React state and clears the field once read", () => {
    expect(pageSource).toContain('tokenField.value = ""');
    expect(pageSource).not.toMatch(/useState\([^)]*token/i);
    // Masked credential field; the screen's clear-token fixture must not ship.
    expect(pageSource).toMatch(
      /id="track-token"[\s\S]{0,120}type="password"/,
    );
  });

  it("looks up only on explicit submit — no effect-driven fetch", () => {
    expect(pageSource).not.toMatch(/useEffect/);
    expect(pageSource).toContain("onSubmit");
  });

  it("associates both inputs with their labels (the §7.7 form scan)", () => {
    expect(pageSource).toContain('htmlFor="track-order-number"');
    expect(pageSource).toContain('id="track-order-number"');
    expect(pageSource).toContain('htmlFor="track-token"');
    expect(pageSource).toContain('id="track-token"');
  });

  it("keeps the single enter-settle on the result panel, token-driven", () => {
    // The only allowed animation: the @starting-style settle; no keyframes.
    expect(pageSource).toContain("duration-(--motion-enter-dur)");
    expect(pageSource).toContain("ease-(--motion-ease)");
    expect(pageSource).not.toMatch(/@keyframes|animate-|animation-timeline/);
  });
});

describe("/track-order head tags (W9-T1 pins, §8.1)", () => {
  it("keeps the metadata on the server segment layout, absolute title", () => {
    expect(layoutSource).toContain('pageMetadata("/track-order"');
    expect(layoutSource).toContain("absoluteTitle: true");
  });
});

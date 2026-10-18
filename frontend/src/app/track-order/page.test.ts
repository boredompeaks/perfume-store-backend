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

/**
 * /track-order behaviour pins (U-W13-T12, §5.6) — the screen restyle may
 * change chrome, never these: the uniform-miss anti-oracle contract, the
 * credential hygiene (token never in state, field cleared once read),
 * the explicit-submit-only lookup, the W9-T1 head-tag arrangement, the
 * §7.7 label association the ui.test form scan depends on, and the
 * single token-driven settle (no new motion on a utility surface).
 */
describe("/track-order lookup contract (§5.6)", () => {
  it("keeps the uniform miss: one backend message, no case branching", () => {
    expect(pageSource).toContain("err instanceof TrackingMissError");
    expect(pageSource).toContain("{state.message}");
    // The oracle rule: the page never inspects the message to pick a UI.
    expect(pageSource).not.toMatch(
      /message\.(includes|toLowerCase|toUpperCase|startsWith|match)/,
    );
  });

  it("keeps the token out of React state and clears the field once read", () => {
    expect(pageSource).toContain('tokenField.value = ""');
    expect(pageSource).not.toMatch(/useState\([^)]*token/i);
    // Masked input: the screen shows the token in clear; a credential
    // field must not (U-W13-T12 docblock).
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
    // The one animation this page is allowed: the existing @starting-style
    // settle on the found container. No stagger, no keyframes, no scroll
    // work (§5.6 "no motion at all" on first load with no query).
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

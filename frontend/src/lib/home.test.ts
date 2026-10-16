import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const srcDir = fileURLToPath(new URL("..", import.meta.url));
const pageSource = readFileSync(join(srcDir, "app/page.tsx"), "utf8");
const globalsCss = readFileSync(join(srcDir, "app/globals.css"), "utf8");

/**
 * The home page's one accent moment (§5.1, §3 override O4, §1 P2) and the
 * two rules that keep it from becoming a defect: it may never touch the hero
 * (the LCP candidate, §10), and its base state must be the final state —
 * gated on `prefers-reduced-motion: no-preference` and on
 * `animation-timeline` support, so Firefox, crawlers and reduced-motion
 * customers all get the settled page.
 */
describe("home settle reveal (§5.1 / §3 O4 / §10)", () => {
  it("animates the three sections below the hero and never the hero", () => {
    // Exactly three consumers: new arrivals, house position, fact strip. A
    // fourth would breach P2's one-accent-moment rule and §3's six-element
    // scroll-timeline cap.
    expect(pageSource.match(/className="settle /g)).toHaveLength(3);
    const hero = pageSource.match(
      /<section className="noise[\s\S]*?<\/section>/,
    )?.[0];
    expect(hero).toBeDefined();
    // §10 / §3: nothing above the fold animates in before LCP.
    expect(hero).not.toContain("settle");
  });

  it("gates the reveal on no-preference and on animation-timeline support", () => {
    const settleAt = globalsCss.indexOf(".settle {");
    expect(settleAt).toBeGreaterThan(-1);
    const mediaAt = globalsCss.lastIndexOf(
      "@media (prefers-reduced-motion: no-preference)",
      settleAt,
    );
    const supportsAt = globalsCss.lastIndexOf(
      "@supports (animation-timeline: view())",
      settleAt,
    );
    // Both gates enclose the rule, in that order: the reduced-motion gate is
    // primary (§8.3), the @supports gate keeps an unsupported browser from
    // running a time-based fade from opacity 0 (§3, override O4).
    expect(mediaAt).toBeGreaterThan(-1);
    expect(supportsAt).toBeGreaterThan(mediaAt);
  });

  it("drives the reveal from tokens, both fill modes, no raw ms", () => {
    const rule = globalsCss.match(/\.settle\s*\{[^}]*\}/)?.[0] ?? "";
    expect(rule).toContain("animation-timeline: view()");
    expect(rule).toContain("animation-range: entry 0% entry 40%");
    expect(rule).toContain("var(--motion-enter-dur)");
    expect(rule).toContain("var(--motion-ease)");
    // `both` is mandatory: it supplies the pre-entry state without that
    // state living in the base rule (O4).
    expect(rule).toMatch(/\bboth\b/);
    // No duration ships that a token cannot name (§2.4).
    expect(rule).not.toMatch(/\d+ms/);
    // The base state is the final state: the keyframe travels 8px, and the
    // settled end of it is the element's resting position.
    const keyframes = globalsCss.match(
      /@keyframes settle-in\s*\{[\s\S]*?\n\}/,
    )?.[0];
    expect(keyframes).toContain("translateY(-8px)");
    expect(keyframes).toContain("translateY(0)");
  });

  it("sets the hero in the display step and holds the sub-paragraph to measure", () => {
    // §5.1's type contract: h1 in `--text-display`, sub-paragraph at the
    // 36ch `--measure` in `--text-body-lg`.
    expect(pageSource).toMatch(/<h1 className="[^"]*text-display/);
    expect(pageSource).toMatch(/max-w-\(--measure\) text-body-lg/);
  });
});

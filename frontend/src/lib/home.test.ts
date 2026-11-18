import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const srcDir = fileURLToPath(new URL("..", import.meta.url));
const pageSource = readFileSync(join(srcDir, "app/page.tsx"), "utf8");
const globalsCss = readFileSync(join(srcDir, "app/globals.css"), "utf8");
const carouselSource = readFileSync(
  join(srcDir, "components/home/HomeCarousel.tsx"),
  "utf8",
);

/** Comments are prose; every assertion on behaviour below reads code only. */
function stripComments(source: string): string {
  return source
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}
const carouselCode = stripComments(carouselSource);

/**
 * The one accent moment must never touch the hero (LCP), and its base state
 * must be final: gated on reduced-motion preference and animation-timeline.
 */
describe("home settle reveal (§5.1 / §3 O4 / §10)", () => {
  it("animates the three sections below the hero and never the hero", () => {
    // Exactly three consumers; a fourth would breach the one-accent rule.
    expect(pageSource.match(/className="settle /g)).toHaveLength(3);
    const hero = pageSource.match(
      /<section className="noise[\s\S]*?<\/section>/,
    )?.[0];
    expect(hero).toBeDefined();
    // Nothing above the fold animates in before LCP.
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
    // Both gates enclose the rule: reduced-motion first, then @supports so an
    // unsupported browser never runs a time-based fade from opacity 0.
    expect(mediaAt).toBeGreaterThan(-1);
    expect(supportsAt).toBeGreaterThan(mediaAt);
  });

  it("drives the reveal from tokens, both fill modes, no raw ms", () => {
    const rule = globalsCss.match(/\.settle\s*\{[^}]*\}/)?.[0] ?? "";
    expect(rule).toContain("animation-timeline: view()");
    expect(rule).toContain("animation-range: entry 0% entry 40%");
    expect(rule).toContain("var(--motion-enter-dur)");
    expect(rule).toContain("var(--motion-ease)");
    // `both` supplies the pre-entry state without putting it in the base rule.
    expect(rule).toMatch(/\bboth\b/);
    // No duration ships that a token cannot name (§2.4).
    expect(rule).not.toMatch(/\d+ms/);
    // Base state is the final state: the keyframe ends at the resting position.
    const keyframes = globalsCss.match(
      /@keyframes settle-in\s*\{[\s\S]*?\n\}/,
    )?.[0];
    expect(keyframes).toContain("translateY(-8px)");
    expect(keyframes).toContain("translateY(0)");
  });

  it("sets the hero in the display step and holds the sub-paragraph to measure", () => {
    // H1 in --text-display; sub-paragraph at 36ch --measure in body-lg.
    expect(pageSource).toMatch(/<h1 className="[^"]*text-display/);
    expect(pageSource).toMatch(/max-w-\(--measure\) text-body-lg/);
  });
});

describe("content-visibility on below-fold bands (§10.3 / F2)", () => {
  it("declares .cv-auto with the spec's pair and a print escape", () => {
    const rule = globalsCss.match(/\.cv-auto\s*\{[^}]*\}/)?.[0] ?? "";
    expect(rule).toContain("content-visibility: auto");
    // Placeholder for the scrollbar; `auto` remembers the real size later.
    expect(rule).toContain("contain-intrinsic-size: auto 600px");
    // Print must not depend on viewport proximity: skipped subtrees can drop
    // out of printed output, so the print layer restores visibility.
    const printRule = globalsCss.match(
      /@media print\s*\{\s*\.cv-auto\s*\{[^}]*\}/,
    )?.[0];
    expect(printRule).toBeDefined();
    expect(printRule).toContain("content-visibility: visible");
  });

  it("applies cv-auto to the two below-fold bands and nothing else on the page", () => {
    // House position + facts strip only; new arrivals keeps plain `settle`.
    expect(pageSource.match(/className="settle cv-auto /g)).toHaveLength(2);
    expect(pageSource.match(/className="settle mx-auto /g)).toHaveLength(1);
    // The hero is the LCP candidate: no content-visibility may reach it.
    const hero = pageSource.match(
      /<section className="noise[\s\S]*?<\/section>/,
    )?.[0];
    expect(hero).toBeDefined();
    expect(hero).not.toContain("cv-auto");
    // The carousel island ships no cv of its own either.
    expect(carouselSource).not.toContain("cv-auto");
  });
});

describe("home carousel data (§5.1 §2 / P5)", () => {
  it("fetches newest-first on the server and renders at most four", () => {
    expect(pageSource).toContain('ordering: "-created_at"');
    expect(pageSource).toContain("page.results.slice(0, 4)");
    expect(pageSource).toContain("<HomeCarousel products={products} />");
    // The island receives props: no client-side products request exists.
    expect(carouselCode).not.toMatch(/fetch\(|apiFetch|useEffect/);
  });

  it("omits the section rather than inventing slides when the catalogue is empty or unreachable", () => {
    expect(pageSource).toContain("if (products.length === 0) return null;");
    // The shared component holds the same contract on its own: no slides,
    // no carousel (never a "Slide 1 of 0" live region).
    expect(carouselCode).toContain("if (products.length === 0) return null;");
    // Both failure paths land in the same quiet omission (§5.1: the home
    // page must still render); neither renders an error dump or a stand-in.
    expect(pageSource.match(/return null;/g) ?? []).toHaveLength(2);
    expect(carouselCode).not.toMatch(/placeholder|lorem|demo product/i);
  });

  it("counts from what it was given, so a short catalogue cannot claim slides", () => {
    // The counter, the progress bars and the slide labels all read
    // `products.length` / the map index — never a hard-coded 04.
    expect(carouselCode).toContain("const total = products.length;");
    expect(carouselCode).not.toMatch(/>04</);
    expect(carouselCode).toContain("pad(total)");
    expect(carouselCode).toContain("aria-label={`${i + 1} of ${total}`}");
  });

  it("suspends into a plate-shaped skeleton, not the retired product grid", () => {
    expect(pageSource).toContain("function CarouselFallback()");
    expect(pageSource).toContain("skeleton aspect-[3/4] lg:col-span-5");
    expect(pageSource).not.toContain("GridSkeleton");
    // Fallback mirrors the slide and stays under the 12-instance shimmer cap.
    expect(pageSource.match(/className="skeleton/g) ?? []).toHaveLength(4);
  });

  it("gives the carousel no priority image — the hero keeps LCP (§10)", () => {
    expect(carouselCode).not.toContain("priority");
  });
});

describe("manual advance only (P3 / A-3)", () => {
  const timerPin =
    /\b(?:setInterval|setTimeout|requestAnimationFrame|queueMicrotask)\b/;

  it("ships no timer, no rAF, no effect that could advance the index on its own", () => {
    // Non-vacuous: fires on the exact shape a timer advance would have.
    expect(
      timerPin.test("const id = setInterval(() => setIndex((i) => i + 1), 5000);"),
    ).toBe(true);
    expect(carouselCode).not.toMatch(timerPin);
    expect(carouselCode).not.toMatch(/useEffect|useLayoutEffect/);
    expect(carouselCode).not.toMatch(/autoplay|autoAdvance|auto-rotate/i);
    // One state, one setter, and every advance routes through `goTo`.
    expect(carouselCode.match(/useState\(/g) ?? []).toHaveLength(1);
    expect(carouselCode.match(/setIndex\(/g) ?? []).toHaveLength(1);
    expect(carouselCode.match(/goTo\(/g) ?? []).toHaveLength(5);
    expect(carouselCode).toContain("onClick");
    expect(carouselCode).toContain("onKeyDown");
  });

  it("clamps instead of wrapping, and disables the control at each end", () => {
    expect(carouselCode).toContain("Math.min(Math.max(next, 0), total - 1)");
    expect(carouselCode).toContain("disabled={index === 0}");
    expect(carouselCode).toContain("disabled={index === total - 1}");
  });
});

describe("slide motion (ui-motion: track token, motion-safe, LCP)", () => {
  it("animates transform only, on the track intent token, opt-in gated", () => {
    expect(carouselSource).toContain(
      "motion-safe:transition-transform",
    );
    expect(carouselSource).toContain(
      "motion-safe:duration-(--motion-track-dur)",
    );
    expect(carouselSource).toContain(
      "motion-safe:ease-(--motion-ease-track)",
    );
    // Composited property only.
    expect(carouselCode).toMatch(/style=\{\{ transform: `translateX\(/);
    expect(carouselCode).not.toMatch(
      /transition-(?:width|height|top|left|margin)/,
    );
    // No duration a token cannot name: no raw ms or Tailwind duration.
    expect(carouselCode).not.toMatch(/\b\d+ms\b/);
    expect(carouselCode).not.toMatch(/duration-(?:\d|\[)/);
    // Colour-only state changes ride the state token (0ms under reduce).
    expect(carouselCode).not.toMatch(/transition-colors(?! duration)/);
  });

  it("reduced motion never sees the transition: motion-safe + token + backstop", () => {
    // 1. The transition only exists under no-preference; 2. the token is
    // neutralised anyway; 3. a blanket backstop covers it too.
    expect(carouselSource).toContain("motion-safe:");
    // 2. The token is neutralised anyway.
    const at = globalsCss.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(at).toBeGreaterThan(-1);
    const reduceBlock = globalsCss.slice(at);
    expect(reduceBlock).toContain("--motion-track-dur: 0ms");
    // 3. The blanket backstop covers the transition declaration.
    expect(reduceBlock).toContain("transition-duration: 0.01ms !important");
  });
});

describe("carousel ARIA (§7 / xias components.md state + keyboard rules)", () => {
  it("declares the region, the slides and one polite announcement channel", () => {
    expect(carouselCode).toContain('role="region"');
    expect(carouselCode).toContain('aria-roledescription="carousel"');
    expect(carouselCode).toContain('aria-labelledby={headingId}');
    expect(carouselCode).toContain('aria-roledescription="slide"');
    expect(carouselCode).toContain('aria-live="polite"');
    expect(carouselCode).toContain("Slide {index + 1} of {total}");
    // The visible counter duplicates the live region, so hide it from AT.
    expect(carouselCode).toMatch(/aria-hidden="true"\s*\n?\s*className="select-none/);
  });

  it("labels both controls and the progress bars, and marks the active one", () => {
    expect(carouselCode).toContain('aria-label="Previous slide"');
    expect(carouselCode).toContain('aria-label="Next slide"');
    expect(carouselCode).toContain("aria-label={`Go to slide ${i + 1}`}");
    expect(carouselCode).toContain("aria-current={i === index");
  });

  it("moves the slides with the arrow keys while focus is inside", () => {
    expect(carouselCode).toContain("onKeyDown={onKeyDown}");
    expect(carouselCode).toContain('event.key === "ArrowLeft"');
    expect(carouselCode).toContain('event.key === "ArrowRight"');
    expect(carouselCode).toContain("event.preventDefault()");
  });

  it("keeps hidden slides out of the tab order", () => {
    expect(carouselCode).toContain("aria-hidden={!active}");
    expect(carouselCode).toContain("tabIndex={active ? undefined : -1}");
    // Only one focusable child per slide, so one tabIndex is the contract.
    expect(carouselCode.match(/<Link/g) ?? []).toHaveLength(2);
  });

  it("holds the controls to the §7.5 target floor", () => {
    // 44px below lg, 32px at lg.
    expect(carouselCode).toContain("h-11 min-w-11");
    expect(carouselCode).toContain("lg:h-10 lg:min-w-10");
    // The progress bars are 24px-tall buttons around a 4px visual.
    expect(carouselCode).toContain("group flex h-6 items-center");
    expect(carouselCode).toContain("block h-1 w-10");
  });
});

describe("screen composition (D1: eyebrow, hairlines, tabular money)", () => {
  it("carries the screen's section head: eyebrow, Playfair h2, counter", () => {
    expect(carouselSource).toContain("Formulations 2024");
    expect(carouselSource).toMatch(
      /<h2 id=\{headingId\} className="mt-2 font-display text-h1">/,
    );
    expect(carouselCode).toContain('padStart(2, "0")');
    expect(carouselCode).toContain("tabular-nums");
  });

  it("stays hairline-framed with a serif, tabular price — no shadow, no radius", () => {
    expect(carouselCode).toMatch(/border border-hairline bg-paper lg:grid-cols-12/);
    expect(carouselCode).toContain(
      "font-display text-h2 tabular-nums lining-nums",
    );
    expect(carouselCode).not.toMatch(/shadow|rounded|drop-shadow/);
  });

  it("keeps the catalogue link honest and on the listing", () => {
    // The screen's "View entire archives" has no archives route.
    expect(carouselCode).toContain('href="/products?ordering=-created_at"');
    expect(carouselCode).toContain("View all");
    expect(carouselCode).not.toContain("archives");
  });

  it("ships zero em-dashes in the new UI copy", () => {
    // No em-dash in customer copy; comments are excluded.
    const jsxText = carouselSource
      .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");
    expect(jsxText).not.toContain("—");
    expect(jsxText).not.toMatch(/elevate|seamless|unlock|empower/i);
  });
});

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

/* ==========================================================================
   U-W11-T4 — the D1 screen's "New arrivals" carousel
   (`media/designs/maison_ther_catalogue_accueil_carousel_edition/`).
   Three claims the reviewer will re-read: the data is the server's real
   newest-four and nothing else (P5), the slide change is manual and
   user-intent only (P3), and reduced motion sees no transition to shorten
   (§7.6 / ui-motion's token-layer policy).
   ========================================================================== */

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
    // §6/§3: the fallback mirrors the slide and stays under the 12-instance
    // shimmer cap (four blocks here).
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
    // The pin is non-vacuous: it fires on the exact shape a timer-driven
    // advance would have.
    expect(
      timerPin.test("const id = setInterval(() => setIndex((i) => i + 1), 5000);"),
    ).toBe(true);
    expect(carouselCode).not.toMatch(timerPin);
    expect(carouselCode).not.toMatch(/useEffect|useLayoutEffect/);
    expect(carouselCode).not.toMatch(/autoplay|autoAdvance|auto-rotate/i);
    // Exactly one piece of state; the setter is called in exactly one
    // place (`goTo`), and every advance — buttons, bars, arrow keys —
    // routes through `goTo`.
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
    // Composited property only: the slide is a whole-plate translateX.
    expect(carouselCode).toMatch(/style=\{\{ transform: `translateX\(/);
    expect(carouselCode).not.toMatch(
      /transition-(?:width|height|top|left|margin)/,
    );
    // No duration ships that a token cannot name (§2.4) — no raw ms, and
    // no Tailwind literal duration either.
    expect(carouselCode).not.toMatch(/\b\d+ms\b/);
    expect(carouselCode).not.toMatch(/duration-(?:\d|\[)/);
    // Colour-only state changes ride the state token (0ms under reduce).
    expect(carouselCode).not.toMatch(/transition-colors(?! duration)/);
  });

  it("reduced motion never sees the transition: motion-safe + token + backstop", () => {
    // 1. The declaration exists only inside `prefers-reduced-motion:
    //    no-preference` (§7.6's opt-in mechanism, grep-checkable), so a
    //    reduced-motion customer gets an instant swap with zero animation.
    expect(carouselSource).toContain("motion-safe:");
    // 2. The token the transition would read is neutralised anyway.
    const at = globalsCss.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(at).toBeGreaterThan(-1);
    const reduceBlock = globalsCss.slice(at);
    expect(reduceBlock).toContain("--motion-track-dur: 0ms");
    // 3. …and the blanket backstop still covers the transition declaration.
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
    // The visible counter duplicates that fact, so it is hidden from AT.
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
    // Only one focusable child per slide, so that single tabIndex is the
    // whole tab-order contract.
    expect(carouselCode.match(/<Link/g) ?? []).toHaveLength(2); // slide CTA + "View all"
  });

  it("holds the controls to the §7.5 target floor", () => {
    // 44px below lg, 32px at lg — never the screen's 40px square.
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
    // §5.1 §2 names this link "View all"; the screen's "View entire
    // archives" wording has no archives route behind it.
    expect(carouselCode).toContain('href="/products?ordering=-created_at"');
    expect(carouselCode).toContain("View all");
    expect(carouselCode).not.toContain("archives");
  });

  it("ships zero em-dashes in the new UI copy", () => {
    // The taste contract: no em-dash in copy the customer reads. (Comments
    // are prose and excluded; the hero sentence predates this task.)
    const jsxText = carouselSource
      .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");
    expect(jsxText).not.toContain("—");
    expect(jsxText).not.toMatch(/elevate|seamless|unlock|empower/i);
  });
});

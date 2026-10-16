import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const srcDir = fileURLToPath(new URL("..", import.meta.url));
const pageSource = readFileSync(
  join(srcDir, "app/products/[slug]/page.tsx"),
  "utf8",
);
const globalsCss = readFileSync(join(srcDir, "app/globals.css"), "utf8");
const barSource = readFileSync(
  join(srcDir, "components/StickyPurchaseBar.tsx"),
  "utf8",
);
const restockSource = readFileSync(
  join(srcDir, "components/RestockForm.tsx"),
  "utf8",
);
const gridSkeletonSource = readFileSync(
  join(srcDir, "components/GridSkeleton.tsx"),
  "utf8",
);

/**
 * U-W7-T1 — the /products/[slug] redesign's load-bearing claims, pinned as
 * source contracts the way `home.test.ts` pins §5.1's. Each block below is a
 * line the wave-6 audit (or §5.2 itself) will re-read, so a future edit that
 * quietly breaks one fails here rather than reaching a reviewer's screenshot.
 */

describe("PDP image treatment (§5.2 item 2 / P4 / §10 LCP)", () => {
  it("ships one priority image with the spec's exact sizes string", () => {
    expect(pageSource).toContain(
      'sizes="(min-width: 768px) 50vw, 100vw"',
    );
    const imageBlock = pageSource.match(
      /<div className="relative aspect-\[4\/5\][\s\S]*?<\/div>/,
    )?.[0];
    expect(imageBlock).toBeDefined();
    expect(imageBlock).toContain("<ProductImage");
    expect(imageBlock).toContain("priority");
    // The LCP element never animates: no entrance, no accent, no transition
    // may touch the bottle's own wrapper (STITCH §2 row 3).
    expect(imageBlock).not.toMatch(/settle|story-drift|animate|transition/);
  });

  it("keeps the entrance off the above-fold column and the accent below it", () => {
    const entranceAt = pageSource.indexOf('className="settle ');
    expect(entranceAt).toBeGreaterThan(-1);
    // The image wrapper sits before the purchase-block entrance in source,
    // and the accent plate (§5.2 item 7, below the fold) after it.
    expect(pageSource.indexOf("aspect-[4/5]")).toBeLessThan(entranceAt);
    expect(pageSource.indexOf('className="story-drift')).toBeGreaterThan(
      entranceAt,
    );
  });
});

describe("accent moment (§5.2:1308 / STITCH §2 row 3)", () => {
  it("declares the story drift behind all three gates, in order", () => {
    const driftAt = globalsCss.indexOf(".story-drift {");
    expect(driftAt).toBeGreaterThan(-1);
    const mediaAt = globalsCss.lastIndexOf(
      "@media (prefers-reduced-motion: no-preference)",
      driftAt,
    );
    const sizeAt = globalsCss.lastIndexOf(
      "@media (min-width: 1024px)",
      driftAt,
    );
    const supportsAt = globalsCss.lastIndexOf(
      "@supports (animation-timeline: scroll())",
      driftAt,
    );
    expect(mediaAt).toBeGreaterThan(-1);
    expect(sizeAt).toBeGreaterThan(mediaAt);
    expect(supportsAt).toBeGreaterThan(sizeAt);
  });

  it("drives the drift from tokens on scroll(), 6vh total, no raw ms", () => {
    const rule = globalsCss.match(/\.story-drift\s*\{[^}]*\}/)?.[0] ?? "";
    expect(rule).toContain("animation-timeline: scroll()");
    expect(rule).toContain("var(--motion-narrative-dur)");
    expect(rule).toContain("var(--motion-ease)");
    expect(rule).toMatch(/\bboth\b/);
    expect(rule).not.toMatch(/\d+ms/);
    const keyframes = globalsCss.match(
      /@keyframes story-drift\s*\{[\s\S]*?\n\}/,
    )?.[0];
    expect(keyframes).toContain("translateY(-3vh)");
    expect(keyframes).toContain("translateY(3vh)");
  });
});

describe("restock preservation (U-W4-F3, §5.2 item 4)", () => {
  it("keeps the flag-gated replacement of the add-to-cart row", () => {
    expect(pageSource).toContain(
      "!inStock && templateConfig.featureFlags.restockNotifications",
    );
    expect(pageSource).toContain("<RestockForm slug={product.slug} />");
    // The form itself is untouched by this task — its identity markers.
    expect(restockSource).toContain("Notify me when it's back");
    expect(restockSource).toContain("restockUnsubscribe");
  });
});

describe("SEO metadata kept verbatim (§8.1)", () => {
  it("pins the title shape and the not-found fallback", () => {
    expect(pageSource).toContain(
      "`${product.name} — Eau de Parfum ${product.size} ml`",
    );
    expect(pageSource).toContain('return { title: "Product not found" };');
    expect(pageSource).toContain("https://schema.org/InStock");
    expect(pageSource).toContain("https://schema.org/OutOfStock");
  });
});

describe("designed states (§6)", () => {
  it("renders 404 through notFound() and everything else as its own panel", () => {
    expect(pageSource).toContain(
      "error instanceof ApiError && error.status === 404",
    );
    expect(pageSource).toContain("We couldn&apos;t load this fragrance.");
    // Distinct from the listing's failure copy — one product, not the grid.
    expect(pageSource).not.toContain("We couldn&apos;t load the fragrances.");
  });

  it("holds the delivery line to published numbers only (P5 / G11)", () => {
    expect(pageSource).toContain("shippingPolicy.dispatchWindow");
    expect(pageSource).toContain("getReturnWindow()");
    expect(pageSource).toMatch(/days \? ` within \$\{days\} days of delivery`/);
  });
});

describe("sticky purchase bar (§5.2:1314 / §6 NEW StickyPurchaseBar)", () => {
  it("wires to the existing cart mutation path, tokens only", () => {
    expect(barSource).toContain("<AddToCartButton");
    expect(barSource).toContain("formatINR(price)");
    expect(barSource).toContain("z-(--z-sticky)");
    expect(barSource).toContain("env(safe-area-inset-bottom)");
    expect(barSource).toContain("lg:hidden");
    expect(barSource).toContain("document.getElementById(anchorId)");
    // The anchor id lives once, in the page, and is passed as a prop.
    expect(pageSource).toContain('const PURCHASE_ANCHOR_ID = "pdp-purchase-anchor"');
    expect(pageSource).toContain("id={PURCHASE_ANCHOR_ID}");
    expect(pageSource).toContain("anchorId={PURCHASE_ANCHOR_ID}");
    expect(barSource).not.toContain("pdp-purchase-anchor");
  });

  it("exists only while the product is in stock", () => {
    expect(pageSource).toMatch(/\{inStock &&\s*\(\s*<StickyPurchaseBar/);
  });
});

describe("type and scale discipline (§2.2 / CHANGE-10)", () => {
  it("uses the named scale steps and no raw tracking or ad-hoc sizes", () => {
    expect(pageSource).toContain("font-display text-h1");
    expect(pageSource).toContain("font-display text-h2");
    expect(pageSource).toContain("text-eyebrow uppercase text-bronze");
    expect(pageSource).toContain("money tabular-nums lining-nums");
    expect(pageSource).not.toContain("tracking-[");
    expect(pageSource).not.toContain("text-4xl");
  });
});

describe("wave-6 P3 comment fixes (comment-prose, zero behaviour)", () => {
  it("the .money comment matches the guard that actually ships", () => {
    expect(globalsCss).not.toContain("the guard fails it by design");
    expect(globalsCss).toContain("NOT_A_COLOUR");
  });

  it("the no-preference declaration count in the comment equals the grep", () => {
    // A count in a comment is a claim audited like code: both sides here.
    const gated =
      globalsCss.match(
        /@media \(prefers-reduced-motion: no-preference\)/g,
      )?.length ?? 0;
    expect(gated).toBe(4);
    expect(globalsCss).toContain("Four such declarations");
  });

  it("GridSkeleton stops claiming home ships gap-y-12 verbatim", () => {
    expect(gridSkeletonSource).not.toContain("ships verbatim");
    expect(gridSkeletonSource).toContain("`gap-y-8` (32px)");
    // Behaviour unchanged by the fix: the default string still ships as-is.
    expect(gridSkeletonSource).toContain(
      'grid-cols-2 gap-x-4 gap-y-12 sm:grid-cols-3 lg:grid-cols-4',
    );
  });
});

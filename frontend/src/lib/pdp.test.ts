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
const pyramidSource = readFileSync(
  join(srcDir, "components/OlfactoryPyramid.tsx"),
  "utf8",
);
const protocolSource = readFileSync(
  join(srcDir, "components/ProtocolTrio.tsx"),
  "utf8",
);
const productsApiSource = readFileSync(
  join(srcDir, "lib/products-api.ts"),
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

/* ==========================================================================
   U-W11-T6 slice 1 — hero/purchase panel + olfactory pyramid pins.
   The screen is media/designs/ther_s_culaire_extrait_de_parfum_refined_hero_flow;
   every claim below is source-level so a silent restructure fails here.
   ======================================================================== */

/** Code-only scan: prose in comments may name retired or future tokens. */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

describe("olfactory pyramid (U-W11-T6, §5.2 item 5 / §12-G10 honesty)", () => {
  it("renders the section only when the server publishes structured notes", () => {
    expect(pageSource).toContain("toOlfactoryTiers(product.notes)");
    expect(pageSource).toContain("<OlfactoryPyramid");
    // The component's own data guard: no tiers, no section markup at all.
    expect(pyramidSource).toMatch(
      /if \(tiers\.length === 0\) return null;/,
    );
  });

  it("never typesets the free-text prose field into tiers", () => {
    // §5.2 item 5: parsing the one prose string into top/heart/base is the
    // banned three-column icon grid in different clothes.
    expect(stripComments(productsApiSource)).not.toMatch(/description/);
    expect(stripComments(pyramidSource)).not.toMatch(/description/);
    expect(pageSource).not.toMatch(/description\.(split|match|search)/);
  });

  it("toOlfactoryTiers is empty without data, one tier per populated level with it", async () => {
    const { toOlfactoryTiers } = await import("./products-api");
    expect(toOlfactoryTiers(undefined)).toEqual([]);
    expect(toOlfactoryTiers({})).toEqual([]);
    expect(toOlfactoryTiers({ top: [], heart: [], base: [] })).toEqual([]);
    const tiers = toOlfactoryTiers({
      top: [{ name: "Bitter Sicilian Neroli" }],
      base: [{ name: "Baltic Amber", detail: "Pine resin tincture" }],
    });
    expect(tiers.map((tier) => tier.key)).toEqual(["top", "base"]);
    expect(tiers[0]?.index).toBe("N° 01");
    expect(tiers[0]?.notes[0]?.name).toBe("Bitter Sicilian Neroli");
    expect(tiers[1]?.notes[0]?.detail).toBe("Pine resin tincture");
  });

  it("types the tiers from the product's optional notes field, not a guess", () => {
    const typesSource = readFileSync(join(srcDir, "lib/types.ts"), "utf8");
    expect(typesSource).toContain("notes?: ProductNotes");
    expect(typesSource).toContain("export type ProductNotes");
  });
});

describe("purchase panel (U-W11-T6 hero slice, design screen hero_flow)", () => {
  it("frames the purchase block in a hairline panel and keeps the sticky bar anchor", () => {
    expect(pageSource).toContain(
      'className="settle border border-hairline bg-surface',
    );
    expect(pageSource).toContain("lg:col-span-5");
    expect(pageSource).toContain("lg:col-span-7");
    // The §5.2:1314 anchor contract is unchanged by the framing.
    expect(pageSource).toContain(
      'const PURCHASE_ANCHOR_ID = "pdp-purchase-anchor"',
    );
    expect(pageSource).toContain("id={PURCHASE_ANCHOR_ID}");
  });

  it("renders the single real volume as the selected option, never a second SKU", () => {
    expect(pageSource).toContain('aria-label="Volume"');
    expect(pageSource).toContain("border border-ink px-4 py-3.5");
    expect(pageSource).toContain("{product.size} ml");
    // P4: one SKU per product. The design screen's 100 ml sibling has no
    // product row behind it, so it must never appear.
    expect(pageSource).not.toMatch(/100\s*ml/);
    expect(pageSource).not.toContain("€");
  });

  it("carries the category eyebrow on the full-width metadata bar", () => {
    expect(pageSource).toContain(
      'className="flex items-center gap-3 text-eyebrow uppercase text-bronze"',
    );
  });
});

/* ==========================================================================
   U-W15-T18 slice 2 — philosophy section, protocol trio, and the
   guarantee-band omission. Same screen; every claim source-level.
   ======================================================================== */

/** The philosophy <section>…</section> block, for scoped assertions. */
function philosophyBlock(): string {
  const block = pageSource.match(
    /<section\s+aria-labelledby="pdp-philosophy-heading"[\s\S]*?<\/section>/,
  )?.[0];
  expect(block).toBeDefined();
  return block ?? "";
}

describe("philosophy section (U-W15-T18, screen 'The Philosophy of the Scent')", () => {
  it("renders only when the server publishes a non-empty description", () => {
    // The fallback pin: the whole section, heading and figure included,
    // sits behind the same `hasDescription` derivation as the prose. With
    // an empty/None description the section renders nothing at all —
    // no shell, no border, no empty heading (P5; the designed fallback
    // chosen over a placeholder, cited in the page comment).
    expect(pageSource).toMatch(
      /\{hasDescription && \(\s*<section\s+aria-labelledby="pdp-philosophy-heading"/,
    );
    expect(pageSource).toContain('id="pdp-philosophy-heading"');
    expect(pageSource).toContain("The Philosophy of the Scent");
  });

  it("typesets the product's own description field as prose, exactly once", () => {
    // Prose source is the real field, repositioned here from T6's notes
    // band (the screen places it in the editorial split). It appears as
    // this section's prose and nowhere else on the page.
    expect(philosophyBlock()).toContain("{product.description}");
    expect(pageSource.match(/\{product\.description\}/g)).toHaveLength(1);
  });

  it("keeps the story-drift accent on the philosophy figure", () => {
    // The page's one accent moment (P2) moved with the prose it serves;
    // the existing accent contract in globals.css is untouched.
    expect(philosophyBlock()).toContain('className="story-drift');
  });

  it("captions the figure with real product data, never the screen's material claims", () => {
    expect(philosophyBlock()).toContain("{product.name} · {product.size} ml");
    expect(stripComments(pageSource)).not.toMatch(
      /Matières Premières|Fig\. 04|Grasse|Formulateur/,
    );
  });
});

describe("protocol trio (U-W15-T18, screen 'WEAR GUIDANCE & TECHNICAL PROTOCOLS')", () => {
  it("ships the three cards, wired after the philosophy section", () => {
    expect(pageSource).toContain("<ProtocolTrio");
    const philosophyAt = pageSource.indexOf(
      'aria-labelledby="pdp-philosophy-heading"',
    );
    const trioAt = pageSource.indexOf("<ProtocolTrio");
    expect(philosophyAt).toBeGreaterThan(-1);
    expect(trioAt).toBeGreaterThan(philosophyAt);
  });

  it("holds every body string to universal copy: no digits, no em-dash", () => {
    // The screen's product-specific numbers and places (14–18 hours,
    // four-month maceration, the Grasse atelier, 15°C–21°C) have no field
    // behind them (P5 / spec §12.D). A digit in any body string would be
    // one of them smuggling back in.
    const bodies = [...protocolSource.matchAll(/body: "([^"]+)"/g)].map(
      (m) => m[1],
    );
    expect(bodies).toHaveLength(3);
    for (const body of bodies) {
      expect(body).not.toMatch(/\d/);
      expect(body).not.toContain("—");
    }
    const titles = [...protocolSource.matchAll(/title: "([^"]+)"/g)].map(
      (m) => m[1],
    );
    expect(titles).toEqual([
      "Application",
      "Longevity & Sillage",
      "Maceration & Care",
    ]);
  });
});

describe("guarantee-band omission (U-W15-T18, P5 / spec §12.D removals)", () => {
  it("ships none of the screen's unsourced guarantee claims", () => {
    // The screen's "Garantie d'Intégrité" paragraph is entirely recorded
    // P5 removals (carbon-neutral Europe dispatch, the 2ml vial, the
    // 30-day window, wax wrapping, door-side pickup, the concierge CTA).
    // The one true pair it would have repeated is already stated once,
    // in the purchase panel's §5.2 item 6 line.
    const code = stripComments(pageSource);
    expect(code).not.toMatch(
      /carbon-neutral|Conciergerie|Garantie|miniature|wax seal/i,
    );
    expect(code).not.toMatch(/30[-\s]day|14[-\s]day/);
    expect(pageSource).toContain("shippingPolicy.dispatchWindow");
    expect(pageSource).toContain("getReturnWindow()");
  });
});

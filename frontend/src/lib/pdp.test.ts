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

/** Source contracts for the PDP redesign. */

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
    // The LCP element never animates.
    expect(imageBlock).not.toMatch(/settle|story-drift|animate|transition/);
  });

  it("keeps the entrance off the above-fold column and the accent below it", () => {
    const entranceAt = pageSource.indexOf('className="settle ');
    expect(entranceAt).toBeGreaterThan(-1);
    // Image wrapper before the entrance; below-fold accent after it.
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
    // Untouched identity markers of the existing form.
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
    // One product's failure copy, not the listing's.
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
    // The anchor id lives once in the page and is passed as a prop.
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
    // A count in a comment is audited like code: check both sides.
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
    // Behaviour unchanged: the default string still ships as-is.
    expect(gridSkeletonSource).toContain(
      'grid-cols-2 gap-x-4 gap-y-12 sm:grid-cols-3 lg:grid-cols-4',
    );
  });
});

/** Hero/purchase panel and olfactory pyramid pins. */

/** Code-only scan: prose may name retired or future tokens. */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

describe("olfactory pyramid (U-W11-T6, §5.2 item 5 / §12-G10 honesty)", () => {
  it("renders the section only when the server publishes structured notes", () => {
    expect(pageSource).toContain("toOlfactoryTiers(product.notes)");
    expect(pageSource).toContain("<OlfactoryPyramid");
    // The component's own data guard: no tiers, no section.
    expect(pyramidSource).toMatch(
      /if \(tiers\.length === 0\) return null;/,
    );
  });

  it("never typesets the free-text prose field into tiers", () => {
    // Parsing prose into top/heart/base is the banned icon grid in disguise.
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
    // The purchase-anchor contract is unchanged by the framing.
    expect(pageSource).toContain(
      'const PURCHASE_ANCHOR_ID = "pdp-purchase-anchor"',
    );
    expect(pageSource).toContain("id={PURCHASE_ANCHOR_ID}");
  });

  it("renders the single real volume as the selected option, never a second SKU", () => {
    expect(pageSource).toContain('aria-label="Volume"');
    expect(pageSource).toContain("border border-ink px-4 py-3.5");
    expect(pageSource).toContain("{product.size} ml");
    // One SKU per product: the screen's 100 ml sibling has no product row.
    expect(pageSource).not.toMatch(/100\s*ml/);
    expect(pageSource).not.toContain("€");
  });

  it("carries the category eyebrow on the full-width metadata bar", () => {
    expect(pageSource).toContain(
      'className="flex items-center gap-3 text-eyebrow uppercase text-bronze"',
    );
  });
});

/** Philosophy section, protocol trio and guarantee-band omission pins. */

/** The philosophy `<section>` block, for scoped assertions. */
function philosophyBlock(): string {
  const block = pageSource.match(
    /<section\s+aria-labelledby="pdp-philosophy-heading"[\s\S]*?<\/section>/,
  )?.[0];
  expect(block).toBeDefined();
  return block ?? "";
}

describe("philosophy section (U-W15-T18, screen 'The Philosophy of the Scent')", () => {
  it("renders only when the server publishes a non-empty description", () => {
    // The whole section sits behind `hasDescription`: with none, nothing
    // renders at all (no shell, border or empty heading).
    expect(pageSource).toMatch(
      /\{hasDescription && \(\s*<section\s+aria-labelledby="pdp-philosophy-heading"/,
    );
    expect(pageSource).toContain('id="pdp-philosophy-heading"');
    expect(pageSource).toContain("The Philosophy of the Scent");
  });

  it("typesets the product's own description field as prose, exactly once", () => {
    // The real description field appears here, and nowhere else on the page.
    expect(philosophyBlock()).toContain("{product.description}");
    expect(pageSource.match(/\{product\.description\}/g)).toHaveLength(1);
  });

  it("keeps the story-drift accent on the philosophy figure", () => {
    // The page's one accent moment moved with the prose it serves.
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
    // Product-specific numbers/places have no API field; a digit would be
    // one smuggling back in.
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
    // The screen's guarantee paragraph is entirely unsourced claims; the one
    // true pair is already stated in the purchase panel.
    const code = stripComments(pageSource);
    expect(code).not.toMatch(
      /carbon-neutral|Conciergerie|Garantie|miniature|wax seal/i,
    );
    expect(code).not.toMatch(/30[-\s]day|14[-\s]day/);
    expect(pageSource).toContain("shippingPolicy.dispatchWindow");
    expect(pageSource).toContain("getReturnWindow()");
  });
});

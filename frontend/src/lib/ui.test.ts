import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { inputClass } from "./ui";

const srcDir = fileURLToPath(new URL("..", import.meta.url));

const globalsCss = readFileSync(join(srcDir, "app/globals.css"), "utf8");

/**
 * Every `--color-*` declared in the `@theme` block, i.e. every name a Tailwind
 * colour utility can actually resolve to. A colour utility is only a string:
 * one referencing a removed token compiles to nothing at all once that token
 * is gone from `@theme`, and nothing in the build complains.
 */
const themeColours = new Set(
  [...(globalsCss.match(/@theme[^{]*\{([\s\S]*?)\n\}/)?.[1] ?? "").matchAll(
    /^\s*(--color-[a-z0-9-]+)\s*:/gm,
  )].map((match) => match[1]),
);

/**
 * The `--text-*` namespace in the same `@theme` block — §2.2's nine-step
 * type scale (`text-display`, `text-body`, `text-label`, …). Those utilities
 * share the `text-` prefix with the colour utilities, so the guard below
 * reads every font-size utility as a colour token named `display` / `body` /
 * `label` and fails on a component that is doing exactly what the type scale
 * exists to do (proved by running this file against `app/page.tsx` before the
 * exemption: seven false positives). Exempted by *name resolution against
 * @theme*, not by blocklisting the words, so a future scale step is covered
 * the moment it is declared. The `--text-…--line-height` companion keys are
 * filtered out — they are not utilities, and one of them would otherwise
 * shadow nothing but bloat the set.
 */
const themeTexts = new Set(
  [...(globalsCss.match(/@theme[^{]*\{([\s\S]*?)\n\}/)?.[1] ?? "").matchAll(
    /^\s*(--text-[a-z0-9-]+)\s*:/gm,
  )]
    .map((match) => match[1])
    // Companion keys (`--text-<step>--line-height`) carry a second `--`
    // after the namespace; the utility names themselves do not.
    .filter((name) => !name.replace(/^--/, "").includes("--")),
);

/** Prefixes that introduce a colour in Tailwind v4. */
const COLOUR_PREFIXES = new Set([
  "bg",
  "text",
  "border",
  "ring",
  "divide",
  "outline",
  "placeholder",
  "from",
  "via",
  "to",
  "decoration",
  "caret",
  "accent",
  "fill",
  "stroke",
  "shadow",
]);

/** Colours Tailwind ships itself, independent of our `@theme`. */
const BUILTIN_COLOURS = new Set([
  "inherit",
  "current",
  "transparent",
  "white",
  "black",
]);

/**
 * Suffixes that are layout or typography rather than colour, and so follow a
 * colour prefix without being one — plus `ghost`, which is a hand-written class
 * in `globals.css` (`.text-ghost`), not a theme token.
 */
const NOT_A_COLOUR = new Set([
  "", "t", "r", "b", "l", "x", "y", "s", "e",
  "solid", "dashed", "dotted", "double", "none", "hidden",
  "collapse", "separate", "spacing", "inline", "block", "reverse",
  "xs", "sm", "base", "lg", "xl", "2xl", "3xl", "4xl", "5xl", "6xl", "7xl",
  "8xl", "9xl",
  "left", "center", "right", "justify", "start", "end",
  "wrap", "nowrap", "ellipsis", "clip", "balance", "pretty",
  "uppercase", "lowercase", "capitalize",
  "normal", "tight", "tighter", "loose", "looser", "widest",
  "inset", "inner", "thin", "medium", "thick", "offset", "ghost",
  // Shadow sizes: every one of xs / sm / lg / xl / 2xl is already above, so
  // `md` was the single omission — `shadow-md` read as `--color-md`.
  "md",
  // `text-shadow`, which puts a shadow size under the `text-` prefix.
  "shadow",
  // Our own `--text-*` type steps that Tailwind also exposes as `text-*`
  // utilities: typography, never a colour. `money` is §5.3's money style
  // (`--text-money`, 0.9375rem/600) — added when the cart line total first
  // used it; pinned by the isColourCandidate case below so the entry cannot
  // be dropped while the class ships.
  "money",
  // Background and caret keywords that share a colour prefix: position, size,
  // attachment, repeat.
  "auto", "top", "bottom", "cover", "contain", "fixed", "local", "scroll",
  "repeat", "no-repeat", "repeat-x", "repeat-y", "repeat-round",
  "repeat-space",
  // `decoration-clone` / `decoration-slice` — box-decoration-break.
  "clone", "slice",
]);

/**
 * Families rather than single words, for the same reason: these follow a colour
 * prefix but belong to another Tailwind namespace. Every pattern is anchored
 * and matches nothing that could be a colour token, because no name in
 * `@theme` contains a digit or one of these keywords.
 *
 * The first entry is the one that made `ring-2` fail. Tailwind's numeric step
 * scale is shared by width, position and spacing utilities, so `ring-2`,
 * `border-x-2`, `ring-offset-2`, `border-spacing-2`, `outline-2`, `stroke-2`
 * and `from-0` all produce a bare number where a colour name would sit. Only
 * the prefixes in `COLOUR_PREFIXES` can reach this check, which is why
 * `rounded-2`, `gap-2` and the rest of the scale were never at risk.
 */
const NOT_A_COLOUR_PATTERNS = [
  /^(?:spacing-|offset-|[trblexy]-)?\d+(?:\.\d+)?$/,
  /^(?:linear|radial|conic)(?:-to-[a-z]+)?$/, // bg-linear-to-r, bg-radial
  /^(?:linecap|linejoin)-[a-z]+$/, // stroke-linecap-round
  /^rule-(?:evenodd|nonzero)$/, // fill-rule-evenodd
];

/**
 * Whether `prefix`-`name` is asking for a colour token at all. Anything this
 * returns `true` for is a colour name, so a name that resolves to nothing in
 * `@theme` is a real defect rather than a layout utility.
 */
export function isColourCandidate(prefix: string, name: string): boolean {
  return (
    COLOUR_PREFIXES.has(prefix) &&
    !BUILTIN_COLOURS.has(name) &&
    !NOT_A_COLOUR.has(name) &&
    !NOT_A_COLOUR_PATTERNS.some((pattern) => pattern.test(name))
  );
}

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sourceFiles(path);
    } else if (/\.tsx?$/.test(entry.name)) {
      yield path;
    }
  }
}

/** Colour token names referenced anywhere in `src` that `@theme` does not declare. */
function unresolvedColourTokens(): string[] {
  const missing = new Set<string>();
  for (const file of sourceFiles(srcDir)) {
    const source = readFileSync(file, "utf8");
    for (const [, prefix, name] of source.matchAll(
      /(?:[a-z-]+:)*([a-z]+)-([a-z0-9-]+)/g,
    )) {
      if (
        isColourCandidate(prefix, name) &&
        !themeColours.has(`--color-${name}`) &&
        !themeTexts.has(`--text-${name}`)
      ) {
        missing.add(name);
      }
    }
  }
  return [...missing].sort();
}

/**
 * Pins the WCAG 2.2 AA fix at the token level (§2.1.2 / §2.1.3): the
 * placeholder must be full-strength `--color-ink-secondary` (an opacity
 * modifier composited it below the 4.5:1 text minimum) and field borders must
 * use `--color-field-border` — `--color-hairline` is 1.29:1 there and is under
 * the 3:1 non-text minimum of 1.4.11. Deliberately not a whole-string
 * snapshot: unrelated layout classes may evolve, but these token classes must
 * not.
 */
describe("inputClass", () => {
  it("uses the AA-compliant field tokens", () => {
    expect(inputClass).toContain("border-field-border");
    expect(inputClass).toContain("placeholder:text-ink-secondary");
  });

  it("never applies an opacity modifier to the placeholder", () => {
    // Any `placeholder:text-*/NN` composite lands below the 4.5:1 floor.
    expect(inputClass).not.toMatch(/placeholder:[^\s]*\/\d+/);
  });
});

describe("theme tokens", () => {
  it("parses a non-empty @theme block out of globals.css", () => {
    // Without this, a regex that silently matched nothing would make the guard
    // below vacuously pass — the exact failure mode it exists to prevent.
    expect(themeColours.size).toBeGreaterThan(0);
  });

  it("parses the --text-* type scale out of the same @theme block", () => {
    // The colour guard exempts by name resolution, so this set is the load-
    // bearing half of that exemption: empty means every font-size utility
    // fails the colour check again.
    expect(themeTexts.size).toBeGreaterThan(0);
    expect(themeTexts.has("--text-display")).toBe(true);
    expect(themeTexts.has("--text-label-sm")).toBe(true);
    // Companion keys are not utilities and must not pollute the set. Built
    // by concatenation so this file's own source never reads as a utility
    // name to the colour guard above (it scans every file in src/).
    const companionKey = `--text-${"display"}--line-height`;
    expect(themeTexts.has(companionKey)).toBe(false);
  });

  it("declares every colour token that any component references", () => {
    // 105 references to removed tokens (the pre-refs muted-ink, hairline and
    // strong-hairline names) shipped with a green suite, because the old
    // assertions checked that a class-name STRING appeared in this file —
    // which was true — rather than that the class resolved to any CSS at all.
    // This asserts resolution.
    expect(unresolvedColourTokens()).toEqual([]);
  });
});

/**
 * The guard above is only useful while it is neither blind nor trigger-happy,
 * and both failure modes are silent: a colour name wrongly classified as
 * layout passes an unresolved token through, and a layout utility wrongly
 * classified as a colour fails the suite on a legitimate class. These pin both
 * directions, because the vocabulary they cover is Tailwind's, not ours, and
 * it grows without anyone editing this file.
 */
describe("isColourCandidate", () => {
  it("reads a colour-prefixed width, offset or position as a width", () => {
    for (const [prefix, name] of [
      ["ring", "2"], // the case that broke the guard
      ["ring", "offset-2"],
      ["border", "2"],
      ["border", "x-2"],
      ["border", "t-4"],
      ["outline", "2"],
      ["stroke", "2"],
      ["from", "0"],
      ["divide", "2"],
    ] as const) {
      expect([prefix, name, isColourCandidate(prefix, name)]).toEqual([
        prefix,
        name,
        false,
      ]);
    }
  });

  it("reads a same-prefix keyword from another namespace as not a colour", () => {
    for (const [prefix, name] of [
      ["shadow", "md"],
      ["text", "shadow"],
      ["text", "money"], // --text-money, §5.3 — a type step, not a colour
      ["caret", "auto"],
      ["bg", "top"],
      ["bg", "no-repeat"],
      ["bg", "linear-to-r"],
      ["bg", "radial"],
      ["stroke", "linecap-round"],
      ["fill", "rule-evenodd"],
      ["decoration", "clone"],
    ] as const) {
      expect([prefix, name, isColourCandidate(prefix, name)]).toEqual([
        prefix,
        name,
        false,
      ]);
    }
  });

  it("still treats every declared colour token as a colour token", () => {
    for (const token of themeColours) {
      const name = token.replace("--color-", "");
      expect([name, isColourCandidate("bg", name)]).toEqual([name, true]);
    }
  });

  it("still treats a misspelt colour as a colour, so it can fail", () => {
    // If this ever returns false the blocklist has swallowed the namespace and
    // `bg-bronze` typos would ship silently.
    expect(isColourCandidate("bg", "bronz")).toBe(true);
    expect(isColourCandidate("text", "hairline-hover")).toBe(true);
  });
});

/* ==========================================================================
   §7 accessibility conformance surface (U-W9-T2).

   §7.8 asks for runnable checks rather than "look at it". Everything below is
   a source-level invariant that vitest can hold: landmarks and the skip link,
   tab order (no tabindex above 0), the focus ring surviving `inputClass`'s
   `focus:outline-none`, label association on every form, the
   aria-invalid/aria-describedby pattern, distinct nav names, the menu
   overlay's trap, the cart's announcement channels, and §7.5's 44px touch
   floor on the pager and both quantity steppers. Playwright owns the
   behavioural halves (overlay wrap-around, the post-step-change focus, the
   contrast walk) — those stay UNPROVEN here until the backend is up.
   ======================================================================== */

const layoutSource = readFileSync(join(srcDir, "app/layout.tsx"), "utf8");
const headerSource = readFileSync(join(srcDir, "components/Header.tsx"), "utf8");
const overlaySource = readFileSync(
  join(srcDir, "components/MenuOverlay.tsx"),
  "utf8",
);
const checkoutSource = readFileSync(
  join(srcDir, "components/checkout/CheckoutView.tsx"),
  "utf8",
);

/**
 * Comments are prose: this file's own pins deliberately quote the retired
 * class pairs (`h-9 min-w-9`, `px-3 py-1.5`) so a reader can see what was
 * repaired, and a comment in a component may cite an old value too. Code-only
 * is what the assertions below measure — and stripping them also keeps a
 * `/* … *​/` mention of `<main>` from reading as a second landmark.
 */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

/** Every non-test source file under src, as [path under src (leading slash), code]. */
function componentSources(): [string, string][] {
  return [...sourceFiles(srcDir)]
    .filter((file) => !/\.test\.tsx?$/.test(file))
    .map((file) => {
      const normalized = file.replace(/\\/g, "/");
      const at = normalized.indexOf("/src/");
      return [
        at === -1 ? normalized : normalized.slice(at + 4),
        stripComments(readFileSync(file, "utf8")),
      ] as const;
    });
}

/**
 * Label targets compared RAW: a template literal like `addr-${f.key}` pairs
 * with the input's own `addr-${f.key}` — string with string, template with
 * template — which is exactly how AddressForm (the §7.1 pattern) wires it.
 */
function labelTargets(source: string): Set<string> {
  const targets = new Set<string>();
  for (const pattern of [
    /<label[^>]*\bhtmlFor="([^"]+)"/g,
    /<label[^>]*\bhtmlFor=\{`([^`]+)`\}/g,
  ]) {
    for (const [, value] of source.matchAll(pattern)) targets.add(value);
  }
  return targets;
}

function inputIds(source: string): string[] {
  const ids: string[] = [];
  for (const pattern of [
    /<(?:input|select|textarea)\b[^>]*\bid="([^"]+)"/g,
    /<(?:input|select|textarea)\b[^>]*\bid=\{`([^`]+)`\}/g,
  ]) {
    for (const [, value] of source.matchAll(pattern)) ids.push(value);
  }
  return ids;
}

describe("7.7 landmarks and the skip link", () => {
  it("renders exactly one <main id=\"main\">, in the root layout", () => {
    const mains = componentSources().filter(([, src]) =>
      /<main\s/.test(src),
    );
    expect(mains.map(([path]) => path)).toEqual(["/app/layout.tsx"]);
    expect(layoutSource).toContain('<main id="main"');
  });

  it("keeps the skip link first in <body>, ahead of the header", () => {
    expect(layoutSource).toContain('href="#main"');
    const skip = layoutSource.indexOf('href="#main"');
    const header = layoutSource.indexOf("<Header />");
    expect(skip).toBeGreaterThan(-1);
    expect(header).toBeGreaterThan(skip);
  });

  it("gives every <nav> a distinct accessible name", () => {
    const names = componentSources()
      .flatMap(([, src]) =>
        [...src.matchAll(/<nav[^>]*aria-label="([^"]+)"/g)].map((m) => m[1]),
      );
    // Not vacuous: the seven names §7.7 enumerates must all be present…
    for (const required of [
      "Primary",
      "Mobile",
      "Shop",
      "Help",
      "Legal",
      "Pagination",
      "Breadcrumb",
    ]) {
      expect(names).toContain(required);
    }
    // …and no two landmarks may share one.
    expect(new Set(names).size).toBe(names.length);
  });
});

describe("7.2 keyboard order and focus", () => {
  it("never puts a tabindex above 0 in the system", () => {
    const offenders = componentSources().flatMap(([path, src]) =>
      [...src.matchAll(/(?:tabIndex=\{|tabindex=")(-?\d+)/g)]
        .filter(([, value]) => Number(value) > 0)
        .map(() => path),
    );
    expect(offenders).toEqual([]);
  });

  it("moves focus to the new step heading on every checkout step change", () => {
    // The spec calls this a real, present-day gap: the panel swapped and
    // focus stayed on the button that no longer existed.
    expect(checkoutSource).toContain("tabIndex={-1}");
    expect((checkoutSource.match(/tabIndex=\{-1\}/g) ?? []).length).toBeGreaterThanOrEqual(3);
    expect(checkoutSource).toContain("stepHeading.current?.focus()");
    // The first render must not steal focus from wherever the customer was.
    expect(checkoutSource).toContain("previousStep.current === step");
  });

  it("traps, wraps and restores focus in the menu overlay (KEEP)", () => {
    expect(overlaySource).toContain("e.key === \"Escape\"");
    expect(overlaySource).toContain("focusables()[0]?.focus()");
    expect(overlaySource).toContain("previouslyFocused?.focus()");
    expect(overlaySource).toMatch(/last\.focus\(\)[\s\S]{0,240}first\.focus\(\)/);
  });
});

describe("7.3 focus visibility", () => {
  // Comments first: a prose mention of the selector must not be read as the rule.
  const cleanedGlobals = globalsCss.replace(/\/\*[\s\S]*?\*\//g, "");
  const focusRule =
    /:focus-visible\s*\{[^}]+\}/.exec(cleanedGlobals)?.[0] ?? "";

  it("draws the ring from --color-focus with a 2px outline and offset", () => {
    expect(globalsCss).toContain("--color-focus: #9c6a2e");
    expect(focusRule).toContain("outline: 2px solid var(--color-focus)");
    expect(focusRule).toContain("outline-offset: 2px");
    expect(focusRule).toContain("box-shadow: 0 0 0 4px");
  });

  it("keeps the ring rule UNLAYERED, so inputClass's focus:outline-none cannot win", () => {
    // The cascade compares layers before specificity. `inputClass` carries
    // `focus:outline-none` (specificity 0-2-0); the ring is 0-1-0. The ring
    // only survives because it is declared outside every @layer — move it
    // into one and every text field silently loses its focus indicator while
    // this suite stays green.
    expect(inputClass).toContain("focus:outline-none");
    const at = cleanedGlobals.indexOf(":focus-visible {");
    expect(at).toBeGreaterThan(-1);
    let depth = 0;
    for (const ch of cleanedGlobals.slice(0, at)) {
      if (ch === "{") depth += 1;
      else if (ch === "}") depth -= 1;
    }
    expect(depth).toBe(0);
  });
});

describe("7.7 names and labels", () => {
  const formFiles = [
    "app/track-order/page.tsx",
    "components/auth/EmailOnlyForm.tsx",
    "components/auth/LoginForm.tsx",
    "components/auth/MfaEnrollForm.tsx",
    "components/auth/RegisterForm.tsx",
    "components/auth/ResetPasswordClient.tsx",
    "components/cart/CouponForm.tsx",
    "components/checkout/AddressForm.tsx",
    "components/FilterBar.tsx",
    "components/RestockForm.tsx",
  ];

  it("associates every form control with a label (or leaves it a labelled wrap)", () => {
    for (const path of formFiles) {
      const source = readFileSync(join(srcDir, path), "utf8");
      const labels = labelTargets(source);
      const orphans = inputIds(source).filter((id) => !labels.has(id));
      expect(orphans, `${path}: input without an associated label`).toEqual([]);
      expect(labels.size, `${path}: labels with no control`).toBeGreaterThan(0);
      expect(labels.size).toBe(new Set(inputIds(source)).size);
    }
    // Checkout's delivery radios are label-WRAPPED (no htmlFor) — the one
    // association style this scan does not read, so pin it by source.
    expect(checkoutSource).toMatch(
      /<label[^>]*>\s*<input\s+type="radio"/,
    );
  });

  it("wires field errors with aria-invalid + aria-describedby (the AddressForm pattern)", () => {
    for (const path of [
      "components/checkout/AddressForm.tsx",
      "components/auth/RegisterForm.tsx",
      "components/auth/ResetPasswordClient.tsx",
    ]) {
      const source = readFileSync(join(srcDir, path), "utf8");
      expect(source, path).toContain("aria-invalid=");
      expect(source, path).toContain("aria-describedby=");
    }
  });

  it("keeps the cart badge on one announcement channel", () => {
    // aria-live on a STATIC wrapper with a changing child, plus the sr-only
    // "0 items" so the aria-label and the live region never fight.
    expect(headerSource).toContain('aria-live="polite"');
    expect(headerSource).toContain('"0 items"');
    expect(headerSource).toContain("sr-only");
  });
});

describe("7.5 target sizes", () => {
  it("holds the 44px touch floor on the pager and both quantity steppers", () => {
    for (const path of [
      "components/Pagination.tsx",
      "components/AddToCartButton.tsx",
      "components/cart/CartLineItem.tsx",
    ]) {
      const source = stripComments(readFileSync(join(srcDir, path), "utf8"));
      expect(
        (source.match(/h-11 min-w-11/g) ?? []).length,
        `${path}: §7.5 wants 44x44 controls`,
      ).toBeGreaterThanOrEqual(2);
      expect(source, path).not.toContain("h-9 min-w-9");
      expect(source, path).not.toContain("px-3 py-1.5");
    }
  });

  it("brings the header's cart, menu and account targets to the 44px floor", () => {
    // Menu button and cart link: h-11/p-3 give 44x44 hit boxes without
    // layout shift (the header's h-16 is unchanged; p-3 on the 20px cart
    // icon = 44px total width, matching the Pagination convention).
    const cleaned = stripComments(headerSource);
    expect(cleaned).toContain("flex h-11 min-w-11 items-center justify-center md:hidden");
    expect(cleaned).toContain("relative flex items-center p-3");
    // Account links (Orders / Sign out / Sign in): min-h-11 + inline-flex
    // makes them block-level 44px-tall targets without changing the header.
    expect(
      (cleaned.match(/inline-flex min-h-11 items-center/g) ?? []).length,
      "header: §7.5 wants 44px account targets",
    ).toBeGreaterThanOrEqual(3);
    expect(cleaned).not.toContain("p-2 md:hidden");
    expect(cleaned).not.toContain("p-1 transition-colors");
  });

  it("gates the account links with a breakpoint display, never hidden+inline-flex (OBS-1)", () => {
    // U-W13-T10: Tailwind v4 emits `.inline-flex` AFTER `.hidden`, so
    // `hidden inline-flex` computes to inline-flex at EVERY viewport — the
    // always-visible "Orders" link overlapped the centred wordmark at 390.
    // The display utility must ride the `sm:` variant alone; `hidden` keeps
    // a free cascade slot at the base layer.
    const cleaned = stripComments(headerSource);
    expect(cleaned).not.toContain("hidden inline-flex");
    expect(
      (cleaned.match(/hidden sm:inline-flex/g) ?? []).length,
      "header: account links are display-gated to sm (OBS-1)",
    ).toBeGreaterThanOrEqual(3);
  });
});

/**
 * U-W10-T3 — CouponForm's error is wired to the input via aria-describedby,
 * following the RegisterForm pattern (§7.1 KEEP: "Form errors linked with
 * aria-invalid + aria-describedby … apply it to every other form").
 */
describe("CouponForm aria wiring (U-W10-T3)", () => {
  const couponSource = readFileSync(
    join(srcDir, "components/cart/CouponForm.tsx"),
    "utf8",
  );

  it("describes the coupon input with its error when one is present", () => {
    expect(couponSource).toContain('aria-describedby={error ? "coupon-code-err" : undefined}');
    expect(couponSource).toContain('id="coupon-code-err"');
    expect(couponSource).toMatch(/<input[^>]*id="coupon-code"[^>]*aria-describedby=/);
  });

  it("wires the input id to its sr-only label", () => {
    expect(couponSource).toContain('htmlFor="coupon-code"');
    expect(couponSource).toContain('id="coupon-code"');
  });
});

/**
 * U-W10-T3 — /orders/[id] must have exactly one h1 (§7.7, "one thing
 * leads"). The order-number heading is promoted from h2; the DOM order
 * guarantees the h1 precedes the conditional payment-success h2.
 */
describe("orders detail heading (U-W10-T3)", () => {
  const orderDetailSource = readFileSync(
    join(srcDir, "components/orders/OrderDetailView.tsx"),
    "utf8",
  );

  it("renders exactly one <h1> — the order-number heading", () => {
    const matches = orderDetailSource.match(/<h1[\s>]/g) ?? [];
    expect(matches.length).toBe(1);
    expect(orderDetailSource).toMatch(/<h1[^>]*font-display[^>]*>\s*Order \{order\.order_number\}/);
    // No other heading may be promoted above h1.
    expect(orderDetailSource).not.toContain("<h0");
  });

  it("keeps the h1 ahead of the payment-success h2 in DOM order", () => {
    const h1At = orderDetailSource.indexOf("<h1");
    const h2At = orderDetailSource.indexOf("<h2");
    expect(h1At).toBeGreaterThan(-1);
    expect(h2At).toBeGreaterThan(h1At);
  });
});

/**
 * U-W11-T5 — /products screen-parity restyle. The screen (media/designs/
 * maison_ther_all_fragrances_refined_cards_atmosphere) drives the visuals;
 * these pins hold the behaviours the restyle must not drop: the native-select
 * ban with its ARIA replacement, the ordering still committing through the
 * URL `update` path, the card's server-money rendering on the tabular money
 * step, the hairline plate frame, and the count sentence's live region with
 * its filtered suffix. Comments are stripped first so a prose mention of a
 * selector in a docblock cannot satisfy a pin.
 */
describe("products sort control (U-W11-T5)", () => {
  const filterSource = stripComments(
    readFileSync(join(srcDir, "components/FilterBar.tsx"), "utf8"),
  );

  it("never ships a native <select> (xias AGENTS.md non-negotiable)", () => {
    expect(filterSource).not.toMatch(/<select[\s>]/);
  });

  it("replaces it with a button trigger + ARIA listbox, keyboard parity included", () => {
    expect(filterSource).toContain('aria-haspopup="listbox"');
    expect(filterSource).toContain('role="listbox"');
    expect(filterSource).toContain('role="option"');
    expect(filterSource).toContain("aria-expanded");
    expect(filterSource).toContain("Escape");
    expect(filterSource).toContain('aria-labelledby="sort-label sort-button"');
  });

  it("still writes the chosen ordering through the URL update path", () => {
    expect(filterSource).toMatch(/update\(\{\s*ordering:/);
  });
});

describe("products listing anatomy (U-W11-T5)", () => {
  const cardSource = stripComments(
    readFileSync(join(srcDir, "components/ProductCard.tsx"), "utf8"),
  );
  const pageSource = stripComments(
    readFileSync(join(srcDir, "app/products/page.tsx"), "utf8"),
  );

  it("renders the price through the server-money formatter on the tabular money step", () => {
    expect(cardSource).toContain("formatINR(product.price)");
    expect(cardSource).toContain(
      'className="money shrink-0 tabular-nums lining-nums"',
    );
  });

  it("frames the card as the screen's hairline plate with a 3:4 image", () => {
    expect(cardSource).toContain("aspect-[3/4]");
    expect(cardSource).toContain("border border-hairline");
    expect(cardSource).toContain("hover:border-hairline-hover");
  });

  it("keeps the count sentence on the live region with its filtered suffix", () => {
    expect(pageSource).toContain('aria-live="polite"');
    expect(pageSource).toContain("matching your filters");
    expect(pageSource).toContain("creations");
  });
});

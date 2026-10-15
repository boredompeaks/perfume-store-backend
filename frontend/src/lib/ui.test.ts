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

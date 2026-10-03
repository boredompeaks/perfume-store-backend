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
]);

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
        !COLOUR_PREFIXES.has(prefix) ||
        BUILTIN_COLOURS.has(name) ||
        NOT_A_COLOUR.has(name)
      ) {
        continue;
      }
      if (!themeColours.has(`--color-${name}`)) missing.add(name);
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

  it("declares every colour token that any component references", () => {
    // 105 references to removed tokens (the pre-refs muted-ink, hairline and
    // strong-hairline names) shipped with a green suite, because the old
    // assertions checked that a class-name STRING appeared in this file —
    // which was true — rather than that the class resolved to any CSS at all.
    // This asserts resolution.
    expect(unresolvedColourTokens()).toEqual([]);
  });
});
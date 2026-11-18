import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { DEFAULT_SEED, seedToCssVars, type FontKey } from "./theme";

/**
 * Proves the rebrand seams hold: identity/contact/nav live in template.config,
 * site and theme, and nowhere else. Comments are stripped before matching so
 * provenance citations are allowed; the seam files and globals.css are skipped.
 */

const srcDir = fileURLToPath(new URL("..", import.meta.url));
const globalsCss = readFileSync(join(srcDir, "app/globals.css"), "utf8");

/** Seam files: the only places the default tenant's literals are allowed. */const SEAM_FILES = new Set([
  "lib/template.config.ts",
  "lib/site.ts",
  "lib/theme.ts",
]);

/** The one sanctioned hardcode: Razorpay's modal takes a literal hex. */
const HEX_EXCEPTIONS: Record<string, readonly string[]> = {
  // The checkout iframe is outside our document and cannot read CSS vars, so
  // the brand hex must track DEFAULT_SEED.brand; asserted below.
  "components/checkout/PaymentLauncher.tsx": ["#8c5338"],
};

/** Default tenant's brand hexes; a rebrand must move them. */
const BRAND_HEXES = ["#8c5338", "#885035"];

function walkSources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...walkSources(full));
    } else if (
      (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx")) &&
      !entry.name.endsWith(".test.ts") &&
      !entry.name.endsWith(".test.tsx")
    ) {
      out.push(full);
    }
  }
  return out;
}

/**
 * Strip block and line comments before matching so provenance citations are
 * allowed while the same text in code is a violation; protect `://` so URLs
 * survive the line-comment pass.
 */
function stripComments(code: string): string {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/:\/\//g, ":\\u002F\\u002F")
    .replace(/\/\/[^\n]*/g, " ");
}

type SourceFile = { path: string; code: string };

function scanSources(): SourceFile[] {
  return walkSources(srcDir)
    .map((full) => ({
      path: relative(srcDir, full).replaceAll("\\", "/"),
      full,
    }))
    .filter(({ path }) => !SEAM_FILES.has(path))
    .map(({ path, full }) => ({
      path,
      code: stripComments(readFileSync(full, "utf8")),
    }));
}

const sources = scanSources();

describe("no brand literals outside the seams (U-W14-T14)", () => {
  // Guards against a vacuous scan.
  it("the scan is not empty and reaches the component tree", () => {
    expect(sources.length).toBeGreaterThan(40);
    expect(sources.some(({ path }) => path.startsWith("components/"))).toBe(true);
    expect(sources.some(({ path }) => path.startsWith("app/"))).toBe(true);
  });

  it("no default-tenant brand name in code or rendered copy", () => {
    const patterns = [/maison[\s._-]*aurel/i];
    const violations: string[] = [];
    for (const { path, code } of sources) {
      for (const pattern of patterns) {
        const match = code.match(pattern);
        if (match) {
          violations.push(`${path}: "${match[0]}"`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it("brand hexes appear only in the named exception", () => {
    const violations: string[] = [];
    for (const { path, code } of sources) {
      const allowed = (HEX_EXCEPTIONS[path] ?? []).map((h) => h.toLowerCase());
      for (const hex of BRAND_HEXES) {
        const count = code.toLowerCase().split(hex).length - 1;
        const allowedCount = allowed.includes(hex) ? count : 0;
        if (count > allowedCount) {
          violations.push(`${path}: ${count - allowedCount}x ${hex}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it("the named hex exception is tracked and still exact", () => {
    // The exception must stay in lockstep with DEFAULT_SEED.brand.
    const file = sources.find(
      ({ path }) => path === "components/checkout/PaymentLauncher.tsx",
    );
    expect(file, "PaymentLauncher.tsx must stay in the scan").toBeDefined();
    const allowed = HEX_EXCEPTIONS["components/checkout/PaymentLauncher.tsx"];
    expect(allowed).toEqual([DEFAULT_SEED.brand.toLowerCase()]);
    expect(file!.code.toLowerCase()).toContain(DEFAULT_SEED.brand.toLowerCase());
  });
});

describe("nav/footer/copy render from the template seams (U-W14-T14)", () => {
  // This pins that components still READ the manifest, which value tests miss.
  const required: Record<string, readonly string[]> = {
    "components/Header.tsx": ["site", "templateConfig"],
    "components/Footer.tsx": ["site", "templateConfig"],
    "components/MenuOverlay.tsx": ["site", "templateConfig"],
    "components/ContactLinks.tsx": ["site"],
    "app/layout.tsx": ["site"],
    "app/contact/page.tsx": ["site"],
    "components/legal/LegalShell.tsx": ["site"],
  };

  for (const [path, modules] of Object.entries(required)) {
    it(`${path} imports ${modules.join(" and ")}`, () => {
      const source = readFileSync(join(srcDir, path), "utf8");
      for (const name of modules) {
        const pattern =
          name === "site"
            ? /import\s*\{[^}]*\bsite\b[^}]*\}\s*from\s*["']@\/lib\/site["']/
            : /import\s*\{[^}]*\btemplateConfig\b[^}]*\}\s*from\s*["']@\/lib\/template\.config["']/;
        expect(
          source.match(pattern),
          `${path} must import ${name} from the seam — rendered copy may not hardcode it`,
        ).not.toBeNull();
      }
    });
  }
});

describe("theme seed wiring is complete (U-W14-T14)", () => {
  const FONT_KEYS: readonly FontKey[] = [
    "playfair-display",
    "plus-jakarta-sans",
    "georgia",
    "system-sans",
  ];
  const baseVars = seedToCssVars(DEFAULT_SEED);
  const seedKeys = Object.keys(DEFAULT_SEED);

  it("every seeded slot drives a CSS var (no dead seed fields)", () => {
    // Dynamic over DEFAULT_SEED's own keys: new palette fields are probed
    // automatically, with no count to update.
    for (const key of seedKeys) {
      const current = DEFAULT_SEED[key as keyof typeof DEFAULT_SEED];
      let probe: Record<string, string>;
      if (/^#[0-9a-f]{6}$/i.test(current)) {
        probe = { [key]: "#010203" };
      } else if ((FONT_KEYS as readonly string[]).includes(current)) {
        probe = { [key]: FONT_KEYS.find((f) => f !== current)! };
      } else {
        // Unknown slot: extend the probe deliberately rather than skip it.
        throw new Error(
          `seed slot "${key}" (${current}) matches no probe — extend this test with its type`,
        );
      }
      const probed = seedToCssVars({ ...DEFAULT_SEED, ...probe } as typeof DEFAULT_SEED);
      expect(probed, `seed slot "${key}" drives no CSS var`).not.toEqual(baseVars);
    }
  });

  it("every var the seed emits is a real globals.css token", () => {
    // The seed must never introduce a custom property.
    for (const name of Object.keys(baseVars)) {
      expect(
        globalsCss.includes(`--${name.slice(2)}:`),
        `${name} must be declared in globals.css`,
      ).toBe(true);
    }
  });

  it("the wave-1 slot map is present (T13 may add, never remove)", () => {
    expect(seedKeys).toEqual(expect.arrayContaining(["brand", "accent", "displayFont", "bodyFont"]));
    expect(Object.keys(baseVars)).toEqual(
      expect.arrayContaining([
        "--color-bronze",
        "--color-bronze-alt",
        "--font-display",
        "--font-sans",
      ]),
    );
  });

  it("every colour-valued seed slot is a 6-digit hex (the contrast guard's input)", () => {
    // The contrast guard reads these fields, so shorthand/named colours would
    // slip past it; pin the format of every hex-shaped slot.
    for (const key of seedKeys) {
      const value = DEFAULT_SEED[key as keyof typeof DEFAULT_SEED];
      if (/^#/.test(value)) {
        expect(value, `seed slot "${key}"`).toMatch(/^#[0-9a-f]{6}$/i);
      }
    }
  });
});

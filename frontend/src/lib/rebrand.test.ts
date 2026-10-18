import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { DEFAULT_SEED, seedToCssVars, type FontKey } from "./theme";

/**
 * Rebrand smoke (U-W14-T14, owner D3): makes the 1-hour rebrand claim real
 * WITHOUT rebranding. The 1hr runbook in `docs/TEMPLATE.md` §8 promises that
 * identity, palette, contact and nav live behind the seams —
 * `template.config.ts` / `site.ts` (identity, contact, nav/footer) and
 * `theme.ts` (palette) — and nowhere else. These tests grep the shipped tree
 * for exactly the literals a rebrand must be able to leave behind, so the
 * promise fails a gate the moment a component hardcodes the tenant again.
 *
 * Scope discipline (the exceptions are NAMED, never implicit):
 *  - Test 1/2 walk `src/` as `.ts`/`.tsx` code, strip comments (provenance
 *    citations like the ref hex in ProductCard's doc-comment are allowed by
 *    design), and skip the three seam files themselves — template.config.ts
 *    IS the default-brand literal, theme.ts IS the default-seed hexes, and
 *    site.ts derives from the manifest. `app/globals.css` is the token
 *    stylesheet (the `--color-*` declarations are the tokens, not
 *    hardcodes); it is guarded by tokens.test.ts / ui.test.ts and by the
 *    "every seeded slot is a real token" test below.
 *  - Tests 3/4 derive their expectations from DEFAULT_SEED's own keys and
 *    from globals.css, never from a hardcoded slot count, so the full-palette
 *    seed (U-W14-T13) is covered the moment those fields exist at runtime.
 */

const srcDir = fileURLToPath(new URL("..", import.meta.url));
const globalsCss = readFileSync(join(srcDir, "app/globals.css"), "utf8");

/** Seam files: the only places the default tenant's literals are allowed. */
const SEAM_FILES = new Set([
  "lib/template.config.ts",
  "lib/site.ts",
  "lib/theme.ts",
]);

/** One shipped hardcode, named: the Razorpay checkout widget takes a hex. */
const HEX_EXCEPTIONS: Record<string, readonly string[]> = {
  // PaymentLauncher configures the hosted Razorpay checkout modal, which
  // renders OUTSIDE our document and cannot read CSS custom properties, so
  // it must be handed `theme.color` as a literal. This is the one sanctioned
  // brand hex in code (documented in the TEMPLATE.md §8 runbook, step 2);
  // the value must track DEFAULT_SEED.brand — asserted below, so a rebrand
  // moves BOTH edits together and a drift names this file.
  "components/checkout/PaymentLauncher.tsx": ["#8c5338"],
};

/** The default tenant's brand hexes at this commit — the rebrand must move them. */
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
 * Comments (block and line) before matching, so provenance citations —
 * "the ref's #B8AF9F" in ProductCard, the "%s — Maison Aurel" format-string
 * notes in pages.ts / track-order/layout.tsx — are allowed while the same
 * text in code or rendered copy is a violation. `://` is protected so URL
 * string literals survive the line-comment pass.
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
  // Guards against a silently vacuous scan: the walk must actually be
  // reading the component tree the rebrand would have to clean.
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
    // If PaymentLauncher ever reads the token at build time this test asks
    // to be deleted; until then the exception must stay in lockstep with
    // DEFAULT_SEED.brand so a rebrand moves BOTH edits together.
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
  // Extends, does not duplicate, template.config.test.ts: that file pins the
  // manifest's VALUES and env precedence; this pins that the rendering
  // components still READ the manifest (a component that re-hardcodes a link
  // list or the brand would pass the value tests and fail here).
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
    // Dynamic over DEFAULT_SEED's own keys: U-W14-T13's full-palette fields
    // are probed the moment they exist at runtime, with no count to update.
    for (const key of seedKeys) {
      const current = DEFAULT_SEED[key as keyof typeof DEFAULT_SEED];
      let probe: Record<string, string>;
      if (/^#[0-9a-f]{6}$/i.test(current)) {
        probe = { [key]: "#010203" };
      } else if ((FONT_KEYS as readonly string[]).includes(current)) {
        probe = { [key]: FONT_KEYS.find((f) => f !== current)! };
      } else {
        // A slot the probe taxonomy does not know: extend the probe here
        // deliberately rather than letting the slot go unproven.
        throw new Error(
          `seed slot "${key}" (${current}) matches no probe — extend this test with its type`,
        );
      }
      const probed = seedToCssVars({ ...DEFAULT_SEED, ...probe } as typeof DEFAULT_SEED);
      expect(probed, `seed slot "${key}" drives no CSS var`).not.toEqual(baseVars);
    }
  });

  it("every var the seed emits is a real globals.css token", () => {
    // theme.ts's own contract: the seed never introduces a custom property.
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
    // U-W14-T13's contrast guard reads these fields; a shorthand or named
    // colour would slip past it, so the format is pinned here for EVERY
    // hex-shaped slot present, current and future.
    for (const key of seedKeys) {
      const value = DEFAULT_SEED[key as keyof typeof DEFAULT_SEED];
      if (/^#/.test(value)) {
        expect(value, `seed slot "${key}"`).toMatch(/^#[0-9a-f]{6}$/i);
      }
    }
  });
});

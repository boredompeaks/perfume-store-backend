import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = fileURLToPath(new URL(".", import.meta.url));
const cartSource = readFileSync(join(here, "CartView.tsx"), "utf8");

/** Strip comments so prose quoting a banned claim cannot fail the scan. */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

const cartCode = stripComments(cartSource);

/** Every storefront source file, test files excluded (they quote the ban). */
function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? walk(join(dir, entry.name)) : [join(dir, entry.name)],
  );
}

const storefrontSources = walk(join(here, "../..")).filter(
  (file) => /\.tsx?$/.test(file) && !/\.test\.tsx?$/.test(file),
);

/**
 * The old guest wording would promise a path /checkout no longer offers; only
 * the words change, not the button's place or target.
 */
describe("cart CTA copy (§5.3, D2 U-W10-T2)", () => {
  it("renames the anonymous CTA to Continue to checkout", () => {
    expect(cartSource).toContain("Continue to checkout");
  });

  it("ships the retired guest wording nowhere in the storefront source", () => {
    // The label must not survive anywhere under src/, in any component.
    const offenders = storefrontSources.filter((file) =>
      readFileSync(file, "utf8").includes("Checkout as guest"),
    );
    expect(storefrontSources.length).toBeGreaterThan(10);
    expect(offenders).toEqual([]);
  });

  it("keeps the button's target, primary-ink block and place", () => {
    // Only the text node moved; href and accent block are unchanged.
    expect(cartSource).toMatch(
      /href="\/checkout"\s+className="block bg-ink px-6 py-3 text-center text-sm text-paper[^"]*"\s*>\s*Continue to checkout/,
    );
  });

  it("keeps the sign-in escape beneath it, carrying ?next=/checkout", () => {
    expect(cartSource).toContain('href="/login?next=/checkout"');
    expect(cartSource).toContain('className="link-underline"');
  });
});

/**
 * The suffix is the real unit count (no "reserved" hold claim), and the rail
 * mirrors checkout's steps as static chrome, never faked progress.
 */
describe("cart title band and step rail (U-W12-T7)", () => {
  it("keeps the band above the state switch so every state carries the h1", () => {
    expect(cartSource).toContain("Revue du panier");
    expect(cartCode).toContain("<h1");
    // The heading renders before any loading/error/empty branch.
    expect(cartCode.indexOf("<h1")).toBeLessThan(
      cartCode.indexOf("isPending ?"),
    );
  });

  it("counts units from the server cart, singular-aware", () => {
    expect(cartCode).toContain("cartCount(cart)");
    expect(cartCode).toMatch(
      /\{unitCount\} \{unitCount === 1 \? "item" : "items"\}/,
    );
  });

  it("ships no reservation claim and no em dash in the band copy", () => {
    const band = cartCode.slice(
      cartCode.indexOf("Revue du panier"),
      cartCode.indexOf("<StepRail"),
    );
    expect(band.length).toBeGreaterThan(0);
    expect(band).not.toMatch(/reserv/i);
    expect(band).not.toContain("—");
  });

  it("mirrors the checkout's real steps as static chrome", () => {
    const rail = cartCode.slice(
      cartCode.indexOf("function StepRail"),
      cartCode.indexOf("export default"),
    );
    expect(rail).toContain('"Cart", "Delivery", "Review", "Payment"');
    expect(rail).toContain('aria-current={i === 0 ? "step" : undefined}');
    // The checkout owns the fill, and future steps are not routes.
    expect(rail).not.toContain("scaleX");
    expect(rail).not.toContain("transition");
    expect(rail).not.toContain("<a");
    expect(rail).not.toContain("href");
  });

  it("keeps the device-saved note under the guest CTA", () => {
    expect(cartCode).toContain("Your cart is saved on this device.");
  });

  it("keeps the designed empty state distinct from the error state", () => {
    // Empty and error panels must keep distinct copy and actions.
    expect(cartCode).toContain("Your cart is empty.");
    expect(cartCode).toContain("Shop fragrances");
    expect(cartCode).toContain("We couldn&apos;t load your cart.");
    expect(cartCode).toContain("Try again");
  });
});

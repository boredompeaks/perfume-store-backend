import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = fileURLToPath(new URL(".", import.meta.url));
const cartSource = readFileSync(join(here, "CartView.tsx"), "utf8");

/**
 * Comments are prose: the components cite the screen, SPEC-12-01 and the
 * retired guest wording in their comments, and a prose mention of a banned
 * claim must not read as shipped copy. Code-only is what the assertions
 * measure (same helper shape as ui.test.ts).
 */
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
 * §5.3 cart CTA under owner directive D2 (U-W10-T2): /checkout now replaces
 * an anonymous visitor with /register?next=/checkout, so the old guest
 * wording would promise an outcome the route no longer offers. The words
 * change; the button's place, its primary-ink block, its target and the
 * sign-in escape beneath it do not.
 */
describe("cart CTA copy (§5.3, D2 U-W10-T2)", () => {
  it("renames the anonymous CTA to Continue to checkout", () => {
    expect(cartSource).toContain("Continue to checkout");
  });

  it("ships the retired guest wording nowhere in the storefront source", () => {
    // "and any variant": the label must not survive in another component or
    // re-enter through a later edit anywhere under src/.
    const offenders = storefrontSources.filter((file) =>
      readFileSync(file, "utf8").includes("Checkout as guest"),
    );
    expect(storefrontSources.length).toBeGreaterThan(10); // not a vacuous walk
    expect(offenders).toEqual([]);
  });

  it("keeps the button's target, primary-ink block and place", () => {
    // Same href and the same one-accent block as before the rename — only
    // the text node moved.
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
 * U-W12-T7 — the screen's title band and step rail (design override D1,
 * media/designs/maison_ther_votre_panier). What ships is the screen's
 * treatment minus the claims the API cannot back: the suffix is the real
 * unit count (the screen's "reserved" wording stays out — stock holds begin
 * at checkout, SPEC-12-01, and the cart payload carries no hold), and the
 * rail mirrors CheckoutView's real steps as static chrome rather than a
 * faked progress state.
 */
describe("cart title band and step rail (U-W12-T7)", () => {
  it("keeps the band above the state switch so every state carries the h1", () => {
    expect(cartSource).toContain("Revue du panier");
    expect(cartCode).toContain("<h1");
    // The heading renders before any isPending / isError / empty branch.
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
    // No fake progress and no fake links: the checkout owns the fill, and
    // the future steps are states inside /checkout rather than routes.
    expect(rail).not.toContain("scaleX");
    expect(rail).not.toContain("transition");
    expect(rail).not.toContain("<a");
    expect(rail).not.toContain("href");
  });

  it("keeps the device-saved note under the guest CTA", () => {
    expect(cartCode).toContain("Your cart is saved on this device.");
  });

  it("keeps the designed empty state distinct from the error state", () => {
    // §5.3: the empty panel and the error panel are different copy with
    // different actions; the restructure around them must not collapse one
    // into the other.
    expect(cartCode).toContain("Your cart is empty.");
    expect(cartCode).toContain("Shop fragrances");
    expect(cartCode).toContain("We couldn&apos;t load your cart.");
    expect(cartCode).toContain("Try again");
  });
});

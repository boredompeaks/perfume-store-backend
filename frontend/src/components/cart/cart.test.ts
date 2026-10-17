import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = fileURLToPath(new URL(".", import.meta.url));
const cartSource = readFileSync(join(here, "CartView.tsx"), "utf8");

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

import { describe, expect, it } from "vitest";
import { site } from "./site";
import { shippingStorageKey } from "./shipping-store";

/**
 * The prefill key must derive from the configured brand, never from a
 * hardcoded tenant slug — otherwise a white-label deployment either shares
 * saved addresses across tenants or leaks the original brand name
 * ("aurel.shipping"). localStorage round-trips are out of scope here: the
 * vitest environment is node, so only the key derivation is provable.
 */
describe("shippingStorageKey", () => {
  it("slugifies a brand name: lowercased, separators collapsed to dots", () => {
    expect(shippingStorageKey("Maison Aurel")).toBe("maison.aurel.shipping");
    expect(shippingStorageKey("Lume & Co.")).toBe("lume.co.shipping");
  });

  it("trims stray whitespace and edge dots before the suffix", () => {
    expect(shippingStorageKey("  ZARA  ")).toBe("zara.shipping");
    expect(shippingStorageKey("Noir.")).toBe("noir.shipping");
  });

  it("defaults to site.name and never falls back to the retired tenant key", () => {
    expect(shippingStorageKey()).toBe(shippingStorageKey(site.name));
    expect(shippingStorageKey()).not.toBe("aurel.shipping");
  });
});

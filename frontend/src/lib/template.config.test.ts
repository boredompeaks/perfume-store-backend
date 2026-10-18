import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TemplateFeatureFlags } from "./template.config";

/**
 * White-label manifest contract (U-W1-T1): defaults are today's Maison Aurel
 * values, NEXT_PUBLIC_* env wins over them, feature flags are booleans with
 * `reviews` off, and `site` re-derives from the manifest with the exact flat
 * key set every existing import compiles against.
 */
const ENV_KEYS = [
  "NEXT_PUBLIC_SITE_NAME",
  "NEXT_PUBLIC_SITE_TAGLINE",
  "NEXT_PUBLIC_SITE_DESCRIPTION",
  "NEXT_PUBLIC_SITE_URL",
  "NEXT_PUBLIC_SUPPORT_EMAIL",
  "NEXT_PUBLIC_SUPPORT_PHONE",
  "NEXT_PUBLIC_WHATSAPP_NUMBER",
  "NEXT_PUBLIC_WHATSAPP_MESSAGE",
  "NEXT_PUBLIC_INSTAGRAM_URL",
] as const;

beforeEach(() => {
  vi.resetModules();
  // Pin every override to empty so a developer's .env.local cannot leak into
  // the default-value assertions (env() treats blank as unset).
  for (const key of ENV_KEYS) {
    vi.stubEnv(key, "");
  }
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("templateConfig defaults (Maison Aurel)", () => {
  it("brand defaults match the shipped tenant", async () => {
    const { templateConfig } = await import("./template.config");

    expect(templateConfig.brand.name).toBe("Maison Aurel");
    expect(templateConfig.brand.tagline).toBe(
      "Perfumes composed with restraint",
    );
    expect(templateConfig.brand.description).toContain("Maison Aurel");
     expect(templateConfig.brand.url).toBe("https://test.boredompeaks.me");
  });

  it("contact defaults match the shipped tenant", async () => {
    const { templateConfig } = await import("./template.config");

    expect(templateConfig.contact.supportEmail).toBe("biochem1981@gmail.com");
    expect(templateConfig.contact.supportPhone).toBe("");
    expect(templateConfig.contact.whatsappNumber).toBe("");
    expect(templateConfig.contact.whatsappMessage).toBe(
      "Hi! I have a question about a fragrance.",
    );
    expect(templateConfig.contact.instagramUrl).toBe("");
  });

  it("nav and footer links mirror the current Header/Footer", async () => {
    const { templateConfig } = await import("./template.config");

    expect(templateConfig.nav).toEqual([
      { href: "/products", label: "Shop" },
      { href: "/products?ordering=-created_at", label: "New arrivals" },
    ]);
    expect(templateConfig.footer.shop).toEqual([
      { href: "/products", label: "Shop all" },
      { href: "/products?ordering=-created_at", label: "New arrivals" },
      { href: "/cart", label: "Cart" },
      { href: "/orders", label: "Orders" },
      { href: "/login", label: "Sign in" },
    ]);
    expect(templateConfig.footer.help).toEqual([
      { href: "/contact", label: "Contact" },
      { href: "/shipping", label: "Shipping" },
      { href: "/returns", label: "Returns & Refunds" },
    ]);
    expect(templateConfig.footer.legal).toEqual([
      { href: "/privacy", label: "Privacy Policy" },
      { href: "/terms", label: "Terms & Conditions" },
    ]);
  });

  it("feature flags default to on except reviews", async () => {
    const { templateConfig } = await import("./template.config");

    expect(templateConfig.featureFlags).toEqual({
      guestCheckout: true,
      restockNotifications: true,
      orderTracking: true,
      returnsRequests: true,
      reviews: false,
    });
  });

  it("feature flags stay boolean-typed in the manifest", async () => {
    const { templateConfig } = await import("./template.config");

    // Compile-time guard (enforced by `next build`/tsc): a non-boolean flag
    // would fail the annotation below even if this test never ran.
    const flags: TemplateFeatureFlags = templateConfig.featureFlags;
    expect(typeof flags.guestCheckout).toBe("boolean");
    expect(typeof flags.reviews).toBe("boolean");
  });
});

/**
 * Manifest → component contract (U-W3-T5): Header, MenuOverlay and Footer
 * render these lists, so a shape regression (empty group, missing label or
 * href) breaks rendered navigation, not just a type. The mobile menu derives
 * from `footer.shop`, so it is covered by the same assertions.
 */
describe("nav/footer lists drive component rendering", () => {
  const groups = (config: {
    nav: readonly { href: string; label: string }[];
    footer: Record<
      "shop" | "help" | "legal",
      readonly { href: string; label: string }[]
    >;
  }) => [
    ["nav", config.nav],
    ["footer.shop", config.footer.shop],
    ["footer.help", config.footer.help],
    ["footer.legal", config.footer.legal],
  ] as const;

  it("every nav/footer group is non-empty", async () => {
    const { templateConfig } = await import("./template.config");

    for (const [name, entries] of groups(templateConfig)) {
      expect(entries.length, `${name} must not be empty`).toBeGreaterThan(0);
    }
  });

  it("every nav/footer entry has a non-empty label and href", async () => {
    const { templateConfig } = await import("./template.config");

    for (const [name, entries] of groups(templateConfig)) {
      for (const entry of entries) {
        expect(entry.label.trim(), `${name} entry label`).not.toBe("");
        expect(entry.href.trim(), `${name} entry href`).not.toBe("");
      }
    }
  });

  it("guestCheckout defaults to true (today's guest CTA copy)", async () => {
    const { templateConfig } = await import("./template.config");

    expect(templateConfig.featureFlags.guestCheckout).toBe(true);
  });
});

describe("NEXT_PUBLIC_* overrides", () => {
  it("brand env overrides win over defaults", async () => {
    vi.stubEnv("NEXT_PUBLIC_SITE_NAME", "Atelier Noir");
    vi.stubEnv("NEXT_PUBLIC_SITE_TAGLINE", "Composed in small batches");
    vi.stubEnv("NEXT_PUBLIC_SITE_DESCRIPTION", "A different house.");
    vi.stubEnv("NEXT_PUBLIC_SITE_URL", "https://ateliernoir.example");
    const { templateConfig } = await import("./template.config");

    expect(templateConfig.brand).toEqual({
      name: "Atelier Noir",
      tagline: "Composed in small batches",
      description: "A different house.",
      url: "https://ateliernoir.example",
    });
  });

  it("contact env overrides win over defaults", async () => {
    vi.stubEnv("NEXT_PUBLIC_SUPPORT_EMAIL", "help@ateliernoir.example");
    vi.stubEnv("NEXT_PUBLIC_SUPPORT_PHONE", "+1 555 0100");
    vi.stubEnv("NEXT_PUBLIC_WHATSAPP_NUMBER", "15550100999");
    vi.stubEnv("NEXT_PUBLIC_WHATSAPP_MESSAGE", "Bonjour!");
    vi.stubEnv("NEXT_PUBLIC_INSTAGRAM_URL", "https://instagram.com/ateliernoir");
    const { templateConfig } = await import("./template.config");

    expect(templateConfig.contact).toEqual({
      supportEmail: "help@ateliernoir.example",
      supportPhone: "+1 555 0100",
      whatsappNumber: "15550100999",
      whatsappMessage: "Bonjour!",
      instagramUrl: "https://instagram.com/ateliernoir",
    });
  });

  it("blank or whitespace-only env falls back to the default", async () => {
    vi.stubEnv("NEXT_PUBLIC_SITE_NAME", "   ");
    vi.stubEnv("NEXT_PUBLIC_SUPPORT_EMAIL", "");
    const { templateConfig } = await import("./template.config");

    expect(templateConfig.brand.name).toBe("Maison Aurel");
    expect(templateConfig.contact.supportEmail).toBe("biochem1981@gmail.com");
  });
});

describe("site re-derives from templateConfig", () => {
  it("keeps the flat export key set call sites compile against", async () => {
    const { site } = await import("./site");

    expect(Object.keys(site).sort()).toEqual([
      "description",
      "instagramUrl",
      "name",
      "supportEmail",
      "supportPhone",
      "tagline",
      "url",
      "whatsappMessage",
      "whatsappNumber",
    ]);
  });

  it("mirrors the manifest defaults", async () => {
    const { site } = await import("./site");
    const { templateConfig } = await import("./template.config");

    expect(site).toEqual({
      name: templateConfig.brand.name,
      tagline: templateConfig.brand.tagline,
      description: templateConfig.brand.description,
      url: templateConfig.brand.url,
      supportEmail: templateConfig.contact.supportEmail,
      supportPhone: templateConfig.contact.supportPhone,
      whatsappNumber: templateConfig.contact.whatsappNumber,
      whatsappMessage: templateConfig.contact.whatsappMessage,
      instagramUrl: templateConfig.contact.instagramUrl,
    });
  });

  it("mirrors the manifest when env overrides win", async () => {
    vi.stubEnv("NEXT_PUBLIC_SITE_NAME", "Atelier Noir");
    vi.stubEnv("NEXT_PUBLIC_SUPPORT_PHONE", "+1 555 0100");
    const { site } = await import("./site");
    const { templateConfig } = await import("./template.config");

    expect(site.name).toBe("Atelier Noir");
    expect(site.name).toBe(templateConfig.brand.name);
    expect(site.supportPhone).toBe("+1 555 0100");
    expect(site.supportPhone).toBe(templateConfig.contact.supportPhone);
  });
});

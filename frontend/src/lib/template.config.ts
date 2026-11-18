/**
 * Single typed source for brand, contact, nav and flags. Values are baked in
 * at build time, so env edits require a rebuild.
 */
function env(name: string): string {
  const value = process.env[name];
  return typeof value === "string" && value.trim() !== "" ? value.trim() : "";
}

/** A storefront link: internal path (or absolute URL) plus its visible label. */
export interface TemplateNavLink {
  href: string;
  label: string;
}

export interface TemplateFeatureFlags {
  guestCheckout: boolean;
  restockNotifications: boolean;
  orderTracking: boolean;
  returnsRequests: boolean;
  reviews: boolean;
}

export interface TemplateManifest {
  brand: {
    name: string;
    tagline: string;
    description: string;
    url: string;
  };
  contact: {
    /** Customer support inbox (also the SMTP sender address). */
    supportEmail: string;
    /** Empty hides the channel in the UI. */
    supportPhone: string;
    /** International digits, no "+"; empty hides. */
    whatsappNumber: string;
    whatsappMessage: string;
    /** Empty hides the channel in the UI. */
    instagramUrl: string;
  };
  /** Primary header navigation. */
  nav: readonly TemplateNavLink[];
  footer: {
    shop: readonly TemplateNavLink[];
    help: readonly TemplateNavLink[];
    legal: readonly TemplateNavLink[];
  };
  featureFlags: TemplateFeatureFlags;
}

export const templateConfig = {
  brand: {
    name: env("NEXT_PUBLIC_SITE_NAME") || "Maison Aurel",
    tagline:
      env("NEXT_PUBLIC_SITE_TAGLINE") || "Perfumes composed with restraint",
    description:
      env("NEXT_PUBLIC_SITE_DESCRIPTION") ||
      "Maison Aurel is a small perfume house. Explore eau de parfum, browse new arrivals, and order with secure checkout.",
    url: env("NEXT_PUBLIC_SITE_URL") || "http://localhost:3000",
  },

  contact: {
    supportEmail: env("NEXT_PUBLIC_SUPPORT_EMAIL") || "biochem1981@gmail.com",
    supportPhone: env("NEXT_PUBLIC_SUPPORT_PHONE"),
    whatsappNumber: env("NEXT_PUBLIC_WHATSAPP_NUMBER"),
    whatsappMessage:
      env("NEXT_PUBLIC_WHATSAPP_MESSAGE") ||
      "Hi! I have a question about a fragrance.",
    instagramUrl: env("NEXT_PUBLIC_INSTAGRAM_URL"),
  },

  // The only copy of these links: Header/Footer/MenuOverlay all read here.
  nav: [
    { href: "/products", label: "Shop" },
    { href: "/products?ordering=-created_at", label: "New arrivals" },
  ],
  footer: {
    shop: [
      { href: "/products", label: "Shop all" },
      { href: "/products?ordering=-created_at", label: "New arrivals" },
      { href: "/cart", label: "Cart" },
      { href: "/orders", label: "Orders" },
      { href: "/login", label: "Sign in" },
    ],
    help: [
      { href: "/contact", label: "Contact" },
      { href: "/shipping", label: "Shipping" },
      { href: "/returns", label: "Returns & Refunds" },
    ],
    legal: [
      { href: "/privacy", label: "Privacy Policy" },
      { href: "/terms", label: "Terms & Conditions" },
    ],
  },

  // guestCheckout and restockNotifications are wired; orderTracking and
  // returnsRequests are declared only. Reviews stay off until moderated.
  featureFlags: {
    guestCheckout: true,
    restockNotifications: true,
    orderTracking: true,
    returnsRequests: true,
    reviews: false,
  },
} as const satisfies TemplateManifest;

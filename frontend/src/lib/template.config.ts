/**
 * White-label template manifest (U-W1-T1): the single typed source for brand,
 * contact, navigation links and feature flags. `site.ts` re-derives its flat
 * `site` export from this object so every existing import keeps compiling
 * untouched.
 *
 * Every brand/contact string is overridable through NEXT_PUBLIC_* env vars
 * (.env.local) — no other file hardcodes tenant values. NEXT_PUBLIC_* values
 * are baked in at build time: after editing, rebuild (next build).
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
    /** Display phone number (e.g. "+91 98765 43210"). Empty = hidden in UI. */
    supportPhone: string;
    /** WhatsApp number in international digits, no "+" (e.g. "919876543210"). Empty = hidden. */
    whatsappNumber: string;
    /** Pre-filled message for the WhatsApp deep link. */
    whatsappMessage: string;
    /** Full profile URL (e.g. "https://instagram.com/maisonaurel"). Empty = hidden. */
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

  // Mirrors the links currently hardcoded in Header/Footer. Components keep
  // their own copies until a later wave switches them over — this manifest
  // must not churn call sites in wave 1.
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

  // White-label switches for later waves: checkout-adjacent flows ship on by
  // default; reviews stay off until a tenant curates moderation.
  featureFlags: {
    guestCheckout: true,
    restockNotifications: true,
    orderTracking: true,
    returnsRequests: true,
    reviews: false,
  },
} as const satisfies TemplateManifest;

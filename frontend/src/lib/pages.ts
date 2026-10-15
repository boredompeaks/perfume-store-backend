import { site } from "./site";

/**
 * Static-route metadata registry — the single source of truth for which
 * static app routes exist, how they're titled/described, and whether search
 * engines may index them. `sitemap.ts` and `robots.ts` both consume this so
 * the two can never drift apart. Dynamic routes (e.g. /products/[slug],
 * /orders/[id]) are deliberately absent: their slugs/ids are resolved at
 * request time, not enumerated here.
 */
export type NavGroup = "shop" | "account" | "legal" | "static";

export interface PageEntry {
  /** Route path as it appears in the app router (always leading-slash). */
  path: string;
  /** Human title, matching the page's own metadata title. */
  title: string;
  /** Short meta description. */
  description: string;
  /**
   * Whether search engines may index this page. `false` => robots-disallow
   * and excluded from the sitemap. Auth, cart, checkout, orders, and staff
   * surfaces are all `false`.
   */
  indexable: boolean;
  /** Coarse grouping for navigation / sitemap heuristics. */
  navGroup?: NavGroup;
}

/**
 * Every static route that has a `page.tsx` in the app router. Order is
 * roughly informational -> transactional -> account -> legal -> staff.
 */
export const pages: readonly PageEntry[] = [
  {
    path: "/",
    title: `${site.name} — ${site.tagline}`,
    description: site.description,
    indexable: true,
    navGroup: "static",
  },
  {
    path: "/products",
    title: "Shop all fragrances",
    description: site.description,
    indexable: true,
    navGroup: "shop",
  },
  {
    path: "/cart",
    title: "Cart",
    description: "Review the items in your cart before checkout.",
    indexable: false,
    navGroup: "shop",
  },
  {
    path: "/checkout",
    title: "Checkout",
    description: "Complete your order with secure checkout.",
    indexable: false,
    navGroup: "shop",
  },
  {
    path: "/orders",
    title: "Your orders",
    description: "View and track your past and current orders.",
    indexable: false,
    navGroup: "account",
  },
  // Spec §5.6 / §8: public shipment tracking keyed on a customer credential.
  // `indexable: false` is deliberate — §8 line 2010 requires `noindex, follow`,
  // line 2019 puts it in the robots disallow list and line 2054 keeps it out
  // of the sitemap. A credential-gate surface has no business being indexed,
  // and the safer default is the one that cannot leak: the page currently has
  // no nav link at all (unlinked + disallowed = no crawl path by construction).
  {
    path: "/track-order",
    title: "Track your order",
    description: "Look up where your parcel is using your order number.",
    indexable: false,
    navGroup: "static",
  },
  {
    path: "/login",
    title: "Sign in",
    description: "Sign in to your account.",
    indexable: false,
    navGroup: "account",
  },
  {
    path: "/register",
    title: "Create account",
    description: "Create an account to start ordering.",
    indexable: false,
    navGroup: "account",
  },
  {
    path: "/verify-email",
    title: "Verify email",
    description: "Verify your email address to activate your account.",
    indexable: false,
    navGroup: "account",
  },
  {
    path: "/resend-verification",
    title: "Resend verification email",
    description: "Resend the account verification email.",
    indexable: false,
    navGroup: "account",
  },
  {
    path: "/reset-password",
    title: "Reset password",
    description: "Set a new password using your reset link.",
    indexable: false,
    navGroup: "account",
  },
  {
    path: "/forgot-password",
    title: "Reset your password",
    description: "Request a password reset email.",
    indexable: false,
    navGroup: "account",
  },
  {
    path: "/forgot-username",
    title: "Forgot username",
    description: "Recover your account username.",
    indexable: false,
    navGroup: "account",
  },
  {
    path: "/contact",
    title: "Contact",
    description: "Get in touch with customer support.",
    indexable: true,
    navGroup: "static",
  },
  {
    path: "/terms",
    title: "Terms & Conditions",
    description: "The terms and conditions governing use of this store.",
    indexable: true,
    navGroup: "legal",
  },
  {
    path: "/privacy",
    title: "Privacy Policy",
    description: "How we collect, use, and protect your personal information.",
    indexable: true,
    navGroup: "legal",
  },
  {
    path: "/shipping",
    title: "Shipping",
    description: "Shipping options, delivery times, and costs.",
    indexable: true,
    navGroup: "legal",
  },
  {
    path: "/returns",
    title: "Returns & Refunds",
    description: "Our returns and refunds policy.",
    indexable: true,
    navGroup: "legal",
  },
  {
    path: "/admin",
    title: "Admin",
    description: "Redirects to the Django admin on the API origin.",
    indexable: false,
    navGroup: "static",
  },
  // Staff surfaces carry their own `robots: { index: false }` page metadata,
  // but the current robots.ts disallow list does not include them. Marking
  // them indexable here keeps the generated disallow list identical to today's
  // behavior; flipping them to `false` would add new disallow entries.
  {
    path: "/staff/login",
    title: "Staff sign in",
    description: "Sign in for staff members.",
    indexable: true,
    navGroup: "account",
  },
  {
    path: "/staff/mfa/enroll",
    title: "Set up multi-factor authentication",
    description: "Enroll in multi-factor authentication for your staff account.",
    indexable: true,
    navGroup: "account",
  },
];

/** Paths search engines may index (sitemap candidates). */
export function publicIndexablePaths(): string[] {
  return pages.filter((p) => p.indexable).map((p) => p.path);
}

/**
 * Paths that must be robots-disallowed. Every non-indexable page is included,
 * which always covers the auth/checkout/orders set regardless of registry
 * edits elsewhere.
 */
export function disallowedPaths(): string[] {
  return pages.filter((p) => !p.indexable).map((p) => p.path);
}

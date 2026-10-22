import type { Metadata } from "next";
import { site } from "./site";

/**
 * Static-route metadata registry — the single source of truth for which
 * static app routes exist, how they're titled/described, and whether search
 * engines may index them. `sitemap.ts` and `robots.ts` both consume this so
 * the two can never drift apart, and `pageMetadata()` below is the shared
 * head-tag generation layer for §8.1 (UI-UX-SPEC). Dynamic routes (e.g.
 * /products/[slug], /orders/[id]) are deliberately absent: their slugs/ids
 * are resolved at request time, not enumerated here.
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
   * and excluded from the sitemap. Auth, cart, checkout, orders, staff,
   * recovery and /track-order surfaces are all `false`.
   */
  indexable: boolean;
  /**
   * Robots directive override for a NOINDEX page (§8.1). Omitted =>
   * "noindex, nofollow", which is what cart/checkout/orders/auth/staff/
   * recovery all ship. /track-order is the one entry the spec names
   * differently: "noindex, follow" (§8.1 line 2010) — disallowed and
   * noindexed, but its links may still be followed.
   */
  robots?: "noindex, follow" | "noindex, nofollow";
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
    robots: "noindex, follow",
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
  // §8.1 (lines 2020-2023): /staff/login and /staff/mfa/enroll are role-gated
  // and unlinked from the storefront nav — discoverable only via a link in a
  // login error message — so robots.txt must disallow them (their own page
  // metadata already says `noindex, nofollow`). `indexable: false` makes the
  // registry, robots.txt and the sitemap agree on that. The earlier `true`
  // here was a real bug: it left both routes undisallowed AND put two noindex
  // pages into the sitemap.
  {
    path: "/staff/login",
    title: "Staff sign in",
    description: "Sign in for staff members.",
    indexable: false,
    navGroup: "account",
  },
  {
    path: "/staff/mfa/enroll",
    title: "Set up multi-factor authentication",
    description: "Enroll in multi-factor authentication for your staff account.",
    indexable: false,
    navGroup: "account",
  },
];

/** Paths search engines may index (sitemap candidates). */
export function publicIndexablePaths(): string[] {
  return pages.filter((p) => p.indexable).map((p) => p.path);
}

/**
 * Prefixes robots.txt must disallow that are not registry routes. §8.1
 * (line 2020) lists `/staff` alongside its two real child routes; `/staff`
 * itself has no `page.tsx` (it always 404s), so it cannot be a registry
 * entry without breaking the one-entry-per-page.tsx contract above. The
 * prefix lives here so `disallowedPaths()` — and therefore `robots.ts` —
 * still emits exactly what the spec ordered, while `/staff/login` and
 * `/staff/mfa/enroll` keep coming from their own `indexable: false` entries.
 */
const ROBOTS_GROUP_PREFIXES: readonly string[] = ["/staff"];

/**
 * Paths that must be robots-disallowed. Every non-indexable page is included,
 * which always covers the auth/checkout/orders set regardless of registry
 * edits elsewhere, plus the spec-ordered group prefixes.
 */
export function disallowedPaths(): string[] {
  return [
    ...pages.filter((p) => !p.indexable).map((p) => p.path),
    ...ROBOTS_GROUP_PREFIXES,
  ];
}

/** §8.1 robots directive for a registry entry. */
export type PageRobots = "index" | "noindex, follow" | "noindex, nofollow";

export function pageRobots(entry: PageEntry): PageRobots {
  if (entry.indexable) return "index";
  return entry.robots ?? "noindex, nofollow";
}

export interface PageMetadataOptions {
  /**
   * Emit the title as `{ absolute }` so the root layout's template
   * ("%s — Maison Aurel") cannot append the brand. §8.1 pins two BARE
   * titles: `/` and `/track-order`. Every other registry row keeps the
   * template (the way the shipped pages already render).
   */
  absoluteTitle?: boolean;
}

/**
 * §8.1's shared metadata generation layer: title, description and canonical
 * all derive from the registry entry, so a page that calls this can never
 * disagree with the sitemap/robots pair built from the same entries.
 *
 * - Indexable pages emit no `robots` meta at all — absence means
 *   index/follow, which is what §8.1's "index" rows ask for — and inherit the
 *   root layout's site-default OG/Twitter ("site defaults" in §8.1).
 * - Noindex pages emit the explicit directive (`noindex, follow` only where
 *   §8.1 says so, `noindex, nofollow` everywhere else) and NULL out the
 *   inherited OG/Twitter: every noindex row in §8.1 reads "OG: none".
 */
export function pageMetadata(
  path: string,
  opts: PageMetadataOptions = {},
): Metadata {
  const entry = pages.find((p) => p.path === path);
  if (!entry) throw new Error(`pageMetadata: no registry entry for "${path}"`);
  return {
    title: opts.absoluteTitle ? { absolute: entry.title } : entry.title,
    description: entry.description,
    alternates: { canonical: entry.path },
    ...(entry.indexable
      ? {}
      : {
          robots: {
            index: false,
            follow: pageRobots(entry) === "noindex, follow",
          },
          openGraph: null,
          twitter: null,
        }),
  };
}

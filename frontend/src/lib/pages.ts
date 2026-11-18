import type { Metadata } from "next";
import { site } from "./site";

/**
 * Single source of truth for static routes, consumed by sitemap, robots and
 * page metadata. Dynamic routes are deliberately absent: their slugs are
 * resolved at request time, not enumerated.
 */
export type NavGroup = "shop" | "account" | "legal" | "static";

export interface PageEntry {
  path: string;
  title: string;
  description: string;
  /** `false` means robots-disallowed and excluded from the sitemap. */
  indexable: boolean;
  /**
   * Override for a noindex page; defaults to "noindex, nofollow". Only
   * /track-order ships "noindex, follow".
   */
  robots?: "noindex, follow" | "noindex, nofollow";
  navGroup?: NavGroup;
}

/** Static routes only, one per `page.tsx` in the app router. */
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
  // Credential-gated surface: noindex/follow, disallowed and out of the
  // sitemap, with no nav link, so there is no crawl path by construction.
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
  // Role-gated and unlinked, so they are disallowed and noindexed.
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
 * `/staff` has no `page.tsx` (it always 404s), so it cannot be a registry
 * entry; it is emitted as a robots prefix alongside its real child routes.
 */
const ROBOTS_GROUP_PREFIXES: readonly string[] = ["/staff"];

/** Every non-indexable page, plus the group prefixes. */
export function disallowedPaths(): string[] {
  return [
    ...pages.filter((p) => !p.indexable).map((p) => p.path),
    ...ROBOTS_GROUP_PREFIXES,
  ];
}

/** Robots directive for a registry entry. */
export type PageRobots = "index" | "noindex, follow" | "noindex, nofollow";

export function pageRobots(entry: PageEntry): PageRobots {
  if (entry.indexable) return "index";
  return entry.robots ?? "noindex, nofollow";
}

export interface PageMetadataOptions {
  /**
   * Emit the title as `{ absolute }` so the root layout's brand template
   * cannot append the brand; `/` and `/track-order` ship bare titles.
   */
  absoluteTitle?: boolean;
}

/**
 * Shared metadata generation so a page cannot disagree with sitemap/robots.
 * Indexable pages emit no robots meta and inherit the default OG/Twitter;
 * noindex pages set the explicit directive and null the inherited cards.
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

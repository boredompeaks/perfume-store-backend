import { describe, expect, it } from "vitest";
import {
  disallowedPaths,
  pageMetadata,
  pageRobots,
  pages,
  publicIndexablePaths,
} from "./pages";
import { site } from "./site";

// Every static `page.tsx` under src/app (dynamic segments like /products/[slug]
// and /orders/[id] are excluded — they resolve slugs/ids at request time).
const STATIC_PAGE_PATHS = [
  "/",
  "/admin",
  "/cart",
  "/checkout",
  "/contact",
  "/forgot-password",
  "/forgot-username",
  "/login",
  "/orders",
  "/privacy",
  "/products",
  "/register",
  "/resend-verification",
  "/reset-password",
  "/returns",
  "/shipping",
  "/staff/login",
  "/staff/mfa/enroll",
  "/terms",
  "/track-order",
  "/verify-email",
];

// The exact disallow list shipped in robots.ts before the registry refactor.
// Kept here as the behavioral baseline the registry must reproduce.
const LEGACY_ROBOTS_DISALLOW = [
  "/admin",
  "/cart",
  "/checkout",
  "/orders",
  "/login",
  "/register",
  "/verify-email",
  "/reset-password",
  "/resend-verification",
  "/forgot-username",
  "/forgot-password",
];

// Deliberate registry additions on top of the legacy baseline, each citing the
// spec row that ordered it. Any OTHER divergence from the baseline is a drift
// bug and fails the parity test below.
const SPEC_DISALLOW_ADDITIONS = [
  // §8 line 2019: /track-order is a per-customer-secret surface — noindex,
  // robots-disallow, and excluded from the sitemap (§5.6, §8 line 2054).
  "/track-order",
  // §8 lines 2020-2023: /staff/login and /staff/mfa/enroll are role-gated and
  // unlinked from the nav — discoverable only through a login-error link —
  // so they must be disallowed (page metadata already says noindex,nofollow)
  // and, being noindex, must stay out of the sitemap.
  "/staff/login",
  "/staff/mfa/enroll",
  // §8 line 2020 also names the /staff group prefix itself. It is not a route
  // (no page.tsx — it always 404s), so it rides in as a group prefix from
  // disallowedPaths() rather than as a registry entry.
  "/staff",
];

describe("page registry", () => {
  it("covers every static page.tsx path exactly once", () => {
    const paths = pages.map((p) => p.path);
    for (const expected of STATIC_PAGE_PATHS) {
      expect(paths.filter((p) => p === expected)).toHaveLength(1);
    }
  });

  it("contains no paths outside the known static set", () => {
    const known = new Set(STATIC_PAGE_PATHS);
    for (const p of pages) {
      expect(known.has(p.path)).toBe(true);
    }
  });

  it("has no duplicate paths", () => {
    const paths = pages.map((p) => p.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it("every entry has a non-empty title and description", () => {
    for (const p of pages) {
      expect(p.title.length).toBeGreaterThan(0);
      expect(p.description.length).toBeGreaterThan(0);
    }
  });
});

describe("disallowedPaths", () => {
  it("matches the legacy robots disallow list plus the pinned spec additions", () => {
    const sorted = [...disallowedPaths()].sort();
    expect(sorted).toEqual(
      [...LEGACY_ROBOTS_DISALLOW, ...SPEC_DISALLOW_ADDITIONS].sort(),
    );
  });

  it("always includes the required auth/checkout/orders set", () => {
    const disallowed = new Set(disallowedPaths());
    for (const required of [
      "/checkout",
      "/orders",
      "/login",
      "/register",
      "/verify-email",
      "/reset-password",
    ]) {
      expect(disallowed.has(required)).toBe(true);
    }
  });

  it("indexable flag drives disallow for every entry", () => {
    const disallowed = new Set(disallowedPaths());
    for (const p of pages) {
      expect(disallowed.has(p.path)).toBe(!p.indexable);
    }
  });
});

describe("publicIndexablePaths", () => {
  it("returns only indexable paths and no overlap with disallowed", () => {
    const indexable = new Set(publicIndexablePaths());
    const disallowed = new Set(disallowedPaths());
    for (const p of pages) {
      expect(indexable.has(p.path)).toBe(p.indexable);
    }
    for (const path of indexable) {
      expect(disallowed.has(path)).toBe(false);
    }
  });

  // The sitemap's static half is `pages.filter(indexable)` verbatim (sitemap.ts),
  // so pinning this set pins both the sitemap's static entries and — via the
  // no-overlap test above — robots.txt's complement.
  it("is exactly the indexable set §8.1 names (sitemap/robots parity)", () => {
    expect([...publicIndexablePaths()].sort()).toEqual(
      [
        "/",
        "/contact",
        "/privacy",
        "/products",
        "/returns",
        "/shipping",
        "/terms",
      ].sort(),
    );
  });
});

describe("§8.1 metadata generation layer (pageMetadata / pageRobots)", () => {
  it("gives every registry route a canonical, description and title from the registry", () => {
    for (const p of pages) {
      const md = pageMetadata(p.path);
      expect(md.alternates?.canonical).toBe(p.path);
      expect(md.description).toBe(p.description);
      const title = md.title;
      const resolved =
        typeof title === "string"
          ? title
          : title && "absolute" in title
            ? title.absolute
            : undefined;
      expect(resolved).toBe(p.title);
    }
  });

  it("pins the home row exactly as §8.1 states it (bare title, no robots override)", () => {
    const md = pageMetadata("/", { absoluteTitle: true });
    expect(md.title).toEqual({
      absolute: `${site.name} — ${site.tagline}`,
    });
    expect(md.alternates?.canonical).toBe("/");
    // Indexable pages emit no robots meta — absence means index, follow.
    expect(md.robots).toBeUndefined();
    // §8.1: indexable rows keep the root layout's site-default OG/Twitter.
    expect(md.openGraph).toBeUndefined();
    expect(md.twitter).toBeUndefined();
  });

  it("keeps template-applied titles as the default (the shipped KEEP shape)", () => {
    expect(pageMetadata("/cart").title).toBe("Cart");
    expect(pageMetadata("/track-order").title).toBe("Track your order");
  });

  it("clears the inherited OG/Twitter on noindex rows (§8.1 'OG: none')", () => {
    for (const p of pages.filter((entry) => !entry.indexable)) {
      const md = pageMetadata(p.path);
      expect(md.openGraph).toBeNull();
      expect(md.twitter).toBeNull();
    }
  });

  it("emits noindex,nofollow for every noindex route except /track-order (§8.1 line 2010)", () => {
    for (const p of pages.filter((entry) => !entry.indexable)) {
      const follow = p.path === "/track-order";
      expect(pageRobots(p)).toBe(follow ? "noindex, follow" : "noindex, nofollow");
      expect(pageMetadata(p.path).robots).toEqual({
        index: false,
        follow,
      });
    }
  });

  it("sends indexable routes to pageRobots' 'index' (§8.1 index rows)", () => {
    for (const p of pages.filter((entry) => entry.indexable)) {
      expect(pageRobots(p)).toBe("index");
    }
  });

  it("keeps the staff group disallowed and out of the sitemap (§8.1 lines 2020-2023)", () => {
    const disallowed = new Set(disallowedPaths());
    for (const path of ["/staff", "/staff/login", "/staff/mfa/enroll"]) {
      expect(disallowed.has(path)).toBe(true);
    }
    const indexable = new Set(publicIndexablePaths());
    expect(indexable.has("/staff/login")).toBe(false);
    expect(indexable.has("/staff/mfa/enroll")).toBe(false);
  });

  it("pageMetadata refuses to invent metadata for an unknown path", () => {
    expect(() => pageMetadata("/not-a-registry-route")).toThrow(
      /no registry entry/,
    );
  });
});

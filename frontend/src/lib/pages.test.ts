import { describe, expect, it } from "vitest";
import { pages, disallowedPaths, publicIndexablePaths } from "./pages";

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
  it("matches the legacy robots disallow list exactly", () => {
    const sorted = [...disallowedPaths()].sort();
    expect(sorted).toEqual([...LEGACY_ROBOTS_DISALLOW].sort());
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
});

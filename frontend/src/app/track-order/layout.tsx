import type { Metadata } from "next";
import { pageMetadata } from "@/lib/pages";

/**
 * Segment metadata for /track-order (§8.1, UI-UX-SPEC — the row marked NEW).
 *
 * The page is a "use client" component and Next.js forbids
 * `export const metadata` from a client page (build error, reconfirmed in
 * this wave), so this server layout is where the route's head tags live.
 * That matters beyond bookkeeping: metadata the root layout emits sits FIRST
 * in the document, so the client page's manual <title>/<meta> tags used to be
 * shadowed — the browser read the site default as this route's title and
 * description. Metadata exported here merges over the root defaults instead,
 * all of it derived from the page registry (pages.ts) via pageMetadata():
 *
 * - title "Track your order" — absolute, so the root template
 *   ("%s — Maison Aurel") cannot append the brand (§8.1 names the bare string)
 * - canonical /track-order (line 2010)
 * - robots `noindex, follow` (line 2010) — disallowed in robots.txt and
 *   excluded from the sitemap through the same registry entry
 * - description from the registry, not the site default
 * - `openGraph: null` / `twitter: null` — §8.1's "OG: none" for this row;
 *   null clears the inherited site defaults rather than advertising a
 *   per-customer tracking surface
 */
export const metadata: Metadata = pageMetadata("/track-order", {
  absoluteTitle: true,
});

export default function TrackOrderLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}

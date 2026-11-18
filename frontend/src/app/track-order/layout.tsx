import type { Metadata } from "next";
import { pageMetadata } from "@/lib/pages";

// Head tags live here because a "use client" page cannot export metadata, and
// the client page's manual tags would be shadowed by the root defaults.
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

import type { ReactNode } from "react";
import { site } from "@/lib/site";

/**
 * The shared authentication column (§5.9): `max-w-md`, centred, h1 on the
 * `--text-h1` step. Its ONE addition in the redesign is the wordmark — a
 * 448px column of form fields with no brand mark reads as a login screen
 * from 2009 (§6 AuthShell row: "ADD the wordmark line. Nothing else
 * changes"). One line of display type at `--text-h3`, centred, above the h1,
 * and nothing else. The name comes from the white-label manifest
 * (`site.name`), never a literal, so a rebrand lands here for free.
 *
 * `font-display` (Playfair, §2.2) composes with the `text-h1` / `text-h3`
 * steps — Tailwind v4 does not attach a family companion variable, so the
 * serif is applied by hand on exactly the two Playfair steps this shell uses.
 * No motion on any auth page (§5.9) — the shell renders at final position.
 */
export default function AuthShell({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="mx-auto max-w-md px-4 py-16 sm:px-6">
      <p className="text-center font-display text-h3 text-ink">{site.name}</p>
      <h1 className="mt-6 font-display text-h1">{title}</h1>
      <div className="mt-8">{children}</div>
    </div>
  );
}

import type { ReactNode } from "react";
import { site } from "@/lib/site";

/**
 * The shared authentication card (§5.9 + W13-T10 design parity): the
 * `maison_ther_connexion_sign_in` screen's anatomy, in shipped tokens. The
 * eyebrow + wordmark sit above a hairline plate over the warm paper canvas;
 * the page title is an italic Playfair line inside the plate (the screen's
 * italic "Sign in"); the form follows. `max-w-md` centred column and the
 * wordmark-on-`--text-h3` line are §5.9 KEEPs. The name comes from the
 * white-label manifest (`site.name`), never a literal, so a rebrand lands
 * here for free.
 *
 * The eyebrow "Haute Parfumerie · Archives" is the sign-in screen's own
 * brand line, in English per the shipped voice. The closing strip mirrors
 * the screen's "N° 04 · Formulation registers" flourish with "Grasse"
 * dropped: the shipped brand manifest makes no location claim, and inventing
 * one would be a fabricated claim (P5).
 *
 * `font-display` (Playfair, §2.2, italic axis loaded) composes with the
 * `text-h1` / `text-h3` steps. No motion on any auth page (§5.9) — the shell
 * renders at final position.
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
      <p className="text-center text-eyebrow text-bronze">
        Haute Parfumerie · Archives
      </p>
      <p className="mt-3 text-center font-display text-h3 text-ink">
        {site.name}
      </p>
      <div className="mt-10 border border-hairline bg-surface p-8 sm:p-10">
        <h1 className="font-display text-h1 italic">{title}</h1>
        <div className="mt-8">{children}</div>
      </div>
      <p className="mt-10 text-center text-eyebrow uppercase text-ink-secondary">
        No. 04 · Formulation registers
      </p>
    </div>
  );
}

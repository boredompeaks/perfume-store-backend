import Link from "next/link";
import type { ReactNode } from "react";
import { site } from "@/lib/site";

export function LegalShell({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
      <p className="text-xs uppercase tracking-[0.2em] text-bronze">
        {site.name}
      </p>
      <h1 className="mt-3 font-display text-4xl tracking-tight">{title}</h1>
      <p className="mt-2 text-sm text-ink-muted">
        Last updated: September 2026
      </p>
      <div className="mt-12 space-y-10">{children}</div>

      <div className="mt-16 border-t border-line pt-6 text-sm text-ink-muted">
        <p>
          Questions? Write to{" "}
          <a
            href={`mailto:${site.supportEmail}`}
            className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
          >
            {site.supportEmail}
          </a>{" "}
          — we reply within 1–2 business days. See also our{" "}
          <Link href="/terms" className="underline underline-offset-4 hover:text-bronze">
            Terms &amp; Conditions
          </Link>{" "}
          and{" "}
          <Link href="/privacy" className="underline underline-offset-4 hover:text-bronze">
            Privacy Policy
          </Link>
          .
        </p>
      </div>
    </div>
  );
}

export function LegalSection({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section>
      <h2 className="font-display text-xl tracking-tight">{title}</h2>
      <div className="mt-3 space-y-3 leading-relaxed text-ink-muted">
        {children}
      </div>
    </section>
  );
}

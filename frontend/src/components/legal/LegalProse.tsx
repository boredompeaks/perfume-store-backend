import type { ReactNode } from "react";

export type LegalProseSection = {
  /** In-page anchor target AND the ToC href — one field, so they cannot drift. */
  id: string;
  title: string;
  body: ReactNode;
};

/**
 * A5.11 CHANGE (terms + privacy only): the long-form legal body gains a plain
 * table of contents. A `<nav>` of in-page anchors sits above the first section,
 * hidden below `md` (a 7-item ToC on a phone is worse than none), and becomes a
 * sticky left column at `lg` so the page is `grid-cols-[200px_1fr]` —
 * `lg:sticky lg:top-24`, `self-start` so the grid item can actually stick.
 * **No scroll spy** (§5.11): that would be a JS scroll listener plus an
 * IntersectionObserver for a navigation aid, with its own reduced-motion story,
 * and it would add nothing the plain anchors do not already say.
 *
 * Prose runs at `--measure` / `--text-body` (§5.11, §2.2.4) in
 * `--color-ink-secondary`; section headings are `--text-h2` Playfair with
 * `--space-6` to their first paragraph (§5.12.2, §5.12.4 rule 2). Defined
 * terms are promoted by the page copy itself (`<strong className="text-ink">`),
 * which is the pattern `privacy/page.tsx` already established.
 *
 * Motion: none. Zero accent moments (§5.7, §5.12.4 rule 3) — a legal page that
 * animates is a legal page nobody trusts, and P2's wrong-when clause names
 * these pages as 0% animated, not 2%. The links' hover is a colour change, the
 * same state feedback every other legal link on the site already has.
 *
 * Server component: no `"use client"`, no effects, no listeners. The ToC and
 * the anchors are server-rendered HTML, so crawlers and the first paint see
 * the same document.
 */
export function LegalProse({
  sections,
}: {
  sections: LegalProseSection[];
}) {
  return (
    <div className="lg:grid lg:grid-cols-[200px_1fr] lg:gap-x-8">
      <nav
        aria-label="Table of contents"
        className="mb-8 hidden text-small text-ink-secondary md:block lg:mb-0 lg:sticky lg:top-24 lg:self-start"
      >
        <ol className="space-y-1">
          {sections.map((section) => (
            <li key={section.id}>
              {/* 40px tall, the same control height as the contact buttons:
                  2.5.8's 24px floor with the house's own shipped precedent. */}
              <a
                href={`#${section.id}`}
                className="block py-2.5 transition-colors hover:text-bronze"
              >
                {section.title}
              </a>
            </li>
          ))}
        </ol>
      </nav>
      <div className="min-w-0 max-w-measure space-y-10">
        {sections.map((section) => (
          <section key={section.id} id={section.id}>
            <h2 className="font-display text-h2 tracking-tight">
              {section.title}
            </h2>
            <div className="mt-6 space-y-3 text-body leading-relaxed text-ink-secondary">
              {section.body}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

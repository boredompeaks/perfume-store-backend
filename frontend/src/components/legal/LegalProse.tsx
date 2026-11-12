import type { ReactNode } from "react";

export type LegalProseSection = {
  /** Anchor target and ToC href from one field, so they cannot drift. */
  id: string;
  title: string;
  body: ReactNode;
};

/**
 * Plain in-page anchors only: a scroll spy would add a client listener and
 * observer for a nav aid. Server-rendered, so ToC and body share one document.
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
      <div className="min-w-0 max-w-(--measure) space-y-10">
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

import Link from "next/link";

type Props = {
  currentPage: number;
  totalPages: number;
  buildHref: (page: number) => string;
};

function pageWindow(current: number, total: number): (number | "gap")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages = new Set<number>(
    [1, total, current - 1, current, current + 1].filter(
      (p) => p >= 1 && p <= total,
    ),
  );
  const sorted = [...pages].sort((a, b) => a - b);
  const out: (number | "gap")[] = [];
  let prev = 0;
  for (const p of sorted) {
    if (p - prev > 1) out.push("gap");
    out.push(p);
    prev = p;
  }
  return out;
}

/**
 * §7.5 / §4: `h-9 min-w-9` (36×36) passes WCAG 2.2's 24px 2.5.8 floor but is
 * below this system's own 44px touch convention, so the control sits at
 * `h-11 min-w-11` — the same class the order-history pager already uses.
 * §4 words the bump as "below `lg`"; it is applied unconditionally here
 * because the `lg:` size variants are not in the production stylesheet and an
 * a11y repair must not emit new utilities. 44px clears §4's 32px desktop
 * floor either way.
 */
const pageLinkClass =
  "flex h-11 min-w-11 items-center justify-center px-2 text-sm transition-colors hover:text-bronze";

export default function Pagination({
  currentPage,
  totalPages,
  buildHref,
}: Props) {
  const prevHref = currentPage > 1 ? buildHref(currentPage - 1) : null;
  const nextHref =
    currentPage < totalPages ? buildHref(currentPage + 1) : null;

  return (
    <nav
      aria-label="Pagination"
      className="mt-14 flex items-center justify-center gap-1"
    >
      {prevHref ? (
        <Link href={prevHref} rel="prev" className={pageLinkClass}>
          ← Prev
        </Link>
      ) : (
        <span aria-disabled="true" className={`${pageLinkClass} opacity-40`}>
          ← Prev
        </span>
      )}

      {pageWindow(currentPage, totalPages).map((entry, index) =>
        entry === "gap" ? (
          <span key={`gap-${index}`} className="px-1 text-sm text-ink-secondary">
            …
          </span>
        ) : entry === currentPage ? (
          <span
            key={entry}
            aria-current="page"
            className="flex h-11 min-w-11 items-center justify-center bg-ink px-2 text-sm text-paper"
          >
            {entry}
          </span>
        ) : (
          <Link key={entry} href={buildHref(entry)} className={pageLinkClass}>
            {entry}
          </Link>
        ),
      )}

      {nextHref ? (
        <Link href={nextHref} rel="next" className={pageLinkClass}>
          Next →
        </Link>
      ) : (
        <span aria-disabled="true" className={`${pageLinkClass} opacity-40`}>
          Next →
        </span>
      )}
    </nav>
  );
}

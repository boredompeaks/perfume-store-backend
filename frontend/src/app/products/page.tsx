import { Suspense } from "react";
import Link from "next/link";
import type { Metadata } from "next";
import FilterBar, { CountFade } from "@/components/FilterBar";
import GridSkeleton from "@/components/GridSkeleton";
import Pagination from "@/components/Pagination";
import ProductCard from "@/components/ProductCard";
import { site } from "@/lib/site";
import { fetchProductsPage } from "@/lib/products-api";
import {
  filtersToParams,
  filtersToPath,
  isFiltered,
  parseFilters,
  type ProductFilters,
} from "@/lib/filters";

type SearchParams = Record<string, string | string[] | undefined>;
type Props = { searchParams: Promise<SearchParams> };

/**
 * §5.2 grid — 2 / 2 / 3 / 4 columns at xs/sm/md/lg, with §4's gutters (16px
 * handheld, 24px tablet, 32px desktop). §4 is explicit that sm stays at two:
 * "3 columns at 640px gives 190px cards, too narrow for a bottle to read."
 * Declared once so the real grid and the `GridSkeleton` fallback cannot drift
 * apart (§6: the skeleton mirrors the real columns at every breakpoint).
 */
const GRID_CLASS =
  "grid grid-cols-2 gap-4 md:grid-cols-3 md:gap-6 lg:grid-cols-4 lg:gap-8";

export async function generateMetadata({
  searchParams,
}: Props): Promise<Metadata> {
  const filters = parseFilters(await searchParams);
  const narrowed = isFiltered(filters);
  const qs = filtersToParams(filters).toString();
  return {
    title: narrowed ? "Search results" : "Shop all fragrances",
    description: site.description,
    // Faceted views are crawl-traps: canonical to /products, noindex when narrowed.
    alternates: {
      canonical: narrowed ? "/products" : qs ? `/products?${qs}` : "/products",
    },
    ...(narrowed ? { robots: { index: false, follow: true } } : {}),
  };
}

export default async function ProductsPage({ searchParams }: Props) {
  const filters = parseFilters(await searchParams);
  const key = filtersToParams(filters).toString();

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      {/* Screen header block (U-W11-T5): bronze eyebrow over the display
          title, eyebrow-to-title gap per the screen (4px = --space-1). The
          screen's right-hand "Série Permanente · Grasse MMXXIV" line is
          design-system fiction (A-0b: refs are a SYSTEM, not content) and is
          deliberately not shipped. */}
      <header>
        <p className="text-eyebrow uppercase text-bronze">Catalogue Raisonné</p>
        <h1 className="mt-1 font-display text-display">All fragrances</h1>
      </header>

      <FilterBar />

      {/*
        §5.2: the result count is the page's `aria-live="polite"` region;
        §7.7: the region is a container that is already in the DOM and the
        changing — cross-fading — line is its child, so the announcement and
        the animation never sit on the same element. `min-h-6` reserves the
        line's height (§2.3 cadence: 24px = --space-6) while the fetch is in
        flight so the grid never moves when the count lands.
      */}
      <div className="mt-6 min-h-6" aria-live="polite">
        <Suspense fallback={null}>
          <CountFade>
            <ResultCount filters={filters} />
          </CountFade>
        </Suspense>
      </div>

      <div className="mt-6">
        <Suspense
          key={key}
          fallback={<GridSkeleton gridClassName={GRID_CLASS} />}
        >
          <ProductGrid filters={filters} />
        </Suspense>
      </div>
    </div>
  );
}

/**
 * The count sentence, per the screen (U-W11-T5): "Showing **N creations** …",
 * with the bold middle step from the screen's emphasis. Two honest exits kept
 * from §5.2: no count is announced for a genuine empty set (the empty state
 * below speaks for it), and none for an unreachable backend — a number we
 * never received is exactly the figure P5 forbids on screen. The filtered
 * suffix ("matching your filters") is the §5.2 behaviour pin and stays; the
 * unfiltered tail is the screen's "in small batch circulation".
 */
async function ResultCount({ filters }: { filters: ProductFilters }) {
  const page = await fetchProductsPage(filtersToParams(filters)).catch(
    () => null,
  );
  if (!page || page.count === 0) return null;

  const filtered = isFiltered(filters);
  return (
    <p className="text-sm text-ink-secondary">
      Showing{" "}
      <span className="font-medium text-ink">
        {page.count} {page.count === 1 ? "creation" : "creations"}
      </span>
      {filtered ? " matching your filters" : " in small batch circulation"}
    </p>
  );
}

async function ProductGrid({ filters }: { filters: ProductFilters }) {
  const page = await fetchProductsPage(filtersToParams(filters)).catch(
    () => null,
  );
  if (!page) return <LoadFailed filters={filters} />;
  const { results, current_page, total_pages } = page;

  const buildHref = (target: number) =>
    filtersToPath({ ...filters, page: target > 1 ? target : undefined });

  if (results.length === 0) return <EmptyResults />;

  return (
    <>
      <div className={GRID_CLASS}>
        {results.map((product, index) => (
          <ProductCard
            key={product.id}
            product={product}
            priority={index === 0}
          />
        ))}
      </div>

      {total_pages > 1 && (
        <Pagination
          currentPage={current_page}
          totalPages={total_pages}
          buildHref={buildHref}
        />
      )}
    </>
  );
}

/** §5.2: KEEP copy and the "Clear all filters" action — already correct. */
function EmptyResults() {
  return (
    <div className="mt-16 py-16 text-center">
      <p className="font-display text-2xl tracking-tight">Nothing matches.</p>
      <p className="mx-auto mt-3 max-w-sm text-ink-secondary">
        Try a different search, or widen the price range.
      </p>
      <Link
        href="/products"
        className="mt-8 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
      >
        Clear all filters
      </Link>
    </div>
  );
}

/**
 * The listing's failure state (§6: every surface names its error state). It
 * renders instead of throwing, so an unreachable backend shows a designed,
 * actionable panel rather than the empty state's copy — which would tell the
 * customer to widen a price range when the truth is that the catalogue did
 * not load. Deliberately NOT the home page's `NewArrivals` shape: home is a
 * supporting section, so its catch returns `null` and the page stands
 * without it; `/products` IS the catalogue, and a silent page there is a
 * dead end. The retry is a plain link so it forces a real re-render with no
 * JS island.
 */
function LoadFailed({ filters }: { filters: ProductFilters }) {
  return (
    <div className="mt-16 py-16 text-center">
      <p className="font-display text-2xl tracking-tight">
        We couldn&apos;t load the fragrances.
      </p>
      <p className="mx-auto mt-3 max-w-sm text-ink-secondary">
        The request failed. Retry, or come back in a moment.
      </p>
      <a
        href={filtersToPath(filters)}
        className="mt-8 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
      >
        Try again
      </a>
    </div>
  );
}

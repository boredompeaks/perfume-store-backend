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

// Declared once so the real grid and the GridSkeleton fallback cannot drift:
// 2/2/3/4 columns, with sm deliberately staying at two.
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
      <header>
        <p className="text-eyebrow uppercase text-bronze">Catalogue Raisonné</p>
        <h1 className="mt-1 font-display text-display">All fragrances</h1>
      </header>

      <FilterBar />

      {/* The aria-live region stays a stable container with the cross-fading
          line as its child; min-h reserves the line so the grid never moves. */}
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

// No count for an empty set or an unreachable backend — only a number the
// server actually returned.
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

// Distinct from the empty state, which would wrongly suggest widening filters.
// A plain link retry forces a server re-render with no JS island.
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

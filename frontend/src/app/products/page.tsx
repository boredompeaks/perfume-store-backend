import { Suspense } from "react";
import Link from "next/link";
import type { Metadata } from "next";
import FilterBar from "@/components/FilterBar";
import GridSkeleton from "@/components/GridSkeleton";
import Pagination from "@/components/Pagination";
import ProductCard from "@/components/ProductCard";
import { site } from "@/lib/site";
import {
  fetchProductsPage,
} from "@/lib/products-api";
import {
  filtersToParams,
  filtersToPath,
  isFiltered,
  parseFilters,
  type ProductFilters,
} from "@/lib/filters";

type SearchParams = Record<string, string | string[] | undefined>;
type Props = { searchParams: Promise<SearchParams> };

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

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <header>
        <h1 className="font-display text-4xl tracking-tight">
          Shop all fragrances
        </h1>
      </header>

      <FilterBar />

      <Suspense key={filtersToParams(filters).toString()} fallback={<div className="mt-8"><GridSkeleton /></div>}>
        <ProductSection filters={filters} />
      </Suspense>
    </div>
  );
}

async function ProductSection({ filters }: { filters: ProductFilters }) {
  const page = await fetchProductsPage(filtersToParams(filters));
  const { results, count, current_page, total_pages } = page;

  const buildHref = (target: number) =>
    filtersToPath({ ...filters, page: target > 1 ? target : undefined });

  if (results.length === 0) {
    return (
      <div className="mt-16 py-16 text-center">
        <p className="font-display text-2xl tracking-tight">
          Nothing matches.
        </p>
        <p className="mx-auto mt-3 max-w-sm text-ink-muted">
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

  return (
    <>
      <p className="mt-6 text-sm text-ink-muted" aria-live="polite">
        {count} {count === 1 ? "product" : "products"}
        {isFiltered(filters) ? " matching your filters" : ""}
      </p>

      <div className="mt-6 grid grid-cols-2 gap-x-4 gap-y-10 sm:grid-cols-3 lg:grid-cols-4">
        {results.map((product, index) => (
          <ProductCard key={product.id} product={product} priority={index === 0} />
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

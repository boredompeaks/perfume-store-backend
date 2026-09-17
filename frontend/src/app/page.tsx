import { Suspense } from "react";
import Link from "next/link";
import GridSkeleton from "@/components/GridSkeleton";
import ProductCard from "@/components/ProductCard";
import { fetchProductsPage } from "@/lib/products-api";
import { site } from "@/lib/site";

// Rendered on demand with the underlying fetch cached for 60s
// (next.revalidate in products-api). Not ISR: a build without backend
// access would otherwise bake the empty "New arrivals" state into the
// prerender until an unverified background revalidation.
export const dynamic = "force-dynamic";

async function NewArrivals() {
  try {
    const page = await fetchProductsPage(
      new URLSearchParams({ ordering: "-created_at" }),
    );
    if (page.results.length === 0) return null;

    return (
      <section className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
        <div className="flex items-baseline justify-between gap-4">
          <h2 className="font-display text-3xl tracking-tight">
            New arrivals
          </h2>
          <Link
            href="/products?ordering=-created_at"
            className="text-sm underline underline-offset-4 transition-colors hover:text-bronze"
          >
            View all
          </Link>
        </div>
        <div className="mt-8 grid grid-cols-2 gap-x-4 gap-y-10 sm:grid-cols-3 lg:grid-cols-4">
          {page.results.map((product, index) => (
            <ProductCard key={product.id} product={product} priority={index < 2} />
          ))}
        </div>
      </section>
    );
  } catch {
    // Backend unreachable — the home page must still render.
    return null;
  }
}

export default function HomePage() {
  const organizationJsonLd = {
    "@context": "https://schema.org",
    "@type": "Organization",
    name: site.name,
    url: site.url,
    description: site.description,
  };

  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(organizationJsonLd),
        }}
      />

      <section className="mx-auto max-w-6xl px-4 sm:px-6">
        <div className="border-b border-line py-24 md:py-36">
          <p className="text-xs uppercase tracking-[0.2em] text-bronze">
            Eau de parfum
          </p>
          <h1 className="mt-5 max-w-3xl font-display text-5xl leading-[1.05] tracking-tight md:text-7xl">
            Perfumes composed with restraint.
          </h1>
          <p className="mt-6 max-w-xl text-lg text-ink-muted">
            {site.name} is a small perfume house. Fewer materials, longer
            macerations, no seasonal noise — fragrances built to be worn
            often.
          </p>
          <div className="mt-10 flex flex-wrap items-center gap-4">
            <Link
              href="/products"
              className="bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
            >
              Shop all fragrances
            </Link>
            <Link
              href="/products?ordering=-created_at"
              className="text-sm underline underline-offset-4 transition-colors hover:text-bronze"
            >
              See new arrivals
            </Link>
          </div>
        </div>
      </section>

      <Suspense
        fallback={
          <section className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
            <GridSkeleton count={4} />
          </section>
        }
      >
        <NewArrivals />
      </Suspense>

      <section className="mx-auto max-w-6xl px-4 pb-24 sm:px-6">
        <div className="grid gap-10 md:grid-cols-2">
          <h2 className="font-display text-3xl tracking-tight">
            The house position
          </h2>
          <div className="space-y-4 leading-relaxed text-ink-muted">
            <p>
              Every fragrance is built around a single idea, worked until it
              holds. We would rather release three perfumes we stand behind
              than thirty that fill a catalogue.
            </p>
            <p>
              What you read on the page is what you get: the materials, the
              concentration, the volume. Nothing is implied that we cannot
              show.
            </p>
          </div>
        </div>
      </section>
    </>
  );
}

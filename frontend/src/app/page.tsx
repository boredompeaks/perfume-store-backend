import { Suspense } from "react";
import Link from "next/link";
import GridSkeleton from "@/components/GridSkeleton";
import ProductCard from "@/components/ProductCard";
import { fetchProductsPage } from "@/lib/products-api";
import { toJsonLdScriptContent } from "@/lib/json-ld";
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
      <section className="mx-auto max-w-6xl px-4 py-24 sm:px-6">
        <div className="flex items-baseline justify-between gap-4 border-t border-line pt-10">
          <h2 className="font-display text-3xl tracking-tight">
            New arrivals
          </h2>
          <Link
            href="/products?ordering=-created_at"
            className="link-underline text-sm transition-colors hover:text-bronze"
          >
            View all
          </Link>
        </div>
        <div className="mt-10 grid grid-cols-2 gap-x-4 gap-y-12 sm:grid-cols-3 lg:grid-cols-4">
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
          __html: toJsonLdScriptContent(organizationJsonLd),
        }}
      />

      <section className="noise relative overflow-hidden border-b border-line">
        <span
          aria-hidden="true"
          className="text-ghost pointer-events-none absolute -right-8 top-6 hidden select-none font-display text-[20rem] leading-none lg:block"
        >
          A
        </span>
        <div className="relative mx-auto max-w-6xl px-4 py-28 sm:px-6 md:py-40">
          <p className="text-xs uppercase tracking-[0.25em] text-bronze">
            Eau de parfum · Small batches
          </p>
          <h1 className="mt-6 max-w-4xl font-display text-6xl leading-[0.98] tracking-tight md:text-8xl">
            Perfumes composed with{" "}
            <em className="italic text-bronze">restraint</em>.
          </h1>
          <p className="mt-8 max-w-xl text-lg leading-relaxed text-ink-muted">
            {site.name} is a small perfume house. Fewer materials, longer
            macerations, no seasonal noise — fragrances built to be worn
            often.
          </p>
          <div className="mt-12 flex flex-wrap items-center gap-5">
            <Link
              href="/products"
              className="bg-ink px-7 py-3.5 text-sm text-paper transition-all hover:bg-bronze active:scale-[0.98]"
            >
              Shop all fragrances
            </Link>
            <Link
              href="/products?ordering=-created_at"
              className="link-underline text-sm transition-colors hover:text-bronze"
            >
              See new arrivals
            </Link>
          </div>
          <p className="mt-14 flex flex-wrap gap-x-6 gap-y-2 text-xs uppercase tracking-[0.15em] text-ink-muted">
            <span>Ships across India</span>
            <span aria-hidden="true" className="text-line">·</span>
            <span>Secure Razorpay payments</span>
            <span aria-hidden="true" className="text-line">·</span>
            <span>7-day returns</span>
          </p>
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
        <div className="grid gap-10 border-t border-line pt-10 md:grid-cols-2">
          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-bronze">
              The house position
            </p>
            <h2 className="mt-4 font-display text-3xl tracking-tight">
              Built around a single idea, worked until it holds.
            </h2>
          </div>
          <div className="space-y-4 leading-relaxed text-ink-muted md:pt-12">
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

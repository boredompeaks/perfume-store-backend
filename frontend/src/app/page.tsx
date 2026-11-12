import { Suspense } from "react";
import Link from "next/link";
import type { Metadata } from "next";
import HomeCarousel from "@/components/home/HomeCarousel";
import { fetchProductsPage } from "@/lib/products-api";
import { toJsonLdScriptContent } from "@/lib/json-ld";
import { getReturnWindow } from "@/lib/policies";
import { pageMetadata } from "@/lib/pages";
import { site } from "@/lib/site";

// On-demand rather than ISR: a backend-less build would otherwise prerender
// the empty arrivals state. The fetch itself is cached for 60s.
export const dynamic = "force-dynamic";

// Title/description are pinned from the page registry so home cannot drift
// from the robots.txt/sitemap pair built from the same entry.
export const metadata: Metadata = pageMetadata("/", { absoluteTitle: true });

// Server-fetched props keep the 60s cache and no client request; the island
// only holds the slide index. Empty or unreachable results omit the section.
async function NewArrivals() {
  try {
    const page = await fetchProductsPage(
      new URLSearchParams({ ordering: "-created_at" }),
    );
    const products = page.results.slice(0, 4);
    if (products.length === 0) return null;

    return (
      <section className="settle mx-auto max-w-6xl px-4 pt-16 sm:px-6 md:pt-24 lg:pt-32">
        <HomeCarousel products={products} />
      </section>
    );
  } catch {
    return null;
  }
}

// Mirrors the real slide's plate: a differently shaped skeleton causes the CLS
// it exists to prevent.
function CarouselFallback() {
  return (
    <section
      aria-hidden="true"
      className="mx-auto max-w-6xl px-4 pt-16 sm:px-6 md:pt-24 lg:pt-32"
    >
      <div className="border border-hairline">
        <div className="grid grid-cols-1 lg:grid-cols-12">
          <div className="skeleton aspect-[3/4] lg:col-span-5" />
          <div className="space-y-4 p-6 sm:p-8 lg:col-span-7 lg:p-16">
            <div className="skeleton h-3 w-1/3" />
            <div className="skeleton h-8 w-1/2" />
            <div className="skeleton h-3 w-2/3" />
          </div>
        </div>
      </div>
    </section>
  );
}

export default async function HomePage() {
  // The strip states the return window only when the API publishes one.
  const { days } = await getReturnWindow();
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

      {/* LCP hero: no animation and no content-visibility, which would defer
          the paint this section exists to win. No campaign photograph. */}
      <section className="noise relative overflow-hidden border-b border-hairline">
        <span
          aria-hidden="true"
          className="text-ghost pointer-events-none absolute -right-8 top-6 hidden select-none font-display text-[20rem] leading-none lg:block"
        >
          A
        </span>
        <div className="mx-auto max-w-6xl px-4 sm:px-6">
          <div className="flex min-h-[62svh] max-w-4xl flex-col justify-center py-16 md:py-24 lg:py-32">
            <p className="text-label uppercase text-bronze">
              Eau de parfum · Small batches
            </p>
            <h1 className="mt-6 font-display text-display">
              Perfumes composed with{" "}
              <em className="italic text-bronze">restraint</em>.
            </h1>
            <p className="mt-8 max-w-(--measure) text-body-lg text-ink-secondary">
              {site.name} is a small perfume house. Fewer materials, longer
              macerations, no seasonal noise — fragrances built to be worn
              often.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-6">
              <Link
                href="/products"
                className="inline-flex min-h-12 items-center bg-ink px-8 text-label-sm uppercase text-paper transition-colors hover:bg-bronze"
              >
                Shop all fragrances
              </Link>
              <Link
                href="/products?ordering=-created_at"
                className="link-underline text-small text-ink transition-colors hover:text-bronze"
              >
                See new arrivals
              </Link>
            </div>
          </div>
        </div>
      </section>

      <Suspense fallback={<CarouselFallback />}>
        <NewArrivals />
      </Suspense>

      {/* cv-auto: this band sits a full screen below the hero, so skip its
          layout/paint until it nears the viewport. */}
      <section className="settle cv-auto mx-auto max-w-6xl px-4 pt-16 sm:px-6 md:pt-24 lg:pt-32">
        <div className="grid gap-6 border-t border-hairline pt-8 md:grid-cols-12 md:gap-8">
          <div className="md:col-span-5">
            <p className="text-label uppercase text-bronze">
              The house position
            </p>
            <h2 className="mt-4 font-display text-h1">
              Built around a single idea, worked until it holds.
            </h2>
          </div>
          <div className="space-y-4 text-body text-ink-secondary md:col-span-7 md:pt-8">
            <p className="max-w-(--measure)">
              Every fragrance is built around a single idea, worked until it
              holds. We would rather release three perfumes we stand behind
              than thirty that fill a catalogue.
            </p>
            <p className="max-w-(--measure)">
              What you read on the page is what you get: the materials, the
              concentration, the volume. Nothing is implied that we cannot
              show.
            </p>
          </div>
        </div>
      </section>

      {/* cv-auto: last band, always below the fold once the carousel is on
          screen. The return window renders only when the API publishes one. */}
      <section className="settle cv-auto mx-auto max-w-6xl px-4 py-16 sm:px-6 md:py-24 lg:py-32">
        <p className="flex flex-wrap items-center gap-x-6 gap-y-2 text-label uppercase text-ink-secondary">
          <span>Ships across India</span>
          <span aria-hidden="true" className="text-hairline">
            ·
          </span>
          <span>Secure Razorpay payments</span>
          <span aria-hidden="true" className="text-hairline">
            ·
          </span>
          <span>{days ? `${days}-day returns` : "Returns"}</span>
        </p>
      </section>
    </>
  );
}

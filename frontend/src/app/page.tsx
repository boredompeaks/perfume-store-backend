import { Suspense } from "react";
import Link from "next/link";
import GridSkeleton from "@/components/GridSkeleton";
import ProductCard from "@/components/ProductCard";
import { fetchProductsPage } from "@/lib/products-api";
import { toJsonLdScriptContent } from "@/lib/json-ld";
import { getReturnWindow } from "@/lib/policies";
import { site } from "@/lib/site";

// Rendered on demand with the underlying fetch cached for 60s
// (next.revalidate in products-api). Not ISR: a build without backend
// access would otherwise bake the empty "New arrivals" state into the
// prerender until an unverified background revalidation.
export const dynamic = "force-dynamic";

/** §5.1 §2 — new arrivals: newest four (the server's default page size). */
async function NewArrivals() {
  try {
    const page = await fetchProductsPage(
      new URLSearchParams({ ordering: "-created_at" }),
    );
    if (page.results.length === 0) return null;

    return (
      <section className="settle mx-auto max-w-6xl px-4 pt-16 sm:px-6 md:pt-24 lg:pt-32">
        <div className="flex items-baseline gap-4 border-t border-hairline pt-8">
          <h2 className="font-display text-h1">New arrivals</h2>
          <Link
            href="/products?ordering=-created_at"
            className="link-underline ml-auto text-small text-ink transition-colors hover:text-bronze"
          >
            View all
          </Link>
        </div>
        <div className="mt-6 grid grid-cols-2 gap-x-4 gap-y-8 sm:grid-cols-3 lg:grid-cols-4">
          {page.results.map((product, index) => (
            <ProductCard
              key={product.id}
              product={product}
              priority={index < 2}
            />
          ))}
        </div>
      </section>
    );
  } catch {
    // Backend unreachable — the home page must still render, quietly.
    return null;
  }
}

export default async function HomePage() {
  // A-12.D.2: the strip states the window only when the API publishes one;
  // without it the item keeps its place in the strip as plain "Returns".
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

      {/* §5.1 §1 — hero: the brand statement and the entry to the catalogue.
          Typographic on paper: there is no campaign photograph, so the KEEP
          paper grain and ghost "A" are the only imagery. Nothing in here
          animates — the hero is the first-screen content and the LCP
          candidate (§10, §3: "Never on the hero"). */}
      <section className="noise relative overflow-hidden border-b border-hairline">
        <span
          aria-hidden="true"
          className="text-ghost pointer-events-none absolute -right-8 top-6 hidden select-none font-display text-[20rem] leading-none lg:block"
        >
          A
        </span>
        <div className="mx-auto max-w-6xl px-4 sm:px-6">
          {/* §5.1: the column is vertically centred in a hero of ~62% of the
              viewport height — not the full-viewport 50% — carrying §2.3's
              hero vertical padding (--space-16/24/32), so the block sits
              above the optical centre and leaves air beneath it. */}
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

      <Suspense
        fallback={
          <section className="mx-auto max-w-6xl px-4 pt-16 sm:px-6 md:pt-24 lg:pt-32">
            <GridSkeleton count={4} />
          </section>
        }
      >
        <NewArrivals />
      </Suspense>

      {/* §5.1 §3 — the house position. Two columns at md, copy KEEP verbatim:
          it is the most honest text on the site. */}
      <section className="settle mx-auto max-w-6xl px-4 pt-16 sm:px-6 md:pt-24 lg:pt-32">
        <div className="grid gap-4 border-t border-hairline pt-8 md:grid-cols-2 md:gap-6 lg:gap-8">
          <div>
            <p className="text-label uppercase text-bronze">
              The house position
            </p>
            <h2 className="mt-4 font-display text-h1">
              Built around a single idea, worked until it holds.
            </h2>
          </div>
          <div className="space-y-4 text-body text-ink-secondary md:pt-8">
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

      {/* §5.1 §4 — three verifiable facts about how the store behaves, not
          trust badges. The return window renders only when the API publishes
          one (A-12.D.2); the `·` glyphs are decorative. */}
      <section className="settle mx-auto max-w-6xl px-4 py-16 sm:px-6 md:py-24 lg:py-32">
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

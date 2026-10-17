import { Suspense } from "react";
import Link from "next/link";
import type { Metadata } from "next";
import HomeCarousel from "@/components/home/HomeCarousel";
import { fetchProductsPage } from "@/lib/products-api";
import { toJsonLdScriptContent } from "@/lib/json-ld";
import { getReturnWindow } from "@/lib/policies";
import { pageMetadata } from "@/lib/pages";
import { site } from "@/lib/site";

// Rendered on demand with the underlying fetch cached for 60s
// (next.revalidate in products-api). Not ISR: a build without backend
// access would otherwise bake the empty "New arrivals" state into the
// prerender until an unverified background revalidation.
export const dynamic = "force-dynamic";

// §8.1 `/` row: canonical is `/`, robots index, title the exact default
// string (absolute so the root template cannot re-wrap it), description the
// site's real one. Title/description already arrive from the root layout;
// pageMetadata (registry-driven generation layer) adds the canonical and
// pins both values so home cannot drift from the robots.txt/sitemap pair
// built from the same registry entry.
export const metadata: Metadata = pageMetadata("/", { absoluteTitle: true });

/**
 * §5.1 §2 — new arrivals: the newest four (the server's default page size),
 * fetched on the server and handed to the D1 screen's carousel as props
 * (`maison_ther_catalogue_accueil_carousel_edition`). The fetch therefore
 * keeps its 60s revalidate cache and no client request runs before the
 * section paints; the client island only holds the slide index.
 *
 * P5 honesty on both failure paths: an empty result set and an unreachable
 * backend each omit the section outright. A carousel cannot exist without
 * slides, and §5.1's discipline is that the home page still renders quietly
 * — never with an error dump or an invented placeholder product.
 */
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
    // Backend unreachable — the home page must still render, quietly.
    return null;
  }
}

/**
 * §6: the fallback mirrors the real slide's plate — same hairline frame,
 * same 5/7 image-to-copy split, same padding — because a skeleton of a
 * different shape causes the CLS it exists to prevent. Four shimmer blocks,
 * well under §3's 12-instance page cap.
 */
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
          candidate (§10, §3: "Never on the hero"). The D1 screen shows an
          atelier photograph behind this block; §5.1 bans the stock-photo
          hero (one image per product, none of them a campaign asset) and
          W6-T1 ruled this treatment compliant, so the photograph is not
          adopted. */}
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

      <Suspense fallback={<CarouselFallback />}>
        <NewArrivals />
      </Suspense>

      {/* §5.1 §3 — the house position, composed against the D1 screen: the
          screen's asymmetric 5/7 split (code.html:446-457) instead of the
          even 2-column grid, applied from `md` so §5.1's "two columns at md"
          still holds. Copy KEEP verbatim — it is the most honest text on the
          site; the screen's own wording ("Philosophy of measure") is mockup
          drift against that KEEP. */}
      <section className="settle mx-auto max-w-6xl px-4 pt-16 sm:px-6 md:pt-24 lg:pt-32">
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

      {/* §5.1 §4 — three verifiable facts about how the store behaves, not
          trust badges. The return window renders only when the API publishes
          one (A-12.D.2); the `·` glyphs are decorative. The screen's band
          ("Complimentary shipping … Razorpay & Stripe … returnable within
          seven days") claims two things this store does not do (Stripe, a
          blanket seven-day window), so §5.1's verified wording stays. */}
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

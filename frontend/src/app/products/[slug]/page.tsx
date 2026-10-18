import Link from "next/link";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import AddToCartButton from "@/components/AddToCartButton";
import OlfactoryPyramid from "@/components/OlfactoryPyramid";
import ProductImage from "@/components/ProductImage";
import ProtocolTrio from "@/components/ProtocolTrio";
import RestockForm from "@/components/RestockForm";
import StickyPurchaseBar from "@/components/StickyPurchaseBar";
import { ApiError } from "@/lib/api";
import { mediaUrl } from "@/lib/config";
import { toJsonLdScriptContent } from "@/lib/json-ld";
import { formatINR } from "@/lib/money";
import { fetchProduct, toOlfactoryTiers } from "@/lib/products-api";
import { getReturnWindow, shippingPolicy } from "@/lib/policies";
import { site } from "@/lib/site";
import { templateConfig } from "@/lib/template.config";

type Props = { params: Promise<{ slug: string }> };

/**
 * The in-flow purchase row's hook for `StickyPurchaseBar` (§5.2: the bar
 * appears "once the page scrolls past the in-flow purchase block"). One
 * definition here, passed as a prop, so the anchor and the observer can
 * never drift apart.
 */
const PURCHASE_ANCHOR_ID = "pdp-purchase-anchor";

/**
 * §8.1 KEEP — this function is deliberately byte-identical to the shipped
 * version: the title shape, canonical, robots and product OG are the PDP's
 * SEO contract, and U-W7-T1 redesigns presentation only.
 */
export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  try {
    const product = await fetchProduct(slug);
    const image = mediaUrl(product.image);
    return {
      title: `${product.name} — Eau de Parfum ${product.size} ml`,
      description: product.description.slice(0, 155),
      alternates: { canonical: `/products/${product.slug}` },
      openGraph: {
        title: `${product.name} — ${site.name}`,
        description: product.description.slice(0, 155),
        url: `/products/${product.slug}`,
        images: image ? [{ url: image, alt: product.name }] : undefined,
      },
    };
  } catch {
    return { title: "Product not found" };
  }
}

export default async function ProductPage({ params }: Props) {
  const { slug } = await params;

  // §8.1: a genuine miss (bad slug, discontinued product) still ends in
  // notFound(), so the route answers 404 through not-found.tsx. Everything
  // else — the API unreachable, a 5xx — is NOT "not found": that renders the
  // designed failure panel below instead of lying to the customer with a
  // 404 page for an outage.
  const product = await fetchProduct(slug).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 404) notFound();
    return null;
  });
  if (!product) return <ProductLoadFailed slug={slug} />;

  const image = mediaUrl(product.image);
  const inStock = product.stock > 0;
  // Spec §5.2 item 4 / §12.G6: while the product is sold out the restock
  // form takes the add-to-cart row entirely (spec A-6: it "replaces the
  // add-to-cart button"). An in-stock PDP keeps today's button, unchanged —
  // this is the same `stock > 0` derivation AddToCartButton uses for its
  // own sold-out branch. The white-label flag is the kill switch.
  const showRestockForm =
    !inStock && templateConfig.featureFlags.restockNotifications;

  // §5.2 item 6 — the one delivery/returns line. Both numbers are the ones
  // the store actually publishes: the dispatch window from the single-source
  // policy constant (`/shipping` renders the same value), the return window
  // only while GET /api/settings/ publishes one (A-12.D.2) — with no number
  // the same line ships minus its claim, never a fallback.
  const { days } = await getReturnWindow();

  // §5.2 item 5 — `Product.description` is one free-text field (§12-G10);
  // it stays prose at measure below and is never parsed into tiers. The
  // structured pyramid (design screen "Olfactory Architecture") renders
  // only when the server publishes `notes` (U-W11-T6; contract gap logged
  // in BACKEND_REQUESTS.md) — with none published the section is absent
  // rather than invented (the banned three-column grid in different
  // clothes). U-W15-T18 repositions the prose itself into the screen's
  // philosophy split (see the section below); `hasDescription` gates that
  // section exactly as it once gated the notes band.
  const pyramidTiers = toOlfactoryTiers(product.notes);

  const hasDescription = product.description.trim().length > 0;

  const productJsonLd = {
    "@context": "https://schema.org",
    "@type": "Product",
    name: product.name,
    description: product.description,
    ...(image ? { image } : {}),
    brand: { "@type": "Brand", name: site.name },
    offers: {
      "@type": "Offer",
      price: product.price,
      priceCurrency: "INR",
      availability: inStock
        ? "https://schema.org/InStock"
        : "https://schema.org/OutOfStock",
      itemCondition: "https://schema.org/NewCondition",
      url: `${site.url}/products/${product.slug}`,
    },
  };

  const breadcrumbJsonLd = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: [
      { "@type": "ListItem", position: 1, name: "Home", item: site.url },
      {
        "@type": "ListItem",
        position: 2,
        name: "Products",
        item: `${site.url}/products`,
      },
      {
        "@type": "ListItem",
        position: 3,
        name: product.name,
        item: `${site.url}/products/${product.slug}`,
      },
    ],
  };

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: toJsonLdScriptContent(productJsonLd) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: toJsonLdScriptContent(breadcrumbJsonLd),
        }}
      />

      {/* §5.2 item 1 — breadcrumb, KEEP as shipped. */}
      <nav aria-label="Breadcrumb" className="text-sm text-ink-secondary">
        <ol className="flex items-center gap-2">
          <li>
            <Link href="/" className="transition-colors hover:text-bronze">
              Home
            </Link>
          </li>
          <li aria-hidden="true">/</li>
          <li>
            <Link
              href="/products"
              className="transition-colors hover:text-bronze"
            >
              Products
            </Link>
          </li>
          <li aria-hidden="true">/</li>
          <li aria-current="page" className="text-ink">
            {product.name}
          </li>
        </ol>
      </nav>

      {/*
        Design screen (ther_s_culaire…refined_hero_flow) "Top Metadata
        Bar": the server's own category string (P5: rendered from the API or
        not at all) over a hairline, spanning both columns. The screen's
        batch line has no field behind it, so the bar carries only what the
        API publishes.
      */}
      <div className="mt-8 flex items-center justify-between border-b border-hairline pb-4">
        {product.category ? (
          <p className="flex items-center gap-3 text-eyebrow uppercase text-bronze">
            <span
              aria-hidden="true"
              className="inline-block h-1.5 w-1.5 rounded-full bg-bronze"
            />
            {product.category}
          </p>
        ) : null}
      </div>

      {/*
        §5.2 item 2 — THE image, and the LCP element: one `aspect-[4/5]`
        plate (`--color-hairline`, §2.1.1's canonical plate), the spec's own
        `sizes` string (§10.3 KEEP), `priority` for the preload. It never
        animates: nothing above the fold may move before LCP (§10, §3
        "Never on the hero" — extended to the PDP's bottle by STITCH §2 row
        3, "the bottle itself never moves"). No gallery, no dots, no
        thumbnails (P4: the model has exactly one ImageField). The screen's
        asymmetric split (image 7 / panel 5) replaces the old 1:1 columns.
      */}
      <div className="mt-10 grid items-start gap-8 lg:grid-cols-12 lg:gap-12">
        <div className="relative aspect-[4/5] bg-hairline lg:col-span-7">
          <ProductImage
            src={product.image}
            alt={product.name}
            priority
            sizes="(min-width: 768px) 50vw, 100vw"
          />
        </div>

        {/*
          §5.2 item 3 — purchase block, framed as the screen's purchase
          module: a hairline panel (no shadow, no radius) holding eyebrow ->
          h1 -> price -> volume -> stock -> action, `lg:sticky lg:top-24`
          KEEP. The `.settle` entrance is the one entrance §5.2:1312-1313
          sanctions for this page; because it is driven by a view() timeline
          it has already settled wherever the block is above the fold at
          load (so the LCP guard holds by construction) and only plays on
          handhelds, where the block arrives after the bottle.
        */}
        <div className="settle border border-hairline bg-surface p-6 sm:p-8 lg:col-span-5 lg:sticky lg:top-24 lg:self-start">
          <h1 className="font-display text-h1">{product.name}</h1>
          {/* §2.2.3: money is the money step with tabular lining figures, the same treatment the cards and cart lines use. */}
          <p className="mt-4 money tabular-nums lining-nums">
            {formatINR(product.price)}
          </p>

          {/*
            Volume, in the screen's selector vocabulary: a rectangular
            hairline option, selected state on the ink border (0 radius, per
            the system). P4 and the API agree on one SKU per product, so the
            single real volume renders as the selected option; a second
            flacon size cannot be honoured and none is fabricated (P5). When
            a variants contract lands, more options slot into this group.
          */}
          <div className="mt-6">
            <p className="text-eyebrow uppercase text-ink-secondary">
              Volume
            </p>
            <div
              role="group"
              aria-label="Volume"
              className="mt-3 flex items-center justify-between border border-ink px-4 py-3.5"
            >
              <span className="text-label text-ink">{product.size} ml</span>
            </div>
          </div>

          {/* CHANGE-3: `--color-success` text with a 6px dot, or muted "Sold out" — the same `stock` value the JSON-LD above reads. */}
          {inStock ? (
            <p className="mt-6 flex items-center gap-2 text-sm text-success">
              <span
                aria-hidden="true"
                className="inline-block h-1.5 w-1.5 rounded-full bg-success"
              />
              In stock
            </p>
          ) : (
            <p className="mt-6 text-sm text-ink-secondary">Sold out</p>
          )}

          <div id={PURCHASE_ANCHOR_ID} className="mt-8">
            {showRestockForm ? (
              <RestockForm slug={product.slug} />
            ) : (
              <AddToCartButton productId={product.id} stock={product.stock} />
            )}
          </div>

          {/*
            §5.2 item 6 — the one delivery/returns line, seated in the
            purchase panel where the screen puts its logistics rows. Both
            numbers are the ones the store actually publishes: the dispatch
            window from the single-source policy constant (`/shipping`
            renders the same value), the return window only while
            GET /api/settings/ publishes one (A-12.D.2) — with no number
            the same line ships minus its claim, never a fallback.
          */}
          <p className="mt-8 border-t border-hairline pt-4 text-small text-ink-secondary">
            <Link
              href="/shipping"
              className="link-underline text-ink transition-colors hover:text-bronze"
            >
              Typically dispatched within {shippingPolicy.dispatchWindow}
            </Link>
            {" · "}
            <Link
              href="/returns"
              className="link-underline text-ink transition-colors hover:text-bronze"
            >
              Returns{days ? ` within ${days} days of delivery` : " policy"}
            </Link>
          </p>
        </div>
      </div>

      {/* Structured pyramid, only when the server publishes notes (see above). */}
      <OlfactoryPyramid tiers={pyramidTiers} />

      {/*
        §5.2 items 5 and 7 at the design screen's deferred slice
        (U-W15-T18; screen block "EDITORIAL ESSENCE & SUPPORTING STILL
        LIFE"). The description prose T6 carried in a plain notes band
        repositions here, into the screen's asymmetric editorial split:
        the narrow story measure left, the product's single real image
        framed as the figure right (P4: no stock photography and no
        invented still life — the bottle is the only image the store
        owns; the screen's amber-resin photograph is mockup content).
        The section is data-gated on the free-text field itself: with no
        description it renders nothing at all, exactly like the pyramid
        above (P5 / §12-G10), pinned in lib/pdp.test.ts.

        Plate colour: §5.2 item 7 still names the retired bronze-soft
        tint, but §2.1.1 dropped that token — hairline is its canonical
        plate and the one the 4:5 product plate already ships
        (ProductCard.tsx records the succession); referencing the
        retired name would fail the colour-token guard in lib/ui.test.ts.
      */}
      {hasDescription && (
        <section
          aria-labelledby="pdp-philosophy-heading"
          className="mt-16 border-t border-hairline pt-8 md:mt-24 lg:mt-32 lg:grid lg:grid-cols-12 lg:items-center lg:gap-12"
        >
          {/*
            Story measure: eyebrow + heading + the product's own prose at
            36ch (§2.2.4). The eyebrow reuses the spec's own name for this
            content (§5.2 item 5, "Fragrance notes"); the screen's
            formulation micro-label ("Notes du Formulateur • Grasse,
            Automne") is product-specific mockup copy with no field behind
            it, so it is omitted (P5).
          */}
          <div className="lg:col-span-5">
            <p className="text-eyebrow uppercase text-bronze">
              Fragrance notes
            </p>
            <h2
              id="pdp-philosophy-heading"
              className="mt-4 font-display text-h2"
            >
              The Philosophy of the Scent
            </h2>
            <p className="mt-6 max-w-(--measure) text-body-lg text-ink-secondary">
              {product.description}
            </p>
          </div>

          {/*
            The figure: the same single product image, re-framed at the
            screen's aspect and captioned with the product's own name and
            volume (real data; the screen's "Fig. 04 — Matières
            Premières" material caption has no field behind it, so the
            caption slot ships the truth or nothing). It carries this
            page's one accent moment (P2): the story drift, the only
            element that keeps pace with the prose beside it (§7,
            STITCH §2 row 3), moved here with the prose it serves.
          */}
          <div className="lg:col-span-7">
            <div className="story-drift relative aspect-[4/3] overflow-hidden bg-hairline">
              <ProductImage
                src={product.image}
                alt={product.name}
                sizes="(min-width: 1024px) 58vw, 100vw"
              />
            </div>
            <p className="mt-4 text-small text-ink-secondary">
              {product.name} · {product.size} ml
            </p>
          </div>
        </section>
      )}

      {/*
        Wear & care trio (screen block "WEAR GUIDANCE & TECHNICAL
        PROTOCOLS", U-W15-T18): layout from the screen, copy universal
        fragrance-knowledge guidance only — see the component's own
        header comment for the removed product-specific claims.
      */}
      <ProtocolTrio />

      {/*
        The screen's "GARANTIE D'INTÉGRITÉ" band is deliberately NOT
        shipped (U-W15-T18). Every claim in its paragraph is a recorded
        P5 removal (spec §0b.3 placeholder table and the §12.D removal
        rows): carbon-neutral dispatch "across Europe" — the store ships
        across India; the complimentary 2ml vial — no gift mechanism
        exists on OrderItem; the 30-day window — the store's window is
        settings-driven (A-12.D.2) and its number is already stated once,
        in the §5.2 item 6 delivery line inside the purchase panel above;
        wax wrapping and door-side pickup have no field and no policy
        page behind either. What remains true is already said once; a
        band repeating it would trade P2 (one accent, stillness) for a
        second copy of the same sentence. The screen's "Conciergerie
        Particulière" CTA names a service this store does not operate —
        /contact renders the real, settings-driven channels and the footer
        already links it, so no dead concierge control is rendered.
      */}

      {/* §5.2 sticky bottom bar — xs/sm only; `lg:hidden` keeps it and the sticky purchase column from ever being two live buttons at once. */}
      {inStock && (
        <StickyPurchaseBar
          anchorId={PURCHASE_ANCHOR_ID}
          productId={product.id}
          stock={product.stock}
          price={product.price}
        />
      )}
    </div>
  );
}

/**
 * The API-down state (§6: every surface names its error state). Distinct
 * copy from the listing's failure panel and from any empty state: the truth
 * here is that one product did not load, not that the catalogue is empty.
 * A plain anchor forces a real server re-render on retry — no JS island.
 */
function ProductLoadFailed({ slug }: { slug: string }) {
  return (
    <div className="mx-auto max-w-6xl px-4 py-16 text-center sm:px-6">
      <p className="font-display text-2xl tracking-tight">
        We couldn&apos;t load this fragrance.
      </p>
      <p className="mx-auto mt-3 max-w-sm text-ink-secondary">
        The request failed. Retry, or come back in a moment.
      </p>
      <a
        href={`/products/${slug}`}
        className="mt-8 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
      >
        Try again
      </a>
    </div>
  );
}

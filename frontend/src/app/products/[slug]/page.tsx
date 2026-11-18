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

// One definition so the in-flow purchase row and StickyPurchaseBar's observer
// can never drift apart.
const PURCHASE_ANCHOR_ID = "pdp-purchase-anchor";

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

  // 404 is a genuine miss; API errors and 5xx render the failure panel rather
  // than lying with a 404 page for an outage.
  const product = await fetchProduct(slug).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 404) notFound();
    return null;
  });
  if (!product) return <ProductLoadFailed slug={slug} />;

  const image = mediaUrl(product.image);
  const inStock = product.stock > 0;
  // When sold out, the restock form replaces the add-to-cart row entirely.
  const showRestockForm =
    !inStock && templateConfig.featureFlags.restockNotifications;

  const { days } = await getReturnWindow();

  // Description is free text, never parsed into tiers; the pyramid renders
  // only when the server publishes notes.
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

      {/* Only the server's own category; the screen's batch line has no API
          field behind it. */}
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

      {/* LCP element: priority preload on a fixed 4:5 plate, never animated.
          One image per product, so no gallery. */}
      <div className="mt-10 grid items-start gap-8 lg:grid-cols-12 lg:gap-12">
        <div className="relative aspect-[4/5] bg-hairline lg:col-span-7">
          <ProductImage
            src={product.image}
            alt={product.name}
            priority
            sizes="(min-width: 768px) 50vw, 100vw"
          />
        </div>

        {/* The .settle entrance is view()-timeline driven: it is already
            settled above the fold, so the LCP guard holds by construction. */}
        <div className="settle border border-hairline bg-surface p-6 sm:p-8 lg:col-span-5 lg:sticky lg:top-24 lg:self-start">
          <h1 className="font-display text-h1">{product.name}</h1>
          <p className="mt-4 money tabular-nums lining-nums">
            {formatINR(product.price)}
          </p>

          {/* One SKU per product, so the single real volume renders as the
              selected option; no other size is fabricated. */}
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

          {/* Dispatch window from the policy single source; return window only
              when the API publishes one, never a fallback. */}
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

      <OlfactoryPyramid tiers={pyramidTiers} />

      {/* Description prose plus the product's one real image, gated on the
          free-text field itself: with no description this renders nothing. */}
      {hasDescription && (
        <section
          aria-labelledby="pdp-philosophy-heading"
          className="mt-16 border-t border-hairline pt-8 md:mt-24 lg:mt-32 lg:grid lg:grid-cols-12 lg:items-center lg:gap-12"
        >
          {/* The screen's formulation micro-label is mockup copy with no
              field behind it. */}
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

          {/* Caption uses real name and volume; the screen's material caption
              has no field behind it. */}
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

      <ProtocolTrio />

      {/* The screen's integrity band and concierge CTA are not shipped: their
          claims are false here, and the real channels live on /contact. */}

      {/* xs/sm only; lg:hidden prevents two live purchase buttons at once. */}
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

// Distinct copy: one product failed to load, not an empty catalogue. A plain
// anchor forces a server re-render on retry, with no JS island.
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

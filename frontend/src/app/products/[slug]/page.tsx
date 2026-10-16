import Link from "next/link";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import AddToCartButton from "@/components/AddToCartButton";
import ProductImage from "@/components/ProductImage";
import RestockForm from "@/components/RestockForm";
import StickyPurchaseBar from "@/components/StickyPurchaseBar";
import { ApiError } from "@/lib/api";
import { mediaUrl } from "@/lib/config";
import { toJsonLdScriptContent } from "@/lib/json-ld";
import { formatINR } from "@/lib/money";
import { fetchProduct } from "@/lib/products-api";
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

  // §5.2 item 5 — `Product.description` is one free-text field (§12-G10),
  // so there is no structured top/heart/base to typeset and none is invented
  // (the banned three-column icon grid in different clothes). With no prose
  // at all the notes section carries only the delivery line rather than an
  // empty heading over nothing.
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

      <div className="mt-8 grid gap-8 md:grid-cols-2">
        {/*
          §5.2 item 2 — THE image, and the LCP element: one `aspect-[4/5]`
          plate (`--color-hairline`, §2.1.1's canonical plate), the spec's own
          `sizes` string (§10.3 KEEP), `priority` for the preload. It never
          animates: nothing above the fold may move before LCP (§10, §3
          "Never on the hero" — extended to the PDP's bottle by STITCH §2 row
          3, "the bottle itself never moves"). No gallery, no dots, no
          thumbnails (P4: the model has exactly one ImageField).
        */}
        <div className="relative aspect-[4/5] bg-hairline">
          <ProductImage
            src={product.image}
            alt={product.name}
            priority
            sizes="(min-width: 768px) 50vw, 100vw"
          />
        </div>

        {/*
          §5.2 item 3 — purchase block: eyebrow → h1 → price → size → stock
          → action, `lg:sticky lg:top-24` KEEP. The `.settle` entrance is the
          one entrance §5.2:1312-1313 sanctions for this page; because it is
          driven by a view() timeline it has already settled wherever the
          block is above the fold at load (so the LCP guard holds by
          construction) and only plays on handhelds, where the block arrives
          after the bottle.
        */}
        <div className="settle lg:sticky lg:top-24 lg:self-start">
          {/* Eyebrow = the server's own category string (P5: rendered from the API or not at all — never a transcribed ref placeholder). */}
          {product.category ? (
            <p className="text-eyebrow uppercase text-bronze">
              {product.category}
            </p>
          ) : null}
          <h1 className="mt-4 font-display text-h1">{product.name}</h1>
          {/* §2.2.3: money is the money step with tabular lining figures, the same treatment the cards and cart lines use. */}
          <p className="mt-4 money tabular-nums lining-nums">
            {formatINR(product.price)}
          </p>
          {/* P4: one SKU per product — a static size line, never a selector — a second SKU cannot honour. */}
          <p className="mt-1 text-sm tabular-nums lining-nums text-ink-secondary">
            {product.size} ml
          </p>

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
        </div>
      </div>

      {/*
        §5.2 items 5–7 — notes, the delivery line, and the still-life plate
        that carries this page's one accent moment (P2). Two `--measure`
        columns with a `--space-8` gutter (P1's inside-section cap), so the
        band sits left under the bottle instead of stretching: the prose is
        36ch (§2.2.4) and the plate is `--measure` wide (§5.2 item 7), which
        is exactly what "the story image drifting as the notes beside it are
        read" (STITCH §2 row 3) needs to sit beside.

        Plate colour: §5.2 item 7 still names the retired bronze-soft tint,
        but §2.1.1 dropped that token — hairline is its canonical plate and
        the one the 4:5 product plate already ships (ProductCard.tsx:8
        records the succession); referencing the retired name would fail the
        colour-token guard in lib/ui.test.ts.
      */}
      <section className="mt-16 border-t border-hairline pt-8 md:mt-24 lg:mt-32 lg:grid lg:grid-cols-[var(--measure)_var(--measure)] lg:gap-8">
        <div>
          {hasDescription && (
            <>
              <h2 className="font-display text-h2">Fragrance notes</h2>
              {/* §5.2 item 5's default: the description as prose at measure. */}
              <p className="mt-6 max-w-(--measure) text-body-lg text-ink-secondary">
                {product.description}
              </p>
            </>
          )}

          {/*
            §5.2 item 6 — one line, `--text-small`, `--color-ink-muted`
            (§2.1.1's canonical name: ink-secondary). The dispatch figure is
            the policy constant `/shipping` renders verbatim; the return
            figure appears only when the settings API publishes one (G11).
          */}
          <p className="mt-8 text-small text-ink-secondary">
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

        {hasDescription && (
          <div className="story-drift relative mt-8 aspect-square max-w-(--measure) overflow-hidden bg-hairline lg:mt-0 lg:sticky lg:top-24 lg:self-start">
            <ProductImage
              src={product.image}
              alt={product.name}
              sizes="(min-width: 1024px) 36ch, 100vw"
            />
          </div>
        )}
      </section>

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

import Link from "next/link";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import AddToCartButton from "@/components/AddToCartButton";
import ProductImage from "@/components/ProductImage";
import { mediaUrl } from "@/lib/config";
import { formatINR } from "@/lib/money";
import { fetchProduct } from "@/lib/products-api";
import { site } from "@/lib/site";
import type { Product } from "@/lib/types";

type Props = { params: Promise<{ slug: string }> };

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

  let product: Product;
  try {
    product = await fetchProduct(slug);
  } catch {
    notFound();
  }

  const image = mediaUrl(product.image);
  const inStock = product.stock > 0;

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
        dangerouslySetInnerHTML={{ __html: JSON.stringify(productJsonLd) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(breadcrumbJsonLd) }}
      />

      <nav aria-label="Breadcrumb" className="text-sm text-ink-muted">
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

      <div className="mt-8 grid gap-10 md:grid-cols-2">
        <div className="relative aspect-[4/5] bg-bronze-soft">
          <ProductImage
            src={product.image}
            alt={product.name}
            priority
            sizes="(min-width: 768px) 50vw, 100vw"
          />
        </div>

        <div className="lg:sticky lg:top-24 lg:self-start">
          <p className="text-xs uppercase tracking-[0.2em] text-bronze">
            Eau de parfum
          </p>
          <h1 className="mt-3 font-display text-4xl tracking-tight md:text-5xl">
            {product.name}
          </h1>
          <p className="mt-4 text-lg">{formatINR(product.price)}</p>
          <p className="mt-1 text-sm text-ink-muted">{product.size} ml</p>

          <p className="mt-6 flex items-center gap-2 text-sm">
            {inStock ? (
              <>
                <span
                  aria-hidden="true"
                  className="inline-block h-1.5 w-1.5 rounded-full bg-bronze"
                />
                In stock
              </>
            ) : (
              <span className="text-ink-muted">Sold out</span>
            )}
          </p>

          <div className="mt-8">
            <AddToCartButton productId={product.id} stock={product.stock} />
          </div>

          <div className="mt-10 border-t border-line pt-8">
            <h2 className="font-display text-xl tracking-tight">
              About this fragrance
            </h2>
            <p className="mt-4 leading-relaxed text-ink-muted">
              {product.description}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

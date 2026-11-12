import Link from "next/link";
import { formatINR } from "@/lib/money";
import type { Product } from "@/lib/types";
import ProductImage from "./ProductImage";

/**
 * The API exposes only `size`, so the screen's lot/concentration slots are
 * deliberately omitted rather than invented. GridSkeleton mirrors the 3:4
 * plate so the fallback cannot cause the CLS it exists to prevent.
 */
export default function ProductCard({
  product,
  priority,
}: {
  product: Product;
  priority?: boolean;
}) {
  const soldOut = product.stock <= 0;

  return (
    <Link
      href={`/products/${product.slug}`}
      className="card-lift group block border border-hairline bg-surface p-3 transition-colors hover:border-hairline-hover sm:p-4"
    >
      <div className="relative aspect-[3/4] overflow-hidden bg-hairline">
        <ProductImage
          src={product.image}
          alt={product.name}
          priority={priority}
          sizes="(min-width: 1024px) 25vw, (min-width: 768px) 33vw, 50vw"
          className="transition-transform duration-(--motion-hover-dur) group-hover:scale-(--hover-scale)"
        />
        {soldOut && (
          <span className="absolute left-3 top-3 bg-ink px-2 py-1 text-eyebrow uppercase text-paper">
            Sold out
          </span>
        )}
      </div>
      <div className="mt-4 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 className="font-display text-body-lg transition-colors duration-(--motion-state-dur) group-hover:text-bronze">
          {product.name}
        </h3>
        {/* §2.2.3: money is tabular so the figure cannot jitter. */}
        <p className="money shrink-0 tabular-nums lining-nums">
          {formatINR(product.price)}
        </p>
      </div>
      {/* Screen meta line: hairline rule, `{size} ml` in secondary ink;
          §2.2.3 volumes are tabular too. */}
      <p className="mt-2 border-t border-hairline pt-2 text-sm tabular-nums lining-nums text-ink-secondary">
        {product.size} ml
      </p>
    </Link>
  );
}

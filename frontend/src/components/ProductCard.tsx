import Link from "next/link";
import { formatINR } from "@/lib/money";
import type { Product } from "@/lib/types";
import ProductImage from "./ProductImage";

export default function ProductCard({
  product,
  priority,
}: {
  product: Product;
  priority?: boolean;
}) {
  const soldOut = product.stock <= 0;

  return (
    <Link href={`/products/${product.slug}`} className="group block">
      <div className="relative aspect-[4/5] overflow-hidden bg-bronze-soft">
        <ProductImage
          src={product.image}
          alt={product.name}
          priority={priority}
          sizes="(min-width: 1024px) 25vw, (min-width: 640px) 33vw, 50vw"
          className="transition-transform duration-300 group-hover:scale-[1.02]"
        />
        {soldOut && (
          <span className="absolute left-3 top-3 bg-ink px-2 py-1 text-xs uppercase tracking-wide text-paper">
            Sold out
          </span>
        )}
      </div>
      <div className="mt-3 flex items-baseline justify-between gap-3">
        <h3 className="font-display text-lg tracking-tight">
          {product.name}
        </h3>
        <p className="shrink-0 text-sm text-ink-muted">
          {formatINR(product.price)}
        </p>
      </div>
      <p className="mt-0.5 text-sm text-ink-muted">{product.size} ml</p>
    </Link>
  );
}

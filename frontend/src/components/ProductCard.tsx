import Link from "next/link";
import { formatINR } from "@/lib/money";
import type { Product } from "@/lib/types";
import ProductImage from "./ProductImage";

/**
 * §5.2 card, in the spec's own hierarchy: a 4:5 image box on the hairline
 * plate (the canonical successor of the pre-refs bronze-soft tint — commit
 * 6109b94; §2.1.1 has no bronze-soft), `h3` name in the display face, price
 * right-aligned in the money step of the §2.2.2 scale with tabular lining
 * figures, and `{size} ml` beneath in secondary ink. No star rating, no
 * "% off", no badge except the `--color-ink` "Sold out" chip: none of those
 * has data behind it (§5.2, P5 — one image, one size, one server price).
 *
 * Hover: `.card-lift` (§0 CHANGE-6, transform only) plus the image scale from
 * the `hover` intent tokens — the card's live-target feedback (§5.2 Motion).
 *
 * `sizes`: §10.3 requires it re-verified whenever the grid changes, and this
 * card now sits under §5.2's 2/2/3/4 — two columns until `md`, so the 33vw
 * step moves from 640px to 768px. That string is also what §4 mandates
 * ("sm … still 2 product columns"); the one grid still breaking it is home's
 * `sm:grid-cols-3` (§5.1's task), which under-serves this value only between
 * 640–767px by fetching the larger candidate for a narrower slot — a bounded,
 * one-breakpoint cost, and the alternative (keeping 640px) would hand the
 * listing a blurry 33vw image in a 50vw slot for every wider viewport.
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
      className="card-lift group block border border-transparent p-3 transition-colors hover:border-hairline-hover"
    >
      <div className="relative aspect-[4/5] overflow-hidden bg-hairline">
        <ProductImage
          src={product.image}
          alt={product.name}
          priority={priority}
          sizes="(min-width: 1024px) 25vw, (min-width: 768px) 33vw, 50vw"
          className="transition-transform duration-(--motion-hover-dur) group-hover:scale-(--hover-scale)"
        />
        {soldOut && (
          <span className="absolute left-3 top-3 bg-ink px-2 py-1 text-xs uppercase tracking-wide text-paper">
            Sold out
          </span>
        )}
      </div>
      <div className="mt-4 flex items-baseline justify-between gap-3">
        <h3 className="font-display text-lg tracking-tight">{product.name}</h3>
        {/* §2.2.3: money is tabular so the figure cannot jitter. */}
        <p className="money shrink-0 tabular-nums lining-nums">
          {formatINR(product.price)}
        </p>
      </div>
      {/* §5.2: `{size} ml` in secondary ink; §2.2.3: volumes are tabular too. */}
      <p className="mt-0.5 text-sm tabular-nums lining-nums text-ink-secondary">
        {product.size} ml
      </p>
    </Link>
  );
}

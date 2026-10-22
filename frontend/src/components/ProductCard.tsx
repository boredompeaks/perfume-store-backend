import Link from "next/link";
import { formatINR } from "@/lib/money";
import type { Product } from "@/lib/types";
import ProductImage from "./ProductImage";

/**
 * The /products card, restyled to the screen (U-W11-T5, design override D1:
 * `media/designs/maison_ther_all_fragrances_refined_cards_atmosphere` —
 * screen.png + code.html are the visual truth; DESIGN.md is the system).
 * Anatomy, in the screen's order: a white `--color-surface` plate inside a
 * `--color-hairline` frame (p-3 sm:p-4, 0 radius, no shadow — DESIGN.md's
 * "Card Hover Elevation" bans the drop shadow the screen's CSS carries and
 * keeps the 2px lift + hairline darken), a 3:4 image plate, the `h3` name in
 * the display face with the ₹ figure right-aligned on the same row (screen
 * lines 137–144), then the screen's hairline rule with `{size} ml` beneath.
 *
 * The screen's lot/concentration slot ("50 ml Extrait … Lot 01") degrades to
 * the size line alone: the API exposes `size` only, and inventing lot numbers
 * is exactly what P5 forbids — logged as a P2 in BACKEND_REQUESTS.md rather
 * than faked here.
 *
 * Hover: `.card-lift` (§0 CHANGE-6, transform only, --motion-hover-dur) plus
 * the border darkening to `--color-hairline-hover` (D2's 3.90:1 override of
 * the ref's #B8AF9F) and the name shift to bronze — the screen's hover-to-
 * secondary name colour, expressed on our bronze token. The name colour
 * transition rides `--motion-state-dur`, so reduced motion gets it instantly.
 *
 * `sizes`: unchanged string, still correct for §5.2's 2/2/3/4 grid (25vw /
 * 33vw / 50vw); the aspect change from 4:5 to 3:4 does not move the column
 * breakpoints. `GridSkeleton` mirrors the new 3:4 plate so the fallback
 * cannot cause the CLS it exists to prevent.
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

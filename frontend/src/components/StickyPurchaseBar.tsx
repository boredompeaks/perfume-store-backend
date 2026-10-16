"use client";

import { useEffect, useState } from "react";
import { formatINR } from "@/lib/money";
import AddToCartButton from "./AddToCartButton";

/**
 * §5.2 (CHANGE, PLAN.md §4) — the xs/sm bottom purchase bar: price left,
 * add-to-cart right, `--z-sticky`, `env(safe-area-inset-bottom)`, and
 * `lg:hidden` so the sticky purchase column takes over at lg — §5.2's own
 * rule: "never both at once".
 *
 * The page passes the in-flow purchase row's anchor id. An
 * IntersectionObserver mounts the bar only once that anchor has scrolled
 * ABOVE the viewport and unmounts it the moment the in-flow button is back
 * in view ("appears once the page scrolls past the in-flow purchase block
 * and hides again when the in-flow button is in view"), so there is never a
 * frame with two add-to-cart controls on screen.
 *
 * The action IS `AddToCartButton` — the same cart mutation path, the same
 * toast, the same double-submit guard, the same server-enforced stock. No
 * second money or stock rule exists here (P5). The price is the server's
 * string through `formatINR`, verbatim.
 *
 * No motion (P2): the page's one accent is the story-image drift (§5.2
 * "Accent moment"), and a bar that slides would be a second moving thing
 * the spec does not ask for — it is there, or it is not.
 *
 * Sold-out products never reach this component: the page gates it on
 * `inStock`, because with `stock <= 0` the in-flow row is the restock form
 * and a bar offering "Add to cart" would contradict it.
 */
export default function StickyPurchaseBar({
  anchorId,
  productId,
  stock,
  price,
}: {
  anchorId: string;
  productId: number;
  stock: number;
  price: string;
}) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const anchor = document.getElementById(anchorId);
    if (!anchor || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => {
      // `top < 0` means the anchor is entirely above the viewport, so the
      // bar never appears before the customer has passed the real button —
      // and `isIntersecting` hides it again as soon as that button returns.
      setVisible(!entry.isIntersecting && entry.boundingClientRect.top < 0);
    });
    observer.observe(anchor);
    return () => observer.disconnect();
  }, [anchorId]);

  useEffect(() => {
    // While the bar is up, reserve its height at the END of the document so
    // it can never sit on top of the footer's last row. The padding rule is
    // scoped below `lg` in globals.css — at lg the bar itself does not exist.
    if (!visible) return;
    document.body.classList.add("pdp-buybar-open");
    return () => document.body.classList.remove("pdp-buybar-open");
  }, [visible]);

  if (!visible) return null;

  return (
    <div
      className="fixed inset-x-0 bottom-0 z-(--z-sticky) border-t border-hairline bg-surface px-4 pt-3 sm:px-6 lg:hidden"
      style={{
        paddingBottom: "calc(var(--space-4) + env(safe-area-inset-bottom))",
      }}
    >
      <div className="mx-auto flex max-w-6xl items-center gap-4">
        <p className="money shrink-0 tabular-nums lining-nums">
          {formatINR(price)}
        </p>
        <div className="min-w-0 flex-1">
          <AddToCartButton productId={productId} stock={stock} />
        </div>
      </div>
    </div>
  );
}

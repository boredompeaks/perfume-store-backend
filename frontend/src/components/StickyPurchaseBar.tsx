"use client";

import { useEffect, useState } from "react";
import { formatINR } from "@/lib/money";
import AddToCartButton from "./AddToCartButton";

/**
 * Mounts only once the in-flow purchase row has scrolled above the viewport,
 * so two add-to-cart controls are never on screen at once.
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
      // `top < 0` means the anchor is above the viewport: the bar never
      // appears before the real button, and hides as soon as it returns.
      setVisible(!entry.isIntersecting && entry.boundingClientRect.top < 0);
    });
    observer.observe(anchor);
    return () => observer.disconnect();
  }, [anchorId]);

  useEffect(() => {
    // Reserve the bar's height at the END of the document so it never covers
    // the footer's last row; the padding rule is scoped below lg.
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

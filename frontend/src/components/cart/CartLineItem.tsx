"use client";

import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { ApiError } from "@/lib/api";
import { deleteCartItem, patchCartItem } from "@/lib/cart-api";
import { formatINR } from "@/lib/money";
import type { Cart, CartItem } from "@/lib/types";
import ProductImage from "../ProductImage";
import { useToast } from "../toast";

/**
 * §3 "Cart - line removed": the removal View Transition. The browser
 * re-lays-out and cross-fades the named lines — R1 forbids the hand-written
 * `height` animation this replaces. Instant reflow whenever the browser has
 * no `startViewTransition`, and always under `prefers-reduced-motion`: the
 * UA's transition animations read no `--motion-*` token, so the media query
 * in this wrapper is the layer that can reach them.
 */
function withRemovalTransition(update: () => void): void {
  const doc = document as Document & {
    startViewTransition?: (updateCallback: () => void) => unknown;
  };
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || typeof doc.startViewTransition !== "function") {
    update();
    return;
  }
  try {
    // flushSync so the new DOM exists before the snapshot is taken; without
    // it the transition would capture the row that is about to disappear.
    doc.startViewTransition(() => flushSync(update));
  } catch {
    update();
  }
}

export default function CartLineItem({
  item,
  vtName,
}: {
  item: CartItem;
  /** Unique `view-transition-name` for this row; absent above the §3 cap. */
  vtName?: string;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [qty, setQty] = useState(item.quantity);
  const [lineError, setLineError] = useState<string | null>(null);

  // Resync the stepper whenever server truth changes (incl. after an
  // over-stock rejection rolls us back).
  useEffect(() => {
    setQty(item.quantity);
  }, [item.quantity]);

  const applyCart = (cart: Cart) => {
    queryClient.setQueryData(["cart"], cart);
  };

  const patch = useMutation({
    mutationFn: (quantity: number) => patchCartItem(item.id, quantity),
    onSuccess: (cart) => {
      applyCart(cart);
      setLineError(null);
    },
    onError: (error) => {
      setLineError(
        error instanceof ApiError
          ? error.message
          : "Could not update quantity.",
      );
      // Reconcile against the server — the local stepper may have drifted.
      queryClient.invalidateQueries({ queryKey: ["cart"] });
    },
  });

  const remove = useMutation({
    mutationFn: () => deleteCartItem(item.id),
    onSuccess: (cart) => {
      withRemovalTransition(() => applyCart(cart));
      toast("Removed from cart");
    },
    onError: (error) => {
      setLineError(
        error instanceof ApiError ? error.message : "Could not remove item.",
      );
      queryClient.invalidateQueries({ queryKey: ["cart"] });
    },
  });

  const maxQty = Math.max(1, item.product.stock);
  const busy = patch.isPending || remove.isPending;

  return (
    <div
      className="flex gap-4 py-6 first:pt-0"
      style={vtName ? { viewTransitionName: vtName } : undefined}
    >
      <Link
        href={`/products/${item.product.slug}`}
        className="relative h-30 w-24 shrink-0 bg-hairline"
        aria-label={item.product.name}
      >
        <ProductImage
          src={item.product.image}
          alt={item.product.name}
          sizes="96px"
        />
      </Link>

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <Link
              href={`/products/${item.product.slug}`}
              className="font-display text-lg tracking-tight transition-colors hover:text-bronze"
            >
              {item.product.name}
            </Link>
            <p className="mt-0.5 text-sm tabular-nums text-ink-secondary">
              {item.product.size} ml · {formatINR(item.product.price)} each
            </p>
          </div>
          <p className="shrink-0 text-money tabular-nums">
            {formatINR(Number(item.product.price) * qty)}
          </p>
        </div>

        <div
          className={`mt-auto flex items-center justify-between pt-3 ${busy ? "opacity-60" : ""}`}
        >
          <div
            className="flex items-center border border-field-border"
            role="group"
            aria-label={`Quantity for ${item.product.name}`}
          >
            {/* §7.5 — same 44px touch floor as the PDP stepper: `px-3 py-1.5`
                was ~32×32, under the system's 44px convention (WCAG's own
                24px minimum still passed). */}
            <button
              type="button"
              onClick={() => {
                const next = Math.max(1, qty - 1);
                setQty(next);
                if (next !== item.quantity) patch.mutate(next);
              }}
              disabled={qty <= 1 || busy}
              aria-label="Decrease quantity"
              className="flex h-11 min-w-11 items-center justify-center text-ink-secondary transition-colors hover:text-ink disabled:opacity-40"
            >
              −
            </button>
            {/* §5.3 "stepper value cross-fade": the key remounts the value so
                the enter-token fade replays on every step. Reduced motion
                drops the rule at the media layer, so the number is simply
                there. */}
            <span
              key={qty}
              className="qty-crossfade w-8 text-center text-sm tabular-nums"
            >
              {qty}
            </span>
            <button
              type="button"
              onClick={() => {
                const next = Math.min(maxQty, qty + 1);
                setQty(next);
                if (next !== item.quantity) patch.mutate(next);
              }}
              disabled={qty >= maxQty || busy}
              aria-label="Increase quantity"
              className="flex h-11 min-w-11 items-center justify-center text-ink-secondary transition-colors hover:text-ink disabled:opacity-40"
            >
              +
            </button>
          </div>

          <button
            type="button"
            onClick={() => remove.mutate()}
            disabled={busy}
            className="text-sm text-ink-secondary underline underline-offset-4 transition-colors hover:text-bronze disabled:opacity-40"
          >
            {remove.isPending ? "Removing…" : "Remove"}
          </button>
        </div>

        {lineError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {lineError}
          </p>
        )}
      </div>
    </div>
  );
}

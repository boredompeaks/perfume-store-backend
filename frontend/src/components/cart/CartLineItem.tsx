"use client";

import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { deleteCartItem, patchCartItem } from "@/lib/cart-api";
import { cartQueryKey } from "@/lib/use-cart";
import { formatINR } from "@/lib/money";
import type { Cart, CartItem } from "@/lib/types";
import ProductImage from "../ProductImage";
import { useToast } from "../toast";

/**
 * View Transition for row removal. The UA's transition animation reads no
 * `--motion-*` token, so the reduced-motion check has to live here.
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
    // flushSync so the new DOM exists before the snapshot is taken; otherwise
    // the transition captures the row that is about to disappear.
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
  /** Unique `view-transition-name`; absent above the snapshot cap. */
  vtName?: string;
}) {
  const queryClient = useQueryClient();
  const { status } = useAuth();
  const toast = useToast();
  const [qty, setQty] = useState(item.quantity);
  const [lineError, setLineError] = useState<string | null>(null);

  // Resync the local stepper whenever server truth changes.
  useEffect(() => {
    setQty(item.quantity);
  }, [item.quantity]);

  const applyCart = (cart: Cart) => {
    // The cart cache is keyed by identity: write into the slot this render
    // read from, not an unkeyed ["cart"] entry no observer would see.
    queryClient.setQueryData(cartQueryKey(status), cart);
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
      // Reconcile against the server; the local stepper may have drifted.
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
      className="flex flex-col gap-4 py-8 sm:flex-row sm:items-center"
      style={vtName ? { viewTransitionName: vtName } : undefined}
    >
      {/* No hover zoom: the screen's 500ms scale exceeds the system's
          hover-motion cap, so it is deliberately cut. */}
      <Link
        href={`/products/${item.product.slug}`}
        className="relative h-32 w-24 shrink-0 border border-hairline bg-hairline sm:h-36 sm:w-28"
        aria-label={item.product.name}
      >
        <ProductImage
          src={item.product.image}
          alt={item.product.name}
          sizes="112px"
        />
      </Link>

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            {/* The API exposes category (scent family), not concentration, so
                the variant line degrades to size and unit price. */}
            <Link
              href={`/products/${item.product.slug}`}
              className="font-display text-h3 tracking-tight transition-colors hover:text-bronze"
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
            {/* Keyed so the cross-fade replays on every step; reduced motion
                drops the rule at the media layer. */}
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

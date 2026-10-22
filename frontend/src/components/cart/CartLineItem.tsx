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
  const { status } = useAuth();
  const toast = useToast();
  const [qty, setQty] = useState(item.quantity);
  const [lineError, setLineError] = useState<string | null>(null);

  // Resync the stepper whenever server truth changes (incl. after an
  // over-stock rejection rolls us back).
  useEffect(() => {
    setQty(item.quantity);
  }, [item.quantity]);

  const applyCart = (cart: Cart) => {
    // The cart cache is keyed by identity (D-04b): write the server's answer
    // into the slot this render read from, not an unkeyed ["cart"] entry no
    // observer would see.
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
    // Screen row anatomy (U-W12-T7, design override D1: maison_ther_votre_
    // panier): thumbnail plate left, serif name + variant line + money
    // right, then the stepper/Remove row beneath - stacked on phones and
    // side-by-side from sm, exactly as the screen's flex-col/sm:flex-row
    // does. py-8 matches the screen's line rhythm; the ledger's top rule
    // comes from the list container (CartView), so no first-row exception
    // is needed. Equal-height defence per xias components.md "Form rows:
    // equal controls, unequal content": the stepper group's cells share the
    // h-11 floor, and the money figure is shrink-0 so a long name cannot
    // stretch the row past its neighbour's height.
    <div
      className="flex flex-col gap-4 py-8 sm:flex-row sm:items-center"
      style={vtName ? { viewTransitionName: vtName } : undefined}
    >
      {/* Screen plate: 96x128 mobile, 112x144 at sm (D1 overrides §5.3's
          96x120 - "thumbnail plate" is an enumerated parity target), with
          the screen's hairline frame on the canonical --color-hairline
          plate. No hover zoom: the screen's scale-1.04/500ms exceeds the
          system's hover cap (<=240ms, <=1.03 - ui-motion), so it is cut as
          decoration rather than shipped. */}
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
            {/* Screen headline-sm is 20px Playfair = the --text-h3 step;
                the screen's hover-to-secondary is our bronze. The variant
                line keeps §5.3's KEEP'd "{size} ml · ₹x each" - the
                screen's concentration word ("Extrait de Parfum") has no API
                field (ProductSerializer exposes category, a scent family,
                not a concentration), the same degrade ProductCard made in
                U-W11-T5, already logged as BACKEND_REQUESTS P2 "Product lot
                code + concentration". */}
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

"use client";

import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { deleteCartItem, patchCartItem } from "@/lib/cart-api";
import { formatINR } from "@/lib/money";
import type { Cart, CartItem } from "@/lib/types";
import ProductImage from "../ProductImage";
import { useToast } from "../toast";

export default function CartLineItem({ item }: { item: CartItem }) {
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
      applyCart(cart);
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
    <div className="flex gap-4 border-b border-line py-6 first:pt-0">
      <Link
        href={`/products/${item.product.slug}`}
        className="relative aspect-[4/5] h-28 w-24 shrink-0 bg-bronze-soft"
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
            <p className="mt-0.5 text-sm text-ink-muted">
              {item.product.size} ml · {formatINR(item.product.price)} each
            </p>
          </div>
          <p className="shrink-0 text-sm">
            {formatINR(Number(item.product.price) * qty)}
          </p>
        </div>

        <div
          className={`mt-auto flex items-center justify-between pt-3 ${busy ? "opacity-60" : ""}`}
        >
          <div
            className="flex items-center border border-line"
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
              className="px-3 py-1.5 text-ink-muted transition-colors hover:text-ink disabled:opacity-40"
            >
              −
            </button>
            <span className="w-8 text-center text-sm">{qty}</span>
            <button
              type="button"
              onClick={() => {
                const next = Math.min(maxQty, qty + 1);
                setQty(next);
                if (next !== item.quantity) patch.mutate(next);
              }}
              disabled={qty >= maxQty || busy}
              aria-label="Increase quantity"
              className="px-3 py-1.5 text-ink-muted transition-colors hover:text-ink disabled:opacity-40"
            >
              +
            </button>
          </div>

          <button
            type="button"
            onClick={() => remove.mutate()}
            disabled={busy}
            className="text-sm text-ink-muted underline underline-offset-4 transition-colors hover:text-bronze disabled:opacity-40"
          >
            {remove.isPending ? "Removing…" : "Remove"}
          </button>
        </div>

        {lineError && (
          <p role="alert" className="mt-2 text-sm text-bronze">
            {lineError}
          </p>
        )}
      </div>
    </div>
  );
}

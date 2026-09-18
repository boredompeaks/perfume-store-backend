"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { addToCart } from "@/lib/cart-api";
import { useToast } from "./toast";

/**
 * Client island on the PDP. Stock is enforced server-side; the stepper cap is
 * a courtesy — the real answer is the backend's "Not enough stock" 400.
 */
export default function AddToCartButton({
  productId,
  stock,
}: {
  productId: number;
  stock: number;
}) {
  const [qty, setQty] = useState(1);
  const [justAdded, setJustAdded] = useState(false);
  const addedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const queryClient = useQueryClient();
  const toast = useToast();

  const maxQty = Math.max(1, stock);
  const soldOut = stock <= 0;

  useEffect(() => {
    return () => {
      if (addedTimer.current) clearTimeout(addedTimer.current);
    };
  }, []);

  const mutation = useMutation({
    mutationFn: () => addToCart(productId, qty),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["cart"] });
      setJustAdded(true);
      if (addedTimer.current) clearTimeout(addedTimer.current);
      addedTimer.current = setTimeout(() => setJustAdded(false), 1500);
      toast("Added to cart", { label: "View cart", href: "/cart" });
    },
    onError: (error) => {
      toast(
        error instanceof ApiError ? error.message : "Could not add to cart",
      );
    },
  });

  if (soldOut) {
    return <p className="text-sm text-ink-muted">Sold out</p>;
  }

  return (
    <div className="flex items-stretch gap-3">
      <div
        className="flex items-center border border-line"
        role="group"
        aria-label="Quantity"
      >
        <button
          type="button"
          onClick={() => setQty((q) => Math.max(1, q - 1))}
          disabled={qty <= 1}
          aria-label="Decrease quantity"
          className="px-3 py-2 text-ink-muted transition-colors hover:text-ink disabled:opacity-40"
        >
          −
        </button>
        <span aria-live="polite" className="w-8 text-center text-sm">
          {qty}
        </span>
        <button
          type="button"
          onClick={() => setQty((q) => Math.min(maxQty, q + 1))}
          disabled={qty >= maxQty}
          aria-label="Increase quantity"
          className="px-3 py-2 text-ink-muted transition-colors hover:text-ink disabled:opacity-40"
        >
          +
        </button>
      </div>

      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || justAdded}
        className="flex-1 bg-ink px-6 py-2 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-70 sm:min-w-48"
      >
        {mutation.isPending
          ? "Adding…"
          : justAdded
            ? "Added"
            : "Add to cart"}
      </button>
    </div>
  );
}

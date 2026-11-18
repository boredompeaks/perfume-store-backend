"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { applyCartCoupon, removeCartCoupon } from "@/lib/cart-api";
import { useCart } from "@/lib/use-cart";
import { formatINR } from "@/lib/money";
import { applyCoupon } from "@/lib/orders-api";
import type { CouponPreview } from "@/lib/types";
import { inputClass } from "@/lib/ui";

/**
 * The persisted cart coupon is the only applied state; every displayed number
 * comes verbatim from the preview response, never client math.
 */
export default function CouponForm({
  onCodeChange,
}: {
  onCodeChange?: (code: string | null) => void;
}) {
  const queryClient = useQueryClient();
  // Shared identity-keyed cart entry, gated on the auth bootstrap; opening a
  // second unkeyed ["cart"] slot would escape invalidation.
  const { data: cart } = useCart();
  const [code, setCode] = useState("");
  const [applied, setApplied] = useState<{
    code: string;
    preview: CouponPreview | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bootstrapped = useRef(false);

  // Preview computes the server numbers, then persist the code. "Applied" is
  // only set after the persist succeeds, so it can never claim a coupon the
  // cart doesn't hold.
  const apply = useMutation({
    mutationFn: async (value: string) => {
      const preview = await applyCoupon(value);
      const cartBody = await applyCartCoupon(value);
      return { preview, cartBody };
    },
    onSuccess: ({ preview }) => {
      setApplied({ code: preview.coupon, preview });
      setError(null);
      setCode("");
      // Prefix invalidation reaches the identity-keyed ["cart", status] entry.
      queryClient.invalidateQueries({ queryKey: ["cart"] });
      onCodeChange?.(preview.coupon);
    },
    onError: (err) => {
      setApplied(null);
      if (err instanceof ApiError) {
        const min = err.extras?.minimum_order_amount;
        if (typeof min === "string" || typeof min === "number") {
          setError(
            `Spend at least ${formatINR(min as string)} to use this coupon.`,
          );
        } else {
          setError(err.message);
        }
      } else {
        setError("Could not check this coupon. Try again.");
      }
      onCodeChange?.(null);
    },
  });

  const remove = useMutation({
    mutationFn: removeCartCoupon,
    onSuccess: () => {
      setApplied(null);
      setError(null);
      setCode("");
      queryClient.invalidateQueries({ queryKey: ["cart"] });
      onCodeChange?.(null);
    },
    onError: (err) => {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not remove this coupon. Try again.",
      );
    },
  });

  // A persisted coupon renders as applied; one preview call fetches its
  // numbers. If that fails the code stays without numbers; checkout
  // re-validates it.
  useEffect(() => {
    if (bootstrapped.current || cart === undefined) return;
    bootstrapped.current = true;
    const persisted = cart.coupon_code;
    if (!persisted) return;
    setApplied({ code: persisted, preview: null });
    onCodeChange?.(persisted);
    applyCoupon(persisted)
      .then((preview) => setApplied({ code: persisted, preview }))
      .catch(() => {
        // Persisted-but-unpreviewable: keep the code, omit the numbers.
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cart]);

  if (applied) {
    return (
      <div className="border border-hairline p-4 text-sm">
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium uppercase tracking-wide">
            {applied.code} applied
          </span>
          <button
            type="button"
            onClick={() => remove.mutate()}
            disabled={remove.isPending}
            className="text-ink-secondary underline underline-offset-4 transition-colors hover:text-bronze disabled:opacity-50"
          >
            {remove.isPending ? "Removing…" : "Remove"}
          </button>
        </div>
        {applied.preview && (
          <div className="mt-3 space-y-1 text-ink-secondary">
            <p>Discount: −{formatINR(applied.preview.discount)}</p>
            <p className="text-ink">
              Total after coupon: {formatINR(applied.preview.final_total)}
            </p>
          </div>
        )}
        <p className="mt-3 text-xs text-ink-secondary">
          The coupon is re-validated server-side at checkout.
        </p>
        {error && (
          <p role="alert" className="mt-2 text-sm text-bronze">
            {error}
          </p>
        )}
      </div>
    );
  }

  return (
    <div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (code.trim()) apply.mutate(code.trim());
        }}
        className="flex gap-2"
      >
        <label htmlFor="coupon-code" className="sr-only">
          Coupon code
        </label>
        <input
          id="coupon-code"
          type="text"
          value={code}
          aria-describedby={error ? "coupon-code-err" : undefined}
          onChange={(e) => setCode(e.target.value)}
          placeholder="Coupon code"
          autoComplete="off"
          className={`${inputClass} min-w-0 flex-1`}
        />
        <button
          type="submit"
          disabled={apply.isPending || !code.trim()}
          className="shrink-0 border border-ink px-4 py-2 text-sm transition-colors hover:bg-ink hover:text-paper disabled:opacity-50"
        >
          {apply.isPending ? "Checking…" : "Apply"}
        </button>
      </form>
      {error && (
        <p id="coupon-code-err" role="alert" className="mt-2 text-sm text-bronze">
          {error}
        </p>
      )}
    </div>
  );
}

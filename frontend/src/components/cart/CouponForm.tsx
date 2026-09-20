"use client";

import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { formatINR } from "@/lib/money";
import { applyCoupon } from "@/lib/orders-api";
import type { CouponPreview } from "@/lib/types";
import { inputClass } from "@/lib/ui";

const COUPON_KEY = "aurel.coupon";

/**
 * Server truth only: whatever is displayed here comes from the
 * apply-coupon response, never from client math. The applied code is kept
 * in sessionStorage so it can carry to checkout (where the server
 * re-validates it for real).
 */
export default function CouponForm({
  onCodeChange,
}: {
  onCodeChange?: (code: string | null) => void;
}) {
  const [code, setCode] = useState("");
  const [preview, setPreview] = useState<CouponPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bootstrapped = useRef(false);

  const mutation = useMutation({
    mutationFn: (value: string) => applyCoupon(value),
    onSuccess: (data) => {
      setPreview(data);
      setError(null);
      try {
        sessionStorage.setItem(COUPON_KEY, data.coupon);
      } catch {
        // ignore
      }
      onCodeChange?.(data.coupon);
    },
    onError: (err) => {
      setPreview(null);
      try {
        sessionStorage.removeItem(COUPON_KEY);
      } catch {
        // ignore
      }
      onCodeChange?.(null);
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
    },
  });

  // Revalidate a code carried over from the cart page (one server call on
  // mount — the explicit-submit-only rule applies to keystrokes).
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    let stored: string | null = null;
    try {
      stored = sessionStorage.getItem(COUPON_KEY);
    } catch {
      stored = null;
    }
    if (stored) {
      setCode(stored);
      mutation.mutate(stored);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const remove = () => {
    setPreview(null);
    setCode("");
    try {
      sessionStorage.removeItem(COUPON_KEY);
    } catch {
      // ignore
    }
    onCodeChange?.(null);
  };

  if (preview) {
    return (
      <div className="border border-line p-4 text-sm">
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium uppercase tracking-wide">
            {preview.coupon} applied
          </span>
          <button
            type="button"
            onClick={remove}
            className="text-ink-muted underline underline-offset-4 transition-colors hover:text-bronze"
          >
            Remove
          </button>
        </div>
        <div className="mt-3 space-y-1 text-ink-muted">
          <p>Discount: −{formatINR(preview.discount)}</p>
          <p className="text-ink">
            Total after coupon: {formatINR(preview.final_total)}
          </p>
        </div>
        <p className="mt-3 text-xs text-ink-muted">
          The coupon is re-validated server-side at checkout.
        </p>
      </div>
    );
  }

  return (
    <div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (code.trim()) mutation.mutate(code.trim());
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
          onChange={(e) => setCode(e.target.value)}
          placeholder="Coupon code"
          autoComplete="off"
          className={`${inputClass} min-w-0 flex-1`}
        />
        <button
          type="submit"
          disabled={mutation.isPending || !code.trim()}
          className="shrink-0 border border-ink px-4 py-2 text-sm transition-colors hover:bg-ink hover:text-paper disabled:opacity-50"
        >
          {mutation.isPending ? "Checking…" : "Apply"}
        </button>
      </form>
      {error && (
        <p role="alert" className="mt-2 text-sm text-bronze">
          {error}
        </p>
      )}
    </div>
  );
}

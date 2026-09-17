"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { getCart } from "@/lib/cart-api";
import { formatINR } from "@/lib/money";
import CartLineItem from "./CartLineItem";
import CouponForm from "./CouponForm";

function CartSkeleton() {
  return (
    <div aria-hidden="true" className="space-y-6">
      {[1, 2, 3].map((n) => (
        <div key={n} className="flex gap-4 border-b border-line pb-6">
          <div className="skeleton aspect-[4/5] h-28 w-24" />
          <div className="flex-1 space-y-3 py-1">
            <div className="skeleton h-4 w-1/2" />
            <div className="skeleton h-3 w-1/3" />
            <div className="skeleton mt-6 h-8 w-32" />
          </div>
        </div>
      ))}
    </div>
  );
}

export default function CartView() {
  const { status } = useAuth();
  const { data: cart, isPending, isError, refetch } = useQuery({
    queryKey: ["cart"],
    queryFn: getCart,
    retry: false,
  });

  if (isPending) return <CartSkeleton />;

  if (isError || !cart) {
    return (
      <div className="border border-line p-10 text-center">
        <p className="font-display text-2xl tracking-tight">
          We couldn&apos;t load your cart.
        </p>
        <p className="mx-auto mt-3 max-w-sm text-ink-muted">
          Check your connection and try again.
        </p>
        <button
          type="button"
          onClick={() => refetch()}
          className="mt-6 bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Try again
        </button>
      </div>
    );
  }

  if (cart.items.length === 0) {
    return (
      <div className="border border-line p-16 text-center">
        <p className="font-display text-2xl tracking-tight">
          Your cart is empty.
        </p>
        <p className="mx-auto mt-3 max-w-sm text-ink-muted">
          Nothing here yet — the shelf is one click away.
        </p>
        <Link
          href="/products"
          className="mt-8 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Shop fragrances
        </Link>
      </div>
    );
  }

  // Display arithmetic on server-provided prices only; totals are
  // confirmed server-side at checkout / apply-coupon.
  const subtotal = cart.items.reduce(
    (sum, item) => sum + Number(item.product.price) * item.quantity,
    0,
  );

  return (
    <div className="grid gap-12 lg:grid-cols-[1fr_360px]">
      <div>
        {cart.items.map((item) => (
          <CartLineItem key={item.id} item={item} />
        ))}
      </div>

      <aside className="self-start border border-line p-6 lg:sticky lg:top-24">
        <h2 className="font-display text-xl tracking-tight">Summary</h2>

        <div className="mt-4 flex items-baseline justify-between text-sm">
          <span className="text-ink-muted">Subtotal</span>
          <span>{formatINR(subtotal)}</span>
        </div>
        <p className="mt-1 text-xs text-ink-muted">
          Final totals are confirmed at checkout.
        </p>

        <div className="mt-5 border-t border-line pt-5">
          <CouponForm />
        </div>

        <div className="mt-6 border-t border-line pt-6">
          {status === "authenticated" ? (
            <Link
              href="/checkout"
              className="block bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
            >
              Proceed to checkout
            </Link>
          ) : status === "anonymous" ? (
            <>
              <Link
                href="/login?next=/checkout"
                className="block bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
              >
                Sign in to checkout
              </Link>
              <p className="mt-3 text-center text-xs text-ink-muted">
                Your cart is saved on this device.
              </p>
            </>
          ) : (
            <span
              aria-hidden="true"
              className="block bg-ink px-6 py-3 text-center text-sm text-paper opacity-60"
            >
              …
            </span>
          )}
        </div>
      </aside>
    </div>
  );
}

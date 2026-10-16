"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { getCart } from "@/lib/cart-api";
import { formatINR } from "@/lib/money";
import { templateConfig } from "@/lib/template.config";
import CartLineItem from "./CartLineItem";
import CouponForm from "./CouponForm";

function CartSkeleton() {
  return (
    <div aria-hidden="true" className="space-y-6">
      {[1, 2, 3].map((n) => (
        <div key={n} className="flex gap-4 border-b border-hairline pb-6">
          {/* 96×120 — the same thumb the populated row renders (§5.3). */}
          <div className="skeleton h-30 w-24 shrink-0" />
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

/**
 * §5.3 `/cart` — line items (left) and the sticky summary (right).
 *
 * Money here is display arithmetic on server-provided prices only: the
 * subtotal has no server field yet (BACKEND_REQUESTS P1 "Totals in the cart
 * payload"), so it is labelled an estimate, and the applied coupon's discount
 * and post-coupon total are rendered verbatim by `CouponForm` from the
 * apply-coupon preview. No pricing rule is implemented in this component.
 */
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
      <div className="border border-hairline p-10 text-center">
        <p className="font-display text-2xl tracking-tight">
          We couldn&apos;t load your cart.
        </p>
        <p className="mx-auto mt-3 max-w-sm text-ink-secondary">
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
      <div className="border border-hairline p-16 text-center">
        <p className="font-display text-2xl tracking-tight">
          Your cart is empty.
        </p>
        <p className="mx-auto mt-3 max-w-sm text-ink-secondary">
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

  const subtotal = cart.items.reduce(
    (sum, item) => sum + Number(item.product.price) * item.quantity,
    0,
  );
  const guestCheckout = templateConfig.featureFlags.guestCheckout;

  return (
    <div className="grid gap-12 lg:grid-cols-[1fr_360px]">
      <div className="divide-y divide-hairline">
        {cart.items.map((item) => (
          <CartLineItem
            key={item.id}
            item={item}
            /* §3 "Cart - line removed": one snapshot per visible line, and the
               row's own cap — snapshot memory scales with the captured area. */
            vtName={
              cart.items.length <= 20 ? `cart-line-${item.id}` : undefined
            }
          />
        ))}
      </div>

      <aside className="z-(--z-sticky) self-start border border-field-border p-6 lg:sticky lg:top-24">
        <h2 className="font-display text-xl tracking-tight">Summary</h2>

        <div className="mt-4 flex items-baseline justify-between gap-3 text-sm">
          <span className="text-ink-secondary">
            Subtotal <span className="text-xs">(estimate)</span>
          </span>
          <span className="tabular-nums">{formatINR(subtotal)}</span>
        </div>
        <p className="mt-1 text-xs text-ink-secondary">
          Delivery is charged once a destination is set; totals are confirmed
          at checkout.
        </p>

        {/* The coupon block shows the real discount and the post-coupon total
            from the server preview — §5.3 "the cart can now show the coupon
            discount for real". */}
        <div className="mt-5 border-t border-hairline pt-5">
          <CouponForm />
        </div>

        <div className="mt-6 border-t border-hairline pt-6">
          {status === "authenticated" ? (
            <Link
              href="/checkout"
              className="block bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
            >
              Proceed to checkout
            </Link>
          ) : status === "anonymous" ? (
            guestCheckout ? (
              <>
                {/* §5.3 CTA by auth state (CHANGE-11). CheckoutView owns the
                    sign-in wall when the flag is off, and surfaces the
                    backend's own refusal verbatim when an anonymous order is
                    refused (BACKEND_REQUESTS P1, TIER1-GUEST-DEADEND) — the
                    cart promises the door, not the outcome. */}
                <Link
                  href="/checkout"
                  className="block bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
                >
                  Checkout as guest
                </Link>
                <p className="mt-3 text-center text-xs text-ink-secondary">
                  or{" "}
                  <Link
                    href="/login?next=/checkout"
                    className="link-underline"
                  >
                    sign in
                  </Link>
                </p>
              </>
            ) : (
              <>
                <Link
                  href="/login?next=/checkout"
                  className="block bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
                >
                  Sign in to checkout
                </Link>
                <p className="mt-3 text-center text-xs text-ink-secondary">
                  Your cart is saved on this device.
                </p>
              </>
            )
          ) : (
            <button
              type="button"
              disabled
              className="block w-full bg-ink px-6 py-3 text-center text-sm text-paper opacity-60"
            >
              Proceed to checkout
            </button>
          )}
        </div>
      </aside>
    </div>
  );
}

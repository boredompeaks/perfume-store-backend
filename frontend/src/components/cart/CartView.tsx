"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth";
import { cartCount } from "@/lib/cart-api";
import { useCart } from "@/lib/use-cart";
import { formatINR } from "@/lib/money";
import { templateConfig } from "@/lib/template.config";
import CartLineItem from "./CartLineItem";
import CouponForm from "./CouponForm";

function CartSkeleton() {
  return (
    <div aria-hidden="true">
      {[1, 2, 3].map((n) => (
        <div
          key={n}
          className="flex flex-col gap-4 border-b border-hairline py-8 sm:flex-row"
        >
          {/* Mirrors the populated row's plate so the fallback cannot cause
              the CLS it exists to prevent. */}
          <div className="skeleton h-32 w-24 shrink-0 sm:h-36 sm:w-28" />
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

/** Static rail: mirrors checkout's real steps and never fakes progress. */
function StepRail() {
  return (
    <ol
      aria-label="Purchase steps"
      className="flex flex-wrap items-center gap-x-4 gap-y-2 text-label-sm uppercase"
    >
      {["Cart", "Delivery", "Review", "Payment"].map((label, i) => (
        <li
          key={label}
          aria-current={i === 0 ? "step" : undefined}
          className={
            i === 0 ? "flex items-center gap-2 text-ink" : "text-ink-secondary"
          }
        >
          {i === 0 && (
            <span aria-hidden="true" className="size-1.5 bg-bronze" />
          )}
          {`0${i + 1} ${label}`}
        </li>
      ))}
    </ol>
  );
}

/**
 * The title band renders above the state switch so the h1 ships in every
 * state. The subtotal is display arithmetic over server prices (no server
 * totals field yet), hence the "estimate" label.
 */
export default function CartView() {
  const { status } = useAuth();
  const { data: cart, isPending, isError, refetch } = useCart();
  const unitCount = cart ? cartCount(cart) : 0;
  const subtotal = cart
    ? cart.items.reduce(
        (sum, item) => sum + Number(item.product.price) * item.quantity,
        0,
      )
    : 0;
  const guestCheckout = templateConfig.featureFlags.guestCheckout;

  return (
    <div>
      <div className="border-b border-hairline pb-6">
        <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
          <div>
            <p className="text-eyebrow uppercase text-bronze">
              Revue du panier · Sélection privée
            </p>
            <div className="mt-2 flex flex-wrap items-baseline gap-x-3">
              <h1 className="font-display text-4xl tracking-tight">
                Your cart
              </h1>
              {unitCount > 0 && (
                <span className="font-display text-h3 italic text-ink-secondary">
                  {unitCount} {unitCount === 1 ? "item" : "items"}
                </span>
              )}
            </div>
          </div>
          <StepRail />
        </div>
      </div>

      {isPending ? (
        <CartSkeleton />
      ) : isError || !cart ? (
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
      ) : cart.items.length === 0 ? (
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
      ) : (
        /* grid-cols-1 below lg: without a definite base track the implicit
           auto column sizes to max-content and the page scrolls sideways.
           The lg template then overrides it. */
        <div className="grid grid-cols-1 gap-12 lg:grid-cols-[1fr_360px]">
          <div className="divide-y divide-hairline border-y border-hairline">
            {cart.items.map((item) => (
              <CartLineItem
                key={item.id}
                item={item}
                /* One snapshot per visible line; snapshot memory scales with
                   the captured area, so cap it. */
                vtName={
                  cart.items.length <= 20 ? `cart-line-${item.id}` : undefined
                }
              />
            ))}
          </div>

          <aside className="z-(--z-sticky) self-start border border-field-border p-6 md:p-8 lg:sticky lg:top-24">
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
                    {/* §5.3 CTA by auth state (CHANGE-11). D2 (U-W10-T2) renames
                        this button: /checkout now replaces an anonymous visitor
                        with /register?next=/checkout, so the old guest wording
                        would promise an outcome the route no longer offers. Same
                        position, same primary-ink block, same target — the cart
                        still promises the door, not the outcome (CheckoutView
                        owns the redirect; it surfaces the backend's own refusal
                        verbatim when an anonymous order is refused —
                        BACKEND_REQUESTS P1, TIER1-GUEST-DEADEND). */}
                    <Link
                      href="/checkout"
                      className="block bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
                    >
                      Continue to checkout
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
                    {/* Screen summary footnote: the cart IS the Django session
                        cart (backend/cart/views.py keys Cart by session_id), so
                        the device wording is the true persistence story for an
                        anonymous visitor - same line the flag-off branch below
                        has always shipped. */}
                    <p className="mt-4 text-center text-xs text-ink-secondary">
                      Your cart is saved on this device.
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
      )}
    </div>
  );
}

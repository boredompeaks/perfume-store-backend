"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { cartCount, getCart } from "@/lib/cart-api";
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
          {/* Mirrors the populated row's plate and its mobile stacking
              (screen: 96x128, 112x144 at sm) so the fallback cannot cause
              the CLS it exists to prevent (§5.3). */}
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

/**
 * The purchase-path rail from the screen ("01 Panier - 02 Expedition -
 * 03 Paiement", U-W12-T7 design override D1: media/designs/
 * maison_ther_votre_panier), restyled to what is actually true: membership
 * and labels mirror CheckoutView's real STEPS (Delivery -> Review ->
 * Payment, CheckoutView.tsx) with this page as step 01, so the rail cannot
 * promise a path the checkout does not run. Static by intent - the brief
 * says "don't fake progress state": the checkout's scaleX fill answers
 * "which step am I on" over there, and replaying it here would claim
 * progress the cart page does not have. The screen draws the future steps
 * with a pointer cursor, but they are states inside /checkout rather than
 * routes, so nothing here is a link (a hover affordance that goes nowhere
 * is scaffolding). The screen's em-dash separators are dropped (house rule:
 * no em dashes in copy) for the gap spacing CheckoutView's own rail uses.
 */
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
            /* The screen marks the current step with a small filled dot;
               square here because the radius language is 0 everywhere but
               the cart counter (DESIGN.md / ui-taste). The state is also
               carried by aria-current and the ink/secondary split, so the
               bullet is decoration only. */
            <span aria-hidden="true" className="size-1.5 bg-bronze" />
          )}
          {`0${i + 1} ${label}`}
        </li>
      ))}
    </ol>
  );
}

/**
 * §5.3 `/cart` — line items (left) and the sticky summary (right), under the
 * screen's title band.
 *
 * The band renders above the state switch, so the h1 ships in every state
 * (skeleton, error, empty, populated). Its count suffix is the real unit
 * total from the cart payload; the screen's "reserved" wording is not
 * shipped - stock holds begin at checkout (SPEC-12-01) and the cart payload
 * carries no hold, so the claim would be invented (P5). The screen's
 * reservation banner is omitted on the same basis (BACKEND_REQUESTS P2
 * "Cart-level reservation banner state").
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
      {/* Screen title band: the maison's French brand eyebrow (the products
          page kept "Catalogue Raisonne" the same way), the screen's English
          h1, the live count in the screen's italic serif slot (no em dash,
          no hold claim), the static step rail, and the screen's hairline
          rule beneath. */}
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
           auto column sizes to max-content and the page scrolls sideways at
           390 (the stacked rows' money/Remove hang past the viewport). The
           lg template then overrides it, unchanged from 5.3. */
        <div className="grid grid-cols-1 gap-12 lg:grid-cols-[1fr_360px]">
          {/* The screen's ledger: a rule above the first line, one between
              every pair, and one closing the list (border-y) - the same
              rule topology CheckoutView and the orders list already use,
              layered onto §5.3's KEEP'd divide-y. */}
          <div className="divide-y divide-hairline border-y border-hairline">
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

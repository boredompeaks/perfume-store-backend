"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError, apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { getCart } from "@/lib/cart-api";
import { formatINR } from "@/lib/money";
import { checkout } from "@/lib/orders-api";
import { templateConfig } from "@/lib/template.config";
import {
  loadShipping,
  saveShipping,
  type ShippingDetails,
} from "@/lib/shipping-store";
import type { Order } from "@/lib/types";
import ProductImage from "../ProductImage";
import AddressForm from "./AddressForm";
import CouponForm from "../cart/CouponForm";
import PaymentLauncher from "./PaymentLauncher";

type Step = "details" | "review" | "pay";

/** Spec 5.4: the three steps, in order — Delivery -> Review -> Payment. */
const STEPS: { id: Step; label: string }[] = [
  { id: "details", label: "Delivery" },
  { id: "review", label: "Review" },
  { id: "pay", label: "Payment" },
];

/** One delivery option priced by `GET /api/shipping/estimate/` (5.4). */
export type EstimateOption = {
  /** Method CODE (slug) — the only thing this client may ever choose. */
  method: string;
  name: string;
  /** Decimal STRING from the server; formatted with formatINR, never summed. */
  amount: string;
  free_shipping: boolean;
};

/**
 * 5.4's step-indicator fill: `stepsCompleted / 2` — 0 on Delivery, half on
 * Review, full on Payment. Exported so the arithmetic is pinned by a test
 * rather than by the pixels.
 */
export function stepFraction(step: Step): number {
  const index = STEPS.findIndex((s) => s.id === step);
  return (index < 0 ? 0 : index) / (STEPS.length - 1);
}

/**
 * D1 (U-W12-T8, screen `maison_ther_commande_r_glement`): the rule's ink fill
 * covers the ACTIVE tab's share of the rule — thirds, so 1/3, 2/3, 1 — which
 * is what the screen draws (pixel probe of screen.png: at Delivery the ink
 * runs 80→359 of a rule spanning 80→919). Spec 5.4's fill is the completed
 * prefix instead (0 / half / full); the owner's screen overrides that layout
 * detail, so this maps the pinned `stepFraction` arithmetic onto the active
 * segment rather than redefining it — `checkout.test.ts` still pins the
 * arithmetic that reaches the pixels.
 */
export function activeFraction(step: Step): number {
  return (stepFraction(step) * (STEPS.length - 1) + 1) / STEPS.length;
}

/**
 * The flag-gated guest wall (CHANGE-11 / TIER1-GUEST-DEADEND), decided in one
 * place so the audited branch cannot drift from the render: flag OFF restores
 * the pre-guest sign-in wall for anonymous visitors exactly as it was; flag ON
 * drops the wall and leaves every further decision — including the 403 the
 * server answers create-order with — to the backend. Nothing here mints,
 * guesses or stores a credential.
 *
 * Since owner directive D2 (U-W10-T2) the component short-circuits an
 * anonymous signal to the register door (`/register?next=/checkout`) before
 * any of these branches paints, so the function is now the gate CONTRACT
 * (pinned by test) rather than a path every visitor walks. It is
 * deliberately not rewritten: the flag's meaning, and the wall behind it,
 * stay auditable.
 */
export function checkoutGate(
  authStatus: "loading" | "authenticated" | "anonymous",
  guestCheckoutEnabled: boolean,
): "loading" | "wall" | "flow" {
  if (authStatus === "loading") return "loading";
  if (authStatus === "anonymous" && !guestCheckoutEnabled) return "wall";
  return "flow";
}

/**
 * 5.4: a 400 from the estimate reads "We cannot deliver there yet" followed by
 * the backend's own refusal message VERBATIM (it is customer-shaped —
 * `shipping/pricing.py` UNSERVICEABLE_MESSAGE). Any other ApiError is rendered
 * as the server said it; only a transport failure gets local copy.
 */
export function estimateErrorCopy(err: unknown): string {
  if (err instanceof ApiError) {
    return err.status === 400
      ? `We cannot deliver there yet. ${err.message}`
      : err.message;
  }
  return "We could not fetch delivery options. Check your connection and try again.";
}

/**
 * 5.4: "the cheapest preselected, because shipping/pricing.py states the
 * precedence explicitly". This is a SELECTION over server-quoted amounts —
 * it decides which server number is shown, and computes no figure of its own.
 */
export function cheapestOption(
  options: EstimateOption[],
): EstimateOption | null {
  if (options.length === 0) return null;
  return options.reduce((best, option) =>
    Number(option.amount) < Number(best.amount) ? option : best,
  );
}

/** One order attempt = one key (5.4 step 2; server cap is 128 chars). */
export function newAttemptKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `ck-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/**
 * What POST /api/orders/checkout/ answers with, beyond `Order`: the
 * customer-facing reference and the server-priced shipping line (5.4 step 3,
 * orders/serializers.py). Read off the response only — never derived here.
 */
type CreatedOrder = Order & {
  order_number: string;
  shipping_amount: string;
};

export default function CheckoutView() {
  const router = useRouter();
  const { status: authStatus } = useAuth();
  // The signal exactly as this render reads it. D2's early return below
  // narrows `authStatus` to "loading" | "authenticated" for the rest of the
  // render, so the two audited hand-off branches that still speak in the RAW
  // signal — the 403 refusal hand-off and the guest pay hand-off, both pinned
  // by testid — read this snapshot instead of fighting the narrowing. With
  // the redirect armed they are unreachable at runtime; they stay because
  // they are contract, not decoration (TIER1-GUEST-DEADEND).
  const rawAuthStatus = authStatus;
  // featureFlags.guestCheckout still gates the guest path (default true):
  // off = the sign-in wall below; on = the wall is down and the backend's own
  // answer decides what a guest may do. D2 (U-W10-T2) sits in front of both:
  // an anonymous visitor is replaced to /register?next=/checkout, so the flag
  // now only shapes the gate contract and any flow the redirect does not
  // cover (an authenticated visitor is never gated at all).
  const guestCheckoutEnabled = templateConfig.featureFlags.guestCheckout;
  const gate = checkoutGate(authStatus, guestCheckoutEnabled);

  const {
    data: cart,
    isPending: cartLoading,
    isError: cartError,
    refetch: refetchCart,
  } = useQuery({ queryKey: ["cart"], queryFn: getCart, retry: false });

  const [step, setStep] = useState<Step>("details");
  const [shipping, setShipping] = useState<ShippingDetails | null>(() =>
    loadShipping(),
  );
  const [couponCode, setCouponCode] = useState<string | null>(null);
  const [order, setOrder] = useState<CreatedOrder | null>(null);
  const [creationError, setCreationError] = useState<string | null>(null);
  const [refused, setRefused] = useState(false);
  const [creating, setCreating] = useState(false);

  // 5.4 CHANGE-12: the delivery estimate, fetched on blur of pincode and
  // state. One slot per destination: a repeat blur of the same destination
  // never refetches, a failure clears the slot so the next blur retries, and
  // a response for a destination the customer has already left is dropped.
  const [estimate, setEstimate] = useState<EstimateOption[] | null>(null);
  const [estimateError, setEstimateError] = useState<string | null>(null);
  const [selectedMethod, setSelectedMethod] = useState<string | null>(null);
  const destinationKey = useRef<string | null>(null);
  const loadedDestination = useRef<string | null>(null);
  const inFlightDestination = useRef<string | null>(null);
  // The Idempotency-Key for the CURRENT attempt: minted when the customer
  // enters review, stable across every "Create order" retry of that attempt,
  // replaced when they go Back and start a new one (5.4 step 2).
  const attemptKey = useRef<string>("");

  // §7.2 — a step change replaces the panel, so without this the focus is
  // left on the button that just disappeared (the gap the spec names as real
  // and absent). Focus moves to the new step's `<h2 tabindex="-1">`: -1 is
  // not a tab stop, so §7.2's "no tabindex above 0" still holds, and the
  // heading is what a screen reader announces as the new location. The ref
  // tracks the heading of whichever step is mounted; the guard keeps the
  // first render from stealing focus from wherever the customer was.
  const stepHeading = useRef<HTMLHeadingElement | null>(null);
  const previousStep = useRef<Step>(step);

  useEffect(() => {
    if (previousStep.current === step) return;
    previousStep.current = step;
    stepHeading.current?.focus();
  }, [step]);

  // Owner directive D2 (U-W10-T2): an anonymous visitor never sees this
  // route — /checkout hands them to /register?next=/checkout instead of the
  // guest wall or the guest flow. `replace`, never `push`, so Back from
  // /register cannot bounce-land on checkout. The ref makes it fire ONCE per
  // mount: a state update (or StrictMode's double-invoke) cannot loop it.
  // Only a RESOLVED "anonymous" is a decision — "loading" has no signal yet,
  // so it falls through to the existing loading state below (no probe, no
  // guessed credential, no new endpoint call: the auth context the page
  // already reads is the whole signal). The target carries `next=/checkout`
  // for both auth doors, which take it through `safeNext`.
  const redirected = useRef(false);
  useEffect(() => {
    if (authStatus !== "anonymous" || redirected.current) return;
    redirected.current = true;
    router.replace("/register?next=/checkout");
  }, [authStatus, router]);

  // Anonymous is a redirect, not a render state (D2): the replace above is
  // already in flight, so the wall and the guest flow must not paint for a
  // route the customer is leaving. One loading frame, never a dead end.
  if (gate === "loading" || authStatus === "anonymous" || cartLoading) {
    return (
      <div className="border border-hairline p-10 text-ink-secondary" role="status">
        Loading checkout…
      </div>
    );
  }

  // The sign-in wall (flag OFF, CHANGE-11 / TIER1-GUEST-DEADEND). D2
  // (U-W10-T2) replaces every anonymous visitor with the register door above,
  // so this branch no longer paints — it is kept, not deleted, because
  // checkoutGate's flag-off decision is an audited contract
  // (checkout.test.ts "guest wall flag gate") and the render must still
  // answer that gate rather than silently diverge from it. The backend's
  // verified refusal (one uniform 403 that mints nothing) still surfaces at
  // submit as the sign-in hand-off below — never faked as success, never
  // pre-judged here.
  if (gate === "wall") {
    return (
      <div className="border border-hairline p-10 text-center">
        <p className="font-display text-2xl tracking-tight">
          Sign in to check out.
        </p>
        <p className="mx-auto mt-3 max-w-sm text-ink-secondary">
          Orders are tied to your account. Your cart is saved on this device
          and waits for you.
        </p>
        <Link
          href="/login?next=/checkout"
          data-testid="checkout-signin"
          className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Sign in
        </Link>
      </div>
    );
  }

  // API down / cart unreadable: the designed error state, never a blank page.
  if (cartError || !cart) {
    return (
      <div className="border border-hairline p-10 text-center">
        <p className="font-display text-2xl tracking-tight">
          We couldn&apos;t load your cart.
        </p>
        <button
          type="button"
          onClick={() => refetchCart()}
          className="mt-6 bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Try again
        </button>
      </div>
    );
  }

  // Empty cart: the designed stub (A-6 keeps it) — one click back to the shelf.
  if (cart.items.length === 0 && !order) {
    return (
      <div className="border border-hairline p-10 text-center">
        <p className="font-display text-2xl tracking-tight">
          Your cart is empty.
        </p>
        <Link
          href="/products"
          className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Shop fragrances
        </Link>
      </div>
    );
  }

  const clientSubtotal = cart.items.reduce(
    (sum, item) => sum + Number(item.product.price) * item.quantity,
    0,
  );
  const cartItemCount = cart.items.reduce(
    (total, item) => total + item.quantity,
    0,
  );

  const chosenOption =
    estimate?.find((option) => option.method === selectedMethod) ?? null;

  /**
   * The summary's courier figure, from server truth only: the created
   * order's shipping_amount, else the server-quoted option the customer
   * picked in step 1. Nothing known yet renders no row — the screen's
   * static "₹ 0 / complimentary over ₹ 15,000" line would be a price this
   * client is not allowed to invent (P5), so the estimate block carries
   * that information instead.
   */
  const courierValue = order
    ? Number(order.shipping_amount) > 0
      ? formatINR(order.shipping_amount)
      : "Complimentary"
    : chosenOption
      ? chosenOption.free_shipping
        ? "Complimentary"
        : formatINR(chosenOption.amount)
      : null;

  const requestEstimate = (details: ShippingDetails) => {
    const state = details.state.trim();
    const pincode = details.pincode.trim();
    if (!state || !pincode) return;
    const key = `${state} ${pincode}`;
    if (key === loadedDestination.current || key === inFlightDestination.current)
      return;
    const isNewDestination = key !== destinationKey.current;
    destinationKey.current = key;
    inFlightDestination.current = key;
    if (isNewDestination) setSelectedMethod(null);
    setEstimateError(null);
    apiFetch<{ options: EstimateOption[] }>(
      `/api/shipping/estimate/?state=${encodeURIComponent(state)}&pincode=${encodeURIComponent(pincode)}`,
    )
      .then((body) => {
        if (destinationKey.current !== key) return; // superseded meanwhile
        loadedDestination.current = key;
        setEstimate(body.options);
        setSelectedMethod((current) => {
          const keep =
            !isNewDestination &&
            current !== null &&
            body.options.some((option) => option.method === current);
          return keep ? current : cheapestOption(body.options)?.method ?? null;
        });
      })
      .catch((err: unknown) => {
        if (destinationKey.current !== key) return;
        loadedDestination.current = null; // the next blur retries
        setEstimate(null);
        setEstimateError(estimateErrorCopy(err));
      })
      .finally(() => {
        if (inFlightDestination.current === key) inFlightDestination.current = null;
      });
  };

  const createOrder = (details: ShippingDetails) => {
    setCreationError(null);
    setRefused(false);
    setCreating(true);
    checkout(
      {
        full_name: details.full_name,
        phone: details.phone.replace(/[\s-]/g, ""),
        address: details.address,
        city: details.city,
        state: details.state,
        pincode: details.pincode,
        coupon_code: couponCode ?? cart.coupon_code ?? undefined,
        shipping_method: selectedMethod ?? undefined,
      },
      attemptKey.current || undefined,
    )
      .then((created) => {
        setOrder(created as CreatedOrder);
        setShipping(details);
        saveShipping(details); // PII save — the moment a working order exists.
        setStep("pay");
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
          // The backend's uniform guest refusal (403, one body for every
          // reason) becomes a sign-in hand-off rather than a retry loop; its
          // own message is the copy — nothing is invented on top of it.
          setRefused(rawAuthStatus === "anonymous" && err.status === 403);
          setCreationError(
            err.status === 404
              ? "Your cart session expired. Please add items again."
              : err.message,
          );
        } else {
          setCreationError("Could not create the order. Try again.");
        }
      })
      .finally(() => setCreating(false));
  };

  const deliveryEstimate = (
    <div data-testid="delivery-estimate">
      <h3 className="text-eyebrow text-ink-secondary">Delivery estimate</h3>
      {estimateError ? (
        <p role="alert" className="mt-3 text-sm text-danger">
          {estimateError}
        </p>
      ) : estimate && estimate.length > 0 ? (
        <fieldset className="mt-3 border-t border-hairline">
          <legend className="sr-only">Delivery option</legend>
          {estimate.map((option) => (
            <label
              key={option.method}
              className="flex cursor-pointer items-center gap-3 border-b border-hairline py-2 text-sm last:border-b-0"
            >
              <input
                type="radio"
                name="delivery-option"
                value={option.method}
                checked={selectedMethod === option.method}
                onChange={() => setSelectedMethod(option.method)}
                className="accent-ink"
                data-testid={`delivery-option-${option.method}`}
              />
              <span>{option.name}</span>
              <span className="ml-auto text-money tabular-nums">
                {option.free_shipping ? "Complimentary" : formatINR(option.amount)}
              </span>
            </label>
          ))}
        </fieldset>
      ) : (
        <p className="mt-3 text-sm text-ink-secondary">
          Enter a state and pincode to see delivery options.
        </p>
      )}
    </div>
  );

  return (
    <div className="grid gap-10 lg:grid-cols-[1fr_380px]">
      <div>
        {/* 5.4's step indicator, in the D1 screen's tabbed form (U-W12-T8):
            numbered tabs ABOVE the 1px rule, both inside max-w-2xl, the active
            tab in ink and the rest muted, and the rule's ink fill covering the
            active tab's third (the screen's own pixels: 80→359 of a rule
            spanning 80→919 at Delivery). Still this page's one motion — an
            --color-ink scaleX fill answering "which step am I on, and how many
            are left"; duration and easing are the progress-intent tokens and
            the reduced-motion layer zeroes their duration at the token layer. */}
        <div className="mb-8 max-w-2xl">
          <ol className="flex justify-between">
            {STEPS.map((item, index) => (
              <li
                key={item.id}
                aria-current={item.id === step ? "step" : undefined}
                className={`text-eyebrow ${
                  item.id === step ? "text-ink" : "text-ink-secondary"
                }`}
              >
                {String(index + 1).padStart(2, "0")}. {item.label}
              </li>
            ))}
          </ol>
          <div
            className="relative mt-2 h-px w-full bg-hairline"
            aria-hidden="true"
          >
            <div
              className="absolute inset-0 origin-left bg-ink"
              style={{
                transform: `scaleX(${activeFraction(step)})`,
                transition:
                  "transform var(--motion-progress-dur) var(--motion-ease)",
              }}
            />
          </div>
        </div>

        {step === "details" && (
          <section aria-labelledby="step-details">
            <p className="text-eyebrow text-bronze">Lieu de Réception</p>
            <h2
              id="step-details"
              ref={stepHeading}
              tabIndex={-1}
              className="mt-1 font-display text-h2 tracking-tight"
            >
              Carriage Destination
            </h2>
            <div className="mt-6">
              <AddressForm
                initial={shipping}
                pending={creating}
                serverError={creationError}
                submitLabel="Continue to review"
                onDestinationBlur={requestEstimate}
                afterFields={deliveryEstimate}
                onSubmit={(details) => {
                  setShipping(details);
                  attemptKey.current = newAttemptKey(); // one key per attempt
                  setStep("review");
                }}
              />
            </div>
          </section>
        )}

        {step === "review" && shipping && (
          <section aria-labelledby="step-review">
            <p className="text-eyebrow text-bronze">Inventaire Raisonné</p>
            <h2
              id="step-review"
              ref={stepHeading}
              tabIndex={-1}
              className="mt-1 font-display text-h2 tracking-tight"
            >
              Order Verification
            </h2>
            <ul className="mt-6 divide-y divide-hairline border-y border-hairline">
              {cart.items.map((item) => (
                <li
                  key={item.id}
                  className="flex items-baseline justify-between gap-4 py-4"
                >
                  <span>
                    <span className="block font-display text-body">
                      {item.product.name}
                    </span>
                    <span className="text-small text-ink-secondary">
                      × {item.quantity}
                    </span>
                  </span>
                  <span className="text-money tabular-nums">
                    {formatINR(Number(item.product.price) * item.quantity)}
                  </span>
                </li>
              ))}
            </ul>
            {/* Kept as ONE text node: the e2e happy path matches
                /Subtotal ₹…/ and /coupon …/ inside this line. */}
            <p className="mt-3 text-small text-ink-secondary">
              Subtotal {formatINR(clientSubtotal)}
              {couponCode ? ` · coupon ${couponCode}` : ""}
            </p>
            <p className="text-small text-ink-secondary">
              The final total is computed server-side when the order is
              created.
            </p>
            {chosenOption && (
              <p className="mt-2 text-small text-ink-secondary">
                Delivery: {chosenOption.name} ·{" "}
                {chosenOption.free_shipping
                  ? "Complimentary"
                  : formatINR(chosenOption.amount)}
              </p>
            )}

            <div className="mt-8 border-t border-hairline pt-6">
              <h3 className="text-eyebrow text-ink-secondary">Coupon</h3>
              <div className="mt-3">
                <CouponForm onCodeChange={setCouponCode} />
              </div>
            </div>

            {creationError && refused && (
              <div role="alert" className="mt-4 border border-hairline p-5">
                <p className="text-sm text-danger">{creationError}</p>
                <Link
                  href="/login?next=/checkout"
                  data-testid="checkout-signin"
                  className="mt-4 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
                >
                  Sign in
                </Link>
              </div>
            )}
            {creationError && !refused && (
              <p role="alert" className="mt-4 text-sm text-danger">
                {creationError}
              </p>
            )}

            <div className="mt-6 flex gap-3">
              <button
                type="button"
                onClick={() => {
                  setCreationError(null);
                  setStep("details");
                }}
                className="border border-ink px-8 py-4 text-label transition-colors hover:bg-ink hover:text-paper"
              >
                Back
              </button>
              <button
                type="button"
                data-testid="create-order"
                onClick={() => createOrder(shipping)}
                disabled={creating}
                className="bg-ink px-8 py-4 text-label text-paper transition-colors hover:bg-bronze disabled:opacity-60"
              >
                Create order
              </button>
            </div>
          </section>
        )}

        {step === "pay" && order && shipping && (
          <section aria-labelledby="step-pay">
            <p className="text-eyebrow text-bronze">Règlement Scellé</p>
            <h2
              id="step-pay"
              ref={stepHeading}
              tabIndex={-1}
              className="mt-1 font-display text-h2 tracking-tight"
            >
              Payment Authorization
            </h2>
            {/* D1 screen's reference box: a ruled two-column header carrying
                the SERVER's reference and total (never derived here), then
                the shipping line and the freeze note it shows around the
                launcher. PaymentLauncher itself is untouched logic-wise. */}
            <div className="mt-6 border border-hairline bg-surface p-6">
              <div className="flex flex-col gap-4 border-b border-hairline pb-4 sm:flex-row sm:items-baseline sm:justify-between">
                <div>
                  <p className="text-eyebrow text-ink-secondary">
                    Order reference
                  </p>
                  <p className="mt-1 font-display text-h3 tracking-tight">
                    {order.order_number}
                  </p>
                </div>
                <div className="sm:text-right">
                  <p className="text-eyebrow text-ink-secondary">
                    Confirmed Total
                  </p>
                  <p className="mt-1 font-display text-h3 tabular-nums">
                    {formatINR(order.total_amount)}
                  </p>
                </div>
              </div>
              <p className="mt-4 text-small text-ink-secondary">
                Shipping{" "}
                <span className="text-money tabular-nums">
                  {Number(order.shipping_amount) > 0
                    ? formatINR(order.shipping_amount)
                    : "Complimentary"}
                </span>
              </p>
              <p className="mt-2 text-small text-ink-secondary">
                The cart is now frozen into this order. Editing it on the cart
                page won&apos;t change this amount.
              </p>
            </div>

            <div className="mt-6">
              {rawAuthStatus === "anonymous" ? (
                /* Verified contract: create_payment and verify_payment are
                   IsAuthenticated, so a guest is never routed into the Razorpay
                   flow — no JWT is faked and no payment call is attempted.
                   The order exists; signing in is where their path resumes. */
                <div
                  className="border border-hairline p-5"
                  role="status"
                  data-testid="guest-pay-handoff"
                >
                  <p className="text-sm text-ink-secondary">
                    Your order is created and waiting for payment. Sign in to
                    complete it.
                  </p>
                  <Link
                    href="/login?next=/checkout"
                    className="mt-4 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
                  >
                    Sign in to pay
                  </Link>
                </div>
              ) : (
                <PaymentLauncher
                  order={order}
                  contact={{
                    name: shipping.full_name,
                    email: shipping.email,
                    phone: shipping.phone,
                  }}
                />
              )}
            </div>
          </section>
        )}
      </div>

      {/* D1 screen's ledger rail: ruled summary card — thumbnailed line
          items, a server-figure breakdown, one big serif total, and the
          provider note. Nothing here invents money: subtotal is the display
          sum of server item prices (reconciled at order creation), delivery
          shows only a server quote or the created order's shipping_amount,
          and the big total reads "Confirmed" only once the server has one. */}
      <aside
        aria-labelledby="order-summary"
        className="self-start border border-hairline bg-surface p-6 lg:sticky lg:top-24"
      >
        <h2
          id="order-summary"
          className="font-display text-h3 tracking-tight"
        >
          Summary
        </h2>
        <div className="mt-4 space-y-4 border-b border-hairline pb-4">
          {cart.items.map((item) => (
            <div key={item.id} className="flex items-center gap-4">
              <div className="relative h-20 w-16 shrink-0 border border-hairline bg-hairline">
                <ProductImage
                  src={item.product.image}
                  alt={item.product.name}
                  sizes="64px"
                />
              </div>
              <div className="min-w-0 flex-1">
                {item.product.category ? (
                  <span className="block text-eyebrow text-bronze">
                    {item.product.category}
                  </span>
                ) : null}
                <p className="truncate font-display text-body">
                  {item.product.name}
                </p>
                <p className="text-small text-ink-secondary">
                  {item.product.size} ml · Qty {item.quantity}
                </p>
              </div>
              <span className="text-money tabular-nums">
                {formatINR(Number(item.product.price) * item.quantity)}
              </span>
            </div>
          ))}
        </div>
        <div className="mt-4 space-y-1 text-small text-ink-secondary">
          <div className="flex items-baseline justify-between gap-3">
            <span>
              Subtotal ({cartItemCount}{" "}
              {cartItemCount === 1 ? "item" : "items"})
            </span>
            <span className="tabular-nums text-ink">
              {formatINR(clientSubtotal)}
            </span>
          </div>
          {courierValue && (
            <div className="flex items-baseline justify-between gap-3">
              <span>Courier delivery</span>
              <span className="text-bronze">{courierValue}</span>
            </div>
          )}
          {order && Number(order.discount_amount) > 0 && (
            <div className="flex items-baseline justify-between gap-3">
              <span>Coupon discount</span>
              <span className="tabular-nums text-ink">
                −{formatINR(order.discount_amount)}
              </span>
            </div>
          )}
        </div>
        <div className="mt-6 flex items-baseline justify-between gap-3 border-t border-hairline pt-4">
          <span className="font-display text-h3">
            {order ? "Confirmed Total" : "Estimated Total"}
          </span>
          <span className="font-display text-h3 tabular-nums">
            {formatINR(order ? order.total_amount : clientSubtotal)}
          </span>
        </div>
        <p className="mt-4 border-t border-hairline pt-4 text-small text-ink-secondary">
          Processed securely via Razorpay. Card credentials never reach our
          servers.
        </p>
      </aside>
    </div>
  );
}

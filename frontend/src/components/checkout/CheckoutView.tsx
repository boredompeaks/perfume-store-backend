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

  const chosenOption =
    estimate?.find((option) => option.method === selectedMethod) ?? null;

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
      <h3 className="text-sm font-medium uppercase tracking-wide text-ink-secondary">
        Delivery estimate
      </h3>
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
                {option.free_shipping ? "Free" : formatINR(option.amount)}
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
        {/* 5.4: the step indicator is the one motion on this page — an
            --color-ink scaleX fill over a hairline rule, answering "which
            step am I on, and how many are left". Duration and easing are
            the progress-intent tokens; the reduced-motion layer zeroes them. */}
        <div className="mb-8">
          <div className="relative h-px w-full bg-hairline" aria-hidden="true">
            <div
              className="absolute inset-0 origin-left bg-ink"
              style={{
                transform: `scaleX(${stepFraction(step)})`,
                transition:
                  "transform var(--motion-progress-dur) var(--motion-ease)",
              }}
            />
          </div>
          <ol className="mt-3 flex gap-6">
            {STEPS.map((item) => (
              <li
                key={item.id}
                aria-current={item.id === step ? "step" : undefined}
                className={`text-label ${
                  item.id === step ? "text-ink" : "text-ink-secondary"
                }`}
              >
                {item.label}
              </li>
            ))}
          </ol>
        </div>

        {step === "details" && (
          <section aria-labelledby="step-details">
            <h2
              id="step-details"
              ref={stepHeading}
              tabIndex={-1}
              className="font-display text-2xl tracking-tight"
            >
              Delivery details
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
            <h2
              id="step-review"
              ref={stepHeading}
              tabIndex={-1}
              className="font-display text-2xl tracking-tight"
            >
              Review your order
            </h2>
            <ul className="mt-6 divide-y divide-hairline border-y border-hairline">
              {cart.items.map((item) => (
                <li
                  key={item.id}
                  className="flex items-baseline justify-between gap-3 py-3 text-sm"
                >
                  <span>
                    {item.product.name}{" "}
                    <span className="text-ink-secondary">× {item.quantity}</span>
                  </span>
                  <span className="text-money tabular-nums">
                    {formatINR(Number(item.product.price) * item.quantity)}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-sm text-ink-secondary">
              Subtotal {formatINR(clientSubtotal)}
              {couponCode ? ` · coupon ${couponCode}` : ""}
            </p>
            <p className="text-xs text-ink-secondary">
              The final total is computed server-side when the order is
              created.
            </p>
            {chosenOption && (
              <p className="mt-2 text-sm text-ink-secondary">
                Delivery: {chosenOption.name} ·{" "}
                {chosenOption.free_shipping
                  ? "Free"
                  : formatINR(chosenOption.amount)}
              </p>
            )}

            <div className="mt-8 border-t border-hairline pt-6">
              <h3 className="text-sm font-medium uppercase tracking-wide text-ink-secondary">
                Coupon
              </h3>
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
                className="border border-ink px-5 py-3 text-sm transition-colors hover:bg-ink hover:text-paper"
              >
                Back
              </button>
              <button
                type="button"
                data-testid="create-order"
                onClick={() => createOrder(shipping)}
                disabled={creating}
                className="bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-60"
              >
                Create order
              </button>
            </div>
          </section>
        )}

        {step === "pay" && order && shipping && (
          <section aria-labelledby="step-pay">
            <h2
              id="step-pay"
              ref={stepHeading}
              tabIndex={-1}
              className="font-display text-2xl tracking-tight"
            >
              Payment
            </h2>
            <p className="mt-3 text-sm text-ink-secondary">
              Order <span className="text-ink">{order.order_number}</span>
            </p>
            <p className="mt-1 text-sm text-ink-secondary">
              Total{" "}
              <strong className="text-money tabular-nums">
                {formatINR(order.total_amount)}
              </strong>{" "}
              — computed and held server-side.
            </p>
            <p className="mt-1 text-sm text-ink-secondary">
              Shipping{" "}
              <span className="text-money tabular-nums">
                {Number(order.shipping_amount) > 0
                  ? formatINR(order.shipping_amount)
                  : "Free"}
              </span>
            </p>
            <p className="mt-3 text-xs text-ink-secondary">
              The cart is now frozen into this order. Editing it on the cart
              page won&apos;t change this amount.
            </p>

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

      <aside
        aria-labelledby="order-summary"
        className="self-start border border-hairline p-6 lg:sticky lg:top-24"
      >
        <h2
          id="order-summary"
          className="font-display text-xl tracking-tight"
        >
          Order summary
        </h2>
        <div className="mt-4 flex items-baseline justify-between text-sm">
          <span className="text-ink-secondary">Cart subtotal</span>
          <span className="tabular-nums">{formatINR(clientSubtotal)}</span>
        </div>
        {order && (
          <>
            {Number(order.discount_amount) > 0 && (
              <div className="mt-2 flex items-baseline justify-between text-sm">
                <span className="text-ink-secondary">Coupon discount</span>
                <span className="tabular-nums">
                  −{formatINR(order.discount_amount)}
                </span>
              </div>
            )}
            <div className="mt-2 flex items-baseline justify-between text-sm">
              <span className="text-ink-secondary">Shipping</span>
              <span className="tabular-nums">
                {Number(order.shipping_amount) > 0
                  ? formatINR(order.shipping_amount)
                  : "Free"}
              </span>
            </div>
            <div className="mt-2 flex items-baseline justify-between text-sm">
              <span className="text-ink-secondary">Order total (server)</span>
              <span className="text-money tabular-nums">
                {formatINR(order.total_amount)}
              </span>
            </div>
          </>
        )}
        <p className="mt-4 border-t border-hairline pt-4 text-xs text-ink-secondary">
          Payments are processed by Razorpay. Card details never touch our
          servers.
        </p>
      </aside>
    </div>
  );
}

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { ApiError, apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useCart } from "@/lib/use-cart";
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

const STEPS: { id: Step; label: string }[] = [
  { id: "details", label: "Delivery" },
  { id: "review", label: "Review" },
  { id: "pay", label: "Payment" },
];

/** One delivery option priced by the server. */
export type EstimateOption = {
  method: string;
  name: string;
  /** Decimal string from the server; format, never sum. */
  amount: string;
  free_shipping: boolean;
};

/** `stepsCompleted / 2`: exported so a test pins the arithmetic, not pixels. */
export function stepFraction(step: Step): number {
  const index = STEPS.findIndex((s) => s.id === step);
  return (index < 0 ? 0 : index) / (STEPS.length - 1);
}

/**
 * The screen fills the active tab's third of the rule, not the completed
 * prefix; this maps `stepFraction` onto it without redefining that arithmetic.
 */
export function activeFraction(step: Step): number {
  return (stepFraction(step) * (STEPS.length - 1) + 1) / STEPS.length;
}

/**
 * One place for the guest-wall decision: flag off restores the sign-in wall,
 * flag on defers to the backend and mints no credential. The redirect now
 * precedes it, but the branch remains the audited contract.
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
 * A 400 estimate reads "We cannot deliver there yet" plus the backend's own
 * refusal verbatim; only a transport failure gets local copy.
 */
export function estimateErrorCopy(err: unknown): string {
  if (err instanceof ApiError) {
    return err.status === 400
      ? `We cannot deliver there yet. ${err.message}`
      : err.message;
  }
  return "We could not fetch delivery options. Check your connection and try again.";
}

/** Preselects the cheapest server quote; computes no figure itself. */
export function cheapestOption(
  options: EstimateOption[],
): EstimateOption | null {
  if (options.length === 0) return null;
  return options.reduce((best, option) =>
    Number(option.amount) < Number(best.amount) ? option : best,
  );
}

/** One order attempt = one key; the server caps it at 128 chars. */
export function newAttemptKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `ck-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** Response fields beyond `Order`, read off the response, never derived. */
type CreatedOrder = Order & {
  order_number: string;
  shipping_amount: string;
};

export default function CheckoutView() {
  const router = useRouter();
  const { status: authStatus } = useAuth();
  // Raw signal kept for the two audited hand-off branches below: D2's early
  // return narrows `authStatus` to loading/authenticated for the render.
  const rawAuthStatus = authStatus;
  // The flag shapes the gate contract; D2 sends anonymous visitors to the
  // register door, so an authenticated visitor is never gated.
  const guestCheckoutEnabled = templateConfig.featureFlags.guestCheckout;
  const gate = checkoutGate(authStatus, guestCheckoutEnabled);

  const {
    data: cart,
    isPending: cartLoading,
    isError: cartError,
    refetch: refetchCart,
  } = useCart();

  const [step, setStep] = useState<Step>("details");
  const [shipping, setShipping] = useState<ShippingDetails | null>(() =>
    loadShipping(),
  );
  const [couponCode, setCouponCode] = useState<string | null>(null);
  const [order, setOrder] = useState<CreatedOrder | null>(null);
  const [creationError, setCreationError] = useState<string | null>(null);
  const [refused, setRefused] = useState(false);
  const [creating, setCreating] = useState(false);

  // One slot per destination: a repeat blur does not refetch, a failure
  // clears the slot, and a response for an abandoned destination is dropped.
  const [estimate, setEstimate] = useState<EstimateOption[] | null>(null);
  const [estimateError, setEstimateError] = useState<string | null>(null);
  const [selectedMethod, setSelectedMethod] = useState<string | null>(null);
  const destinationKey = useRef<string | null>(null);
  const loadedDestination = useRef<string | null>(null);
  const inFlightDestination = useRef<string | null>(null);
  // Stable across retries of this attempt; replaced on Back.
  const attemptKey = useRef<string>("");

  // A step change replaces the panel, so focus moves to the new `<h2
  // tabindex="-1">`; the guard keeps the first render from stealing focus.
  const stepHeading = useRef<HTMLHeadingElement | null>(null);
  const previousStep = useRef<Step>(step);

  useEffect(() => {
    if (previousStep.current === step) return;
    previousStep.current = step;
    stepHeading.current?.focus();
  }, [step]);

  // Anonymous visitors go to the register door before any guest branch paints.
  // `replace` keeps Back from bounce-landing here; the ref fires once per
  // mount so StrictMode cannot loop it.
  const redirected = useRef(false);
  useEffect(() => {
    if (authStatus !== "anonymous" || redirected.current) return;
    redirected.current = true;
    router.replace("/register?next=/checkout");
  }, [authStatus, router]);

  // Redirect is in flight: never paint the wall or guest flow at a route the
  // customer is leaving.
  if (gate === "loading" || authStatus === "anonymous" || cartLoading) {
    return (
      <div className="border border-hairline p-10 text-ink-secondary" role="status">
        Loading checkout…
      </div>
    );
  }

  // Kept as audited gate contract even though the redirect above means this
  // branch no longer paints.
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

  // Courier figure from server truth only; unknown renders no row rather than
  // inventing the screen's static ₹0/complimentary line.
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
        saveShipping(details); // saves PII only once a working order exists
        setStep("pay");
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
          // The backend's uniform guest 403 becomes a sign-in hand-off; its
          // own message is the copy.
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
        {/* Numbered tabs above the rule; the ink fill covers the active tab's
            third and is the page's one motion. */}
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
                /* Payment calls require a JWT: a guest gets this hand-off, so
                   no session is faked and their order is left payable. */
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

      {/* Summary shows only server figures; subtotal is the display sum of
          server item prices and "Confirmed" waits for a server total. */}
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

"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { getCart } from "@/lib/cart-api";
import { formatINR } from "@/lib/money";
import { checkout } from "@/lib/orders-api";
import {
  loadShipping,
  saveShipping,
  type ShippingDetails,
} from "@/lib/shipping-store";
import AddressForm from "./AddressForm";
import CouponForm from "../cart/CouponForm";
import PaymentLauncher from "./PaymentLauncher";

type Step = "details" | "review" | "pay";

export default function CheckoutView() {
  const { status: authStatus } = useAuth();

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
  const [order, setOrder] = useState<Awaited<ReturnType<typeof checkout>> | null>(
    null,
  );
  const [creationError, setCreationError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  if (authStatus === "loading" || cartLoading) {
    return (
      <div className="border border-line p-10 text-ink-muted" role="status">
        Loading checkout…
      </div>
    );
  }

  if (authStatus === "anonymous") {
    return (
      <div className="border border-line p-10 text-center">
        <p className="font-display text-2xl tracking-tight">
          Sign in to check out.
        </p>
        <p className="mx-auto mt-3 max-w-sm text-ink-muted">
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
      <div className="border border-line p-10 text-center">
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
      <div className="border border-line p-10 text-center">
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

  const createOrder = (details: ShippingDetails) => {
    setCreationError(null);
    setCreating(true);
    checkout({
      full_name: details.full_name,
      phone: details.phone.replace(/[\s-]/g, ""),
      address: details.address,
      city: details.city,
      state: details.state,
      pincode: details.pincode,
      coupon_code: couponCode ?? undefined,
    })
      .then((created) => {
        setOrder(created);
        setShipping(details);
        saveShipping(details); // PII save — the moment a working order exists.
        try {
          sessionStorage.removeItem("aurel.coupon");
        } catch {
          // ignore
        }
        setStep("pay");
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
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

  return (
    <div className="grid gap-10 lg:grid-cols-[1fr_380px]">
      <div>
        {step === "details" && (
          <section aria-labelledby="step-details">
            <h2
              id="step-details"
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
                onSubmit={(details) => {
                  setShipping(details);
                  setStep("review");
                }}
              />
            </div>
            <div className="mt-8 border-t border-line pt-6">
              <h3 className="text-sm font-medium uppercase tracking-wide text-ink-muted">
                Coupon
              </h3>
              <div className="mt-3">
                <CouponForm onCodeChange={setCouponCode} />
              </div>
            </div>
          </section>
        )}

        {step === "review" && shipping && (
          <section aria-labelledby="step-review">
            <h2
              id="step-review"
              className="font-display text-2xl tracking-tight"
            >
              Review your order
            </h2>
            <ul className="mt-6 divide-y divide-line border-y border-line">
              {cart.items.map((item) => (
                <li
                  key={item.id}
                  className="flex items-baseline justify-between gap-3 py-3 text-sm"
                >
                  <span>
                    {item.product.name}{" "}
                    <span className="text-ink-muted">× {item.quantity}</span>
                  </span>
                  <span>
                    {formatINR(Number(item.product.price) * item.quantity)}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-sm text-ink-muted">
              Subtotal {formatINR(clientSubtotal)}
              {couponCode ? ` · coupon ${couponCode}` : ""}
            </p>
            <p className="text-xs text-ink-muted">
              The final total is computed server-side when the order is
              created.
            </p>

            {creationError && (
              <p role="alert" className="mt-4 text-sm text-bronze">
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
            <h2 id="step-pay" className="font-display text-2xl tracking-tight">
              Payment
            </h2>
            <p className="mt-3 text-sm text-ink-muted">
              Order #{order.id} · total{" "}
              <strong className="text-ink">
                {formatINR(order.total_amount)}
              </strong>{" "}
              — computed and held server-side.
            </p>
            <p className="mt-1 text-xs text-ink-muted">
              The cart is now frozen into this order. Editing it on the cart
              page won&apos;t change this amount.
            </p>

            <div className="mt-6">
              <PaymentLauncher
                order={order}
                contact={{
                  name: shipping.full_name,
                  email: shipping.email,
                  phone: shipping.phone,
                }}
              />
            </div>
          </section>
        )}
      </div>

      <aside className="self-start border border-line p-6 lg:sticky lg:top-24">
        <h2 className="font-display text-xl tracking-tight">Order summary</h2>
        <div className="mt-4 flex items-baseline justify-between text-sm">
          <span className="text-ink-muted">Cart subtotal</span>
          <span>{formatINR(clientSubtotal)}</span>
        </div>
        {order && (
          <>
            <div className="mt-2 flex items-baseline justify-between text-sm">
              <span className="text-ink-muted">Order total (server)</span>
              <span>{formatINR(order.total_amount)}</span>
            </div>
            {Number(order.discount_amount) > 0 && (
              <p className="mt-1 text-xs text-ink-muted">
                Includes coupon discount of {formatINR(order.discount_amount)}.
              </p>
            )}
          </>
        )}
        <p className="mt-4 border-t border-line pt-4 text-xs text-ink-muted">
          Payments are processed by Razorpay. Card details never touch our
          servers.
        </p>
      </aside>
    </div>
  );
}

"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fetchOrder, type OrderRecord } from "@/lib/orders-api";
import { formatINR } from "@/lib/money";
import { loadShipping } from "@/lib/shipping-store";
import OrderStatusBadge, {
  formatOrderDate,
  fulfilmentLabels,
  paymentLabels,
} from "./OrderStatusBadge";
import ShipmentTracking from "./ShipmentTracking";
import PaymentLauncher from "../checkout/PaymentLauncher";

/**
 * Every miss is the backend's uniform 404, so the UI cannot and does not
 * distinguish unknown from foreign ids. A signed-out caller gets the wall.
 */
export default function OrderDetailView({
  orderId,
  paymentSuccess,
}: {
  orderId: number;
  paymentSuccess: boolean;
}) {
  const { status: authStatus } = useAuth();
  const { data: order, isPending, isError, error, refetch } = useQuery({
    // Keyed under ["orders"] so PaymentLauncher's prefix invalidation
    // refreshes it after verify; "detail" keeps it clear of ["orders", page].
    queryKey: ["orders", "detail", orderId],
    queryFn: () => fetchOrder(orderId),
    retry: false,
  });

  if (authStatus === "loading" || (authStatus === "authenticated" && isPending)) {
    return (
      <div aria-hidden="true" className="grid gap-10 lg:grid-cols-[1fr_380px]">
        <div className="space-y-4">
          <div className="skeleton h-8 w-40" />
          <div className="skeleton h-4 w-64" />
          <div className="skeleton h-24 w-full" />
          <div className="skeleton h-4 w-48" />
        </div>
        <div className="skeleton h-48 w-full" />
      </div>
    );
  }

  if (authStatus === "anonymous" || (error instanceof ApiError && error.status === 401)) {
    return (
      <div className="border border-hairline p-10 text-center">
        <p className="font-display text-2xl tracking-tight">
          Sign in to view this order.
        </p>
        <Link
          href={`/login?next=/orders/${orderId}`}
          className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Sign in
        </Link>
      </div>
    );
  }

  if (isError || !order) {
    // Uniform 404: no id in the copy, since the response cannot tell us
    // whether the order exists, belongs to someone else, or never will.
    if (error instanceof ApiError && error.status === 404) {
      return (
        <div className="border border-hairline p-10 text-center">
          <p className="font-display text-2xl tracking-tight">
            Order not found.
          </p>
          <Link
            href="/orders"
            className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
          >
            All orders
          </Link>
        </div>
      );
    }

    // Network failure gets a designed panel, like every other state.
    return (
      <div className="border border-hairline p-10 text-center">
        <p className="text-ink-secondary">Couldn&apos;t load this order.</p>
        <button
          type="button"
          onClick={() => refetch()}
          className="mt-4 bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Try again
        </button>
      </div>
    );
  }

  return (
    <div>
      {/* Screen's top indicator (chip + timestamp, right). The "← All
          orders" half lives in the server segment above this component
          (orders/[id]/page.tsx) so it renders without JS. The timestamp is
          hidden below sm — the screen itself hides it there (code.html
          line 31). */}
      <div className="flex flex-wrap items-center justify-end gap-4">
        <OrderStatusBadge
          status={order.status}
          paymentStatus={order.payment_status}
          fulfilmentStatus={order.fulfilment_status}
        />
        <span className="hidden text-sm text-ink-secondary tabular-nums lining-nums sm:inline">
          {formatOrderDate(order.created_at)}
        </span>
      </div>

      {/* §5.5 item 3 / §7.7 — the page's single h1. First in DOM so the
          payment-success h2 below never precedes it in the heading
          hierarchy; `id` never appears in customer-facing text. The folio
          line is the screen's "Vault Registry #ORD-…" eyebrow WITHOUT the
          number: the h1 below IS the reference, and repeating it would make
          the cancelled-order header read as a receipt narrative. */}
      <header className="mt-6 border-b border-hairline pb-8">
        <p className="text-eyebrow uppercase text-bronze">Vault registry</p>
        <h1 className="mt-2 font-display text-h1">
          Order {order.order_number}
        </h1>
      </header>

      {paymentSuccess && order.status === "confirmed" && (
        <div
          data-testid="payment-success-banner"
          className="mt-8 border border-hairline bg-surface p-6"
          role="status"
        >
          <h2 className="font-display text-h2">
            Payment received — thank you.
          </h2>
          <p className="mt-2 text-sm text-ink-secondary">
            Order {order.order_number} is confirmed. A confirmation summary is
            below.
          </p>
        </div>
      )}

      <div className="mt-10 grid gap-10 lg:grid-cols-[1fr_380px]">
        <div>
          <OrderJourney order={order} />
          <ShipmentTracking orderNumber={order.order_number} />

          {/* Screen's "Items in Consignment" header rule; the count is the
              real line quantity (P5), "flacons" the house vocabulary
              (HomeCarousel "View flacon"). */}
          <section aria-labelledby="order-items-heading" className="mt-12">
            <div className="flex items-baseline justify-between border-b border-hairline pb-3">
              <h2 id="order-items-heading" className="text-eyebrow uppercase text-ink">
                Items
              </h2>
              <span className="text-label-sm uppercase text-bronze">
                {order.items.reduce((s, i) => s + i.quantity, 0)} flacons
              </span>
            </div>
            <ul className="divide-y divide-hairline">
              {order.items.map((item) => (
                <li
                  key={item.id}
                  className="flex items-baseline justify-between gap-3 py-4 text-sm"
                >
                  <span className="min-w-0">
                    <span className="block font-display text-body">
                      {item.product_name} × {item.quantity}
                    </span>
                    {/* sku / variant_name are the frozen checkout snapshot
                        (models.py:325-333): rendered from this row, never a
                        live product read — the item cannot change after the
                        order. Empty today (no variant input yet), so both stay
                        out of the way until the backend fills them. */}
                    {item.variant_name &&
                      item.variant_name !== item.product_name && (
                        <span className="block text-xs text-ink-secondary">
                          {item.variant_name}
                        </span>
                      )}
                    {item.sku && (
                      <span className="block text-xs text-ink-secondary tabular-nums lining-nums">
                        SKU {item.sku}
                      </span>
                    )}
                    <span className="block text-xs text-ink-secondary tabular-nums lining-nums">
                      {formatINR(item.price)} each
                    </span>
                  </span>
                  <span className="text-money tabular-nums lining-nums">
                    {formatINR(item.subtotal)}
                  </span>
                </li>
              ))}
            </ul>

            <dl className="mt-6 space-y-1 text-sm">
              <div className="flex justify-between">
                <dt className="text-ink-secondary">Subtotal</dt>
                <dd className="tabular-nums lining-nums">
                  {/* Display-only sum of the server's own per-line subtotals —
                      no pricing rule runs client-side (P5 / A-13). */}
                  {formatINR(
                    order.items.reduce((s, i) => s + Number(i.subtotal), 0),
                  )}
                </dd>
              </div>
              {Number(order.discount_amount) > 0 && (
                <div className="flex justify-between">
                  <dt className="text-ink-secondary">
                    Coupon {order.coupon ? `(${order.coupon})` : ""}
                  </dt>
                  <dd className="tabular-nums lining-nums">
                    −{formatINR(order.discount_amount)}
                  </dd>
                </div>
              )}
              {/* §5.5 item 6 — the shipping lines the serializer now ships. */}
              <div className="flex justify-between">
                <dt className="text-ink-secondary">Shipping</dt>
                <dd className="tabular-nums lining-nums">
                  {formatINR(order.shipping_amount)}
                </dd>
              </div>
              {order.shipping_method_code && (
                <div className="flex justify-between">
                  <dt className="text-ink-secondary">Delivery option</dt>
                  <dd>{order.shipping_method_code}</dd>
                </div>
              )}
              {/* Screen's total treatment: eyebrow label, ink rule, serif
                  tabular figure (§2.2 permits Playfair on money-adjacent
                  display). */}
              <div className="mt-3 flex items-baseline justify-between border-t border-ink pt-3">
                <dt className="text-eyebrow uppercase text-ink">Total</dt>
                <dd className="font-display text-h3 tabular-nums lining-nums">
                  {formatINR(order.total_amount)}
                </dd>
              </div>
            </dl>
          </section>
        </div>

        <aside className="self-start border border-hairline p-6 text-sm lg:sticky lg:top-24">
          <h2 className="text-eyebrow uppercase text-bronze">Delivery to</h2>
          <address className="mt-3 not-italic leading-relaxed">
            {order.full_name}
            <br />
            {order.address}
            <br />
            {order.city}, {order.state} {order.pincode}
            <br />
            {order.phone}
          </address>

          {payable(order) && (
            <div
              className="mt-6 border-t border-hairline pt-6"
              data-testid="resume-payment-panel"
            >
              <h3 className="text-sm font-medium uppercase tracking-wide text-ink-secondary">
                Payment pending
              </h3>
              <p className="mt-2 text-xs text-ink-secondary">
                This order hasn&apos;t been paid yet. You can complete the
                payment safely — the amount is locked server-side.
              </p>
              <div className="mt-4">
                <PaymentLauncher
                  order={order}
                  contact={loadShipping() ?? {}}
                  buttonLabel="Complete payment"
                />
              </div>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

/**
 * The backend refuses anything whose status is not `pending` and treats a
 * `failed` payment as retryable, so "payable" means payable and not captured.
 */
function payable(order: OrderRecord): boolean {
  return (
    order.status === "pending" &&
    (order.payment_status === "pending" ||
      order.payment_status === "failed" ||
      order.payment_status === "authorized")
  );
}

/**
 * The six timestamps are exactly what the serializer ships; unreached steps
 * print their label with no date. Markers settle on the view timeline via
 * `.settle`, which is disabled for reduced motion and unsupported browsers.
 */
function OrderJourney({ order }: { order: OrderRecord }) {
  const steps: {
    label: string;
    value?: string;
    current?: boolean;
    at?: string | null;
  }[] = [
    {
      label: "Payment",
      value: paymentLabels[order.payment_status] ?? order.payment_status,
      current: true,
    },
    {
      label: "Fulfilment",
      value:
        fulfilmentLabels[order.fulfilment_status] ?? order.fulfilment_status,
      current: true,
    },
    { label: "Paid", at: order.paid_at },
    { label: "Fulfilled", at: order.fulfilled_at },
    { label: "Shipped", at: order.shipped_at },
    { label: "Delivered", at: order.delivered_at },
    { label: "Cancelled", at: order.cancelled_at },
    { label: "Refunded", at: order.refunded_at },
  ];

  const milestones = steps.filter((step) => !step.current);
  const reached = milestones.filter((step) => step.at).length;

  return (
    <section aria-labelledby="order-journey-heading">
      <div className="flex items-baseline justify-between border-b border-hairline pb-3">
        <h2
          id="order-journey-heading"
          className="text-eyebrow uppercase text-ink"
        >
          Order journey
        </h2>
        <span className="text-label-sm uppercase tabular-nums text-bronze">
          {reached} of {milestones.length} milestones
        </span>
      </div>
      <ol className="mt-8">
        {steps.map((step, i) => {
          const isReached = step.current ?? Boolean(step.at);
          const last = i === steps.length - 1;
          return (
            <li key={step.label} className="flex gap-4">
              <span aria-hidden="true" className="relative w-2 shrink-0">
                {/* Marker sits at left-0 (its 8px = the 8px column), NOT
                    centred with a translate: `.settle` animates `transform`,
                    and a translated marker would lose its offset mid-flight.
                    The rule below carries the translate; it never animates. */}
                <span
                  className={`settle absolute left-0 top-1.5 h-2 w-2 ${
                    isReached
                      ? "bg-ink"
                      : "border border-hairline bg-surface"
                  }`}
                />
                {!last && (
                  <span className="absolute bottom-0 left-1/2 top-4 w-px -translate-x-1/2 bg-hairline" />
                )}
              </span>
              <span className="flex min-w-0 flex-1 flex-wrap items-baseline justify-between gap-x-4 gap-y-1 pb-6">
                <span
                  className={`font-display text-body ${
                    isReached ? "text-ink" : "text-ink-secondary"
                  }`}
                >
                  {step.label}
                </span>
                {step.current && step.value ? (
                  <span className="border border-hairline px-2 py-0.5 text-xs uppercase tracking-wide text-ink-secondary">
                    {step.value}
                  </span>
                ) : step.at ? (
                  <time
                    dateTime={step.at}
                    className="text-xs text-ink-secondary tabular-nums lining-nums"
                  >
                    {formatOrderDate(step.at)}
                  </time>
                ) : (
                  <span className="sr-only">Not yet</span>
                )}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

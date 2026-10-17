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
import PaymentLauncher from "../checkout/PaymentLauncher";

/**
 * §5.5 — single order, fed by `GET /api/orders/{id}/` (CHANGE-13), not by
 * the list. The backend answers every miss (unknown id, someone else's
 * order) with the same uniform 404, and the UI does not try to tell those
 * cases apart (§5.5 line 1481-1484); a signed-out caller never reaches it —
 * that state renders the sign-in wall.
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
    // Keyed UNDER the "orders" prefix so PaymentLauncher's shared
    // `invalidateQueries({ queryKey: ["orders"] })` (PaymentLauncher.tsx:118)
    // refreshes this read after a successful verify — without it the
    // `?payment=success` banner would read a stale "pending" status. The
    // literal "detail" segment keeps it from ever colliding with the list's
    // numeric `["orders", page]` key.
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
    // The uniform 404 (§5.5: the backend deliberately does not distinguish
    // "not yours" / "does not exist"). No id in the copy: `order_number` is
    // the customer-facing reference and it is exactly what a 404 does not
    // hand us, so the panel names nothing it cannot know.
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

    // API-down / network failure: designed panel, not bare prose, and — like
    // money states — never animated.
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
      {/* §5.5 item 3 / §7.7 — the page's single h1. First in DOM so the
          payment-success h2 below never precedes it in the heading
          hierarchy; `id` never appears in customer-facing text. */}
      <div className="flex flex-wrap items-center gap-4">
        <h1 className="font-display text-3xl tracking-tight">
          Order {order.order_number}
        </h1>
        <OrderStatusBadge
          status={order.status}
          paymentStatus={order.payment_status}
          fulfilmentStatus={order.fulfilment_status}
        />
        <span className="text-sm text-ink-secondary">
          {formatOrderDate(order.created_at)}
        </span>
      </div>

      {paymentSuccess && order.status === "confirmed" && (
        <div
          data-testid="payment-success-banner"
          className="mt-8 border border-hairline bg-surface p-6"
          role="status"
        >
          <h2 className="font-display text-2xl tracking-tight">
            Payment received — thank you.
          </h2>
          <p className="mt-2 text-sm text-ink-secondary">
            Order {order.order_number} is confirmed. A confirmation summary is
            below.
          </p>
        </div>
      )}

      <div className="mt-8 grid gap-10 lg:grid-cols-[1fr_380px]">
        <div>
          <OrderJourney order={order} />

          <h3 className="mt-8 text-sm font-medium uppercase tracking-wide text-ink-secondary">
            Items
          </h3>
          <ul className="mt-3 divide-y divide-hairline border-y border-hairline">
            {order.items.map((item) => (
              <li
                key={item.id}
                className="flex items-baseline justify-between gap-3 py-3 text-sm"
              >
                <span className="min-w-0">
                  <span className="block">
                    {item.product_name} × {item.quantity}
                  </span>
                  {/* sku / variant_name are the frozen checkout snapshot
                      (models.py:440-456): rendered from this row, never a
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

          <dl className="mt-4 space-y-1 text-sm">
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
            <div className="flex justify-between border-t border-hairline pt-2 text-base">
              <dt>Total</dt>
              <dd className="font-medium tabular-nums lining-nums">
                {formatINR(order.total_amount)}
              </dd>
            </div>
          </dl>
        </div>

        <aside className="self-start border border-hairline p-6 text-sm lg:sticky lg:top-24">
          <h3 className="text-sm font-medium uppercase tracking-wide text-ink-secondary">
            Delivery to
          </h3>
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
 * §5.5 item 8 says "resume payment when `payment_status` is unpaid" — the
 * backend's vocabulary has no `unpaid`, and its own precondition is the
 * source of truth: `payment/` refuses anything whose `status` is not
 * `pending` (backend/orders/views/payment.py:92-94) and treats a `failed`
 * payment as retryable (orders/state.py:178). So an order is resumable when
 * the machine says it can be paid AND the money has not landed.
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
 * §5.5 item 4 — the lifecycle timeline. The six timestamps are exactly what
 * `OrderSerializer` ships (backend/orders/serializers.py:131-136); the two
 * machine dimensions ride as current-state rows (serializers.py:97-98).
 * `OrderStatusEvent` rows are NOT in the projection, so nothing reads them.
 *
 * Unreached steps print their label and no date — that is the state, and it
 * is the customer's actual question ("what is still to come", §5.5).
 *
 * Accent moment (§5.5 line 1478, P2): the markers settle in on
 * `animation-timeline: view()` via the existing `.settle` class — reading
 * position mapped onto journey position, declared inside
 * `prefers-reduced-motion: no-preference` + `@supports` (globals.css:525-544),
 * so reduced motion and unsupported browsers render every marker already
 * filled, and the same status is printed as text at the top of the page.
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

  return (
    <section aria-labelledby="order-journey-heading">
      <h3
        id="order-journey-heading"
        className="text-sm font-medium uppercase tracking-wide text-ink-secondary"
      >
        Order journey
      </h3>
      <ol className="mt-4">
        {steps.map((step, i) => {
          const reached = step.current ?? Boolean(step.at);
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
                    reached
                      ? "bg-ink"
                      : "border border-hairline bg-surface"
                  }`}
                />
                {!last && (
                  <span className="absolute bottom-0 left-1/2 top-4 w-px -translate-x-1/2 bg-hairline" />
                )}
              </span>
              <span className="flex min-w-0 flex-1 flex-wrap items-baseline justify-between gap-x-4 gap-y-1 pb-4">
                <span className={`text-sm ${reached ? "text-ink" : "text-ink-secondary"}`}>
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

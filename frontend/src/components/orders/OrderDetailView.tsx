"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fetchOrders } from "@/lib/orders-api";
import { formatINR } from "@/lib/money";
import { loadShipping } from "@/lib/shipping-store";
import type { Order } from "@/lib/types";
import OrderStatusBadge, { formatOrderDate } from "./OrderStatusBadge";
import PaymentLauncher from "../checkout/PaymentLauncher";

export default function OrderDetailView({
  orderId,
  paymentSuccess,
}: {
  orderId: number;
  paymentSuccess: boolean;
}) {
  const { status: authStatus } = useAuth();
  const { data: orders, isPending, isError, error } = useQuery({
    queryKey: ["orders"],
    queryFn: fetchOrders,
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
      <div className="border border-line p-10 text-center">
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

  if (isError || !orders) {
    return (
      <p className="text-ink-muted">Couldn&apos;t load this order.</p>
    );
  }

  const order: Order | undefined = orders.find((o) => o.id === orderId);
  if (!order) {
    return (
      <div className="border border-line p-10 text-center">
        <p className="font-display text-2xl tracking-tight">
          Order #{orderId} wasn&apos;t found.
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

  return (
    <div>
      {paymentSuccess && order.status === "confirmed" && (
        <div
          data-testid="payment-success-banner"
          className="mb-8 border border-line bg-surface p-6"
          role="status"
        >
          <h2 className="font-display text-2xl tracking-tight">
            Payment received — thank you.
          </h2>
          <p className="mt-2 text-sm text-ink-muted">
            Order #{order.id} is confirmed. A confirmation summary is below.
          </p>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-4">
        <h2 className="font-display text-3xl tracking-tight">
          Order #{order.id}
        </h2>
        <OrderStatusBadge status={order.status} />
        <span className="text-sm text-ink-muted">
          {formatOrderDate(order.created_at)}
        </span>
      </div>

      <div className="mt-8 grid gap-10 lg:grid-cols-[1fr_380px]">
        <div>
          <h3 className="text-sm font-medium uppercase tracking-wide text-ink-muted">
            Items
          </h3>
          <ul className="mt-3 divide-y divide-line border-y border-line">
            {order.items.map((item) => (
              <li
                key={item.id}
                className="flex items-baseline justify-between gap-3 py-3 text-sm"
              >
                <span>
                  {item.product_name}{" "}
                  <span className="text-ink-muted">
                    × {item.quantity} · {formatINR(item.price)} each
                  </span>
                </span>
                <span>{formatINR(item.subtotal)}</span>
              </li>
            ))}
          </ul>

          <dl className="mt-4 space-y-1 text-sm">
            <div className="flex justify-between">
              <dt className="text-ink-muted">Subtotal</dt>
              <dd>
                {formatINR(
                  order.items.reduce((s, i) => s + Number(i.subtotal), 0),
                )}
              </dd>
            </div>
            {Number(order.discount_amount) > 0 && (
              <div className="flex justify-between">
                <dt className="text-ink-muted">
                  Coupon {order.coupon ? `(${order.coupon})` : ""}
                </dt>
                <dd>−{formatINR(order.discount_amount)}</dd>
              </div>
            )}
            <div className="flex justify-between border-t border-line pt-2 text-base">
              <dt>Total</dt>
              <dd className="font-medium">{formatINR(order.total_amount)}</dd>
            </div>
          </dl>
        </div>

        <aside className="self-start border border-line p-6 text-sm lg:sticky lg:top-24">
          <h3 className="text-sm font-medium uppercase tracking-wide text-ink-muted">
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

          {order.status === "pending" && (
            <div className="mt-6 border-t border-line pt-6" data-testid="resume-payment-panel">
              <h3 className="text-sm font-medium uppercase tracking-wide text-ink-muted">
                Payment pending
              </h3>
              <p className="mt-2 text-xs text-ink-muted">
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

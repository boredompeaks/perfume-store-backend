"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fetchOrders } from "@/lib/orders-api";
import { formatINR } from "@/lib/money";
import OrderStatusBadge, { formatOrderDate } from "./OrderStatusBadge";

export default function OrdersListView() {
  const { status: authStatus } = useAuth();
  const { data: orders, isPending, isError, error, refetch } = useQuery({
    queryKey: ["orders"],
    queryFn: fetchOrders,
    retry: false,
  });

  if (authStatus === "loading" || (authStatus === "authenticated" && isPending)) {
    return (
      <p className="text-ink-muted" role="status">
        Loading your orders…
      </p>
    );
  }

  if (authStatus === "anonymous" || (error instanceof ApiError && error.status === 401)) {
    return (
      <div className="border border-line p-10 text-center">
        <p className="font-display text-2xl tracking-tight">
          Sign in to see your orders.
        </p>
        <Link
          href="/login?next=/orders"
          className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Sign in
        </Link>
      </div>
    );
  }

  if (isError || !orders) {
    return (
      <div className="border border-line p-10 text-center">
        <p className="text-ink-muted">Couldn&apos;t load your orders.</p>
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

  if (orders.length === 0) {
    return (
      <div className="border border-line p-16 text-center">
        <p className="font-display text-2xl tracking-tight">No orders yet.</p>
        <Link
          href="/products"
          className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Shop fragrances
        </Link>
      </div>
    );
  }

  return (
    <ul className="divide-y divide-line border-y border-line">
      {orders.map((order) => {
        const itemSummary = order.items
          .map((item) => `${item.product_name} × ${item.quantity}`)
          .join(" · ");

        return (
          <li key={order.id}>
            <Link
              href={`/orders/${order.id}`}
              className="block py-5 transition-colors hover:text-bronze"
            >
              <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
                <span className="font-display text-lg tracking-tight">
                  Order #{order.id}
                </span>
                <OrderStatusBadge status={order.status} />
                <span className="text-sm text-ink-muted">
                  {formatOrderDate(order.created_at)}
                </span>
                <span className="ml-auto text-sm">
                  {formatINR(order.total_amount)}
                </span>
              </div>
              <p className="mt-1 truncate text-sm text-ink-muted">
                {itemSummary}
              </p>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

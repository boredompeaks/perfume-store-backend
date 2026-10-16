"use client";

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fetchOrderHistoryPage } from "@/lib/orders-api";
import { formatINR } from "@/lib/money";
import OrderStatusBadge, { formatOrderDate } from "./OrderStatusBadge";

/**
 * §5.5 — order history. A list of transactions is a ledger: the only motion
 * on this page is the row hover (`--motion-hover-dur`, §3's orders-row row);
 * there is no entrance animation and no accent moment, by design (P2 —
 * none-by-design, and §5.5 line 1441 says so verbatim).
 *
 * Pagination reads the backend's own contract: `?page=` / `?page_size=` with
 * the house page-number envelope (backend/orders/views/order.py:48-79,
 * `page_size` capped by `ORDER_HISTORY_MAX_PAGE_SIZE`). The pager only
 * appears when the envelope says there is more than one page.
 */
export default function OrdersListView() {
  const { status: authStatus } = useAuth();
  const [page, setPage] = useState(1);
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: ["orders", page],
    queryFn: () => fetchOrderHistoryPage(page),
    retry: false,
  });

  if (authStatus === "loading" || (authStatus === "authenticated" && isPending)) {
    return (
      <p className="text-ink-secondary" role="status">
        Loading your orders…
      </p>
    );
  }

  if (authStatus === "anonymous" || (error instanceof ApiError && error.status === 401)) {
    return (
      <div className="border border-hairline p-10 text-center">
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

  if (isError || !data) {
    return (
      <div className="border border-hairline p-10 text-center">
        <p className="text-ink-secondary">Couldn&apos;t load your orders.</p>
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

  if (data.results.length === 0) {
    return (
      <div className="border border-hairline p-16 text-center">
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
    <div>
      <ul className="divide-y divide-hairline border-y border-hairline">
        {data.results.map((order) => {
          const itemSummary = order.items
            .map((item) => `${item.product_name} × ${item.quantity}`)
            .join(" · ");

          return (
            <li key={order.id}>
              <Link
                href={`/orders/${order.id}`}
                className="block py-5 transition-colors duration-(--motion-hover-dur) hover:text-bronze"
              >
                <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
                  <h3 className="font-display text-lg tracking-tight">
                    Order #{order.order_number}
                  </h3>
                  <OrderStatusBadge
                    status={order.status}
                    paymentStatus={order.payment_status}
                    fulfilmentStatus={order.fulfilment_status}
                  />
                  <span className="text-sm text-ink-secondary">
                    {formatOrderDate(order.created_at)}
                  </span>
                  <span className="ml-auto text-money tabular-nums lining-nums">
                    {formatINR(order.total_amount)}
                  </span>
                </div>
                <p className="mt-1 truncate text-sm text-ink-secondary">
                  {itemSummary}
                </p>
              </Link>
            </li>
          );
        })}
      </ul>

      {data.total_pages > 1 && (
        <nav
          aria-label="Order history pages"
          className="mt-8 flex items-center justify-between gap-4"
        >
          <button
            type="button"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={!data.previous_page}
            className="h-11 min-w-11 border border-field-border px-4 text-sm transition-colors hover:border-ink disabled:opacity-40"
          >
            ← Prev
          </button>
          <p className="text-sm text-ink-secondary tabular-nums lining-nums">
            Page {data.current_page} of {data.total_pages}
          </p>
          <button
            type="button"
            onClick={() => setPage((p) => p + 1)}
            disabled={!data.next_page}
            className="h-11 min-w-11 border border-field-border px-4 text-sm transition-colors hover:border-ink disabled:opacity-40"
          >
            Next →
          </button>
        </nav>
      )}
    </div>
  );
}

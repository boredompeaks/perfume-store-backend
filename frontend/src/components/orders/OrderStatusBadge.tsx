import type { OrderStatus } from "@/lib/types";

const labels: Record<OrderStatus, string> = {
  pending: "Pending payment",
  confirmed: "Confirmed",
  shipped: "Shipped",
  delivered: "Delivered",
  cancelled: "Cancelled",
};

/**
 * Labels mirror the backend's own choices; an unrecognised future value falls
 * through to the raw API string rather than being hidden.
 */
export const paymentLabels: Record<string, string> = {
  pending: "Pending",
  authorized: "Authorized",
  captured: "Captured",
  failed: "Failed",
  partially_refunded: "Partially refunded",
  refunded: "Refunded",
};

export const fulfilmentLabels: Record<string, string> = {
  unfulfilled: "Unfulfilled",
  partially_fulfilled: "Partially fulfilled",
  fulfilled: "Fulfilled",
};

/**
 * One badge cannot say both dimensions, so each renders as a chip only where
 * it differs from what the status word already tells the customer.
 */
const paymentShows: Record<OrderStatus, (payment: string) => boolean> = {
  // "Pending payment" already IS payment=pending; anything else is news.
  pending: (payment) => payment !== "pending",
  // On a cancelled order only a moved charge/refund is news.
  cancelled: (payment) =>
    payment === "captured" ||
    payment === "partially_refunded" ||
    payment === "refunded",
  // Confirmed/shipped/delivered read as paid (verify captures before it
  // confirms); a non-capture — COD still due, failure, refund — is news.
  confirmed: (payment) => payment !== "captured",
  shipped: (payment) => payment !== "captured",
  delivered: (payment) => payment !== "captured",
};

const fulfilmentShows: Record<OrderStatus, (fulfilment: string) => boolean> = {
  // "Pending payment" already reads as not-yet-on-its-way.
  pending: (fulfilment) => fulfilment !== "unfulfilled",
  cancelled: () => false,
  // "Confirmed" already implies unfulfilled; a pack before dispatch is news.
  confirmed: (fulfilment) => fulfilment !== "unfulfilled",
  // Shipped/delivered already imply fulfilled; a split shipment does not.
  shipped: (fulfilment) => fulfilment === "partially_fulfilled",
  delivered: (fulfilment) => fulfilment === "partially_fulfilled",
};

const chipClass =
  "inline-block border border-hairline px-2 py-0.5 text-xs uppercase tracking-wide text-ink-secondary";

/**
 * No green success styling: state is text plus tone, never colour alone. The
 * status word carries the fill; dimension chips stay muted.
 */
export default function OrderStatusBadge({
  status,
  paymentStatus,
  fulfilmentStatus,
}: {
  status: OrderStatus;
  /** Optional so non-order-read call sites keep compiling. */
  paymentStatus?: string;
  fulfilmentStatus?: string;
}) {
  const muted = status === "pending" || status === "cancelled";
  const payment =
    paymentStatus !== undefined && paymentShows[status](paymentStatus)
      ? (paymentLabels[paymentStatus] ?? paymentStatus)
      : null;
  const fulfilment =
    fulfilmentStatus !== undefined && fulfilmentShows[status](fulfilmentStatus)
      ? (fulfilmentLabels[fulfilmentStatus] ?? fulfilmentStatus)
      : null;

  return (
    <span className="inline-flex flex-wrap items-baseline gap-2">
      <span
        className={`inline-block px-2 py-0.5 text-xs uppercase tracking-wide ${
          muted ? "border border-hairline text-ink-secondary" : "bg-ink text-paper"
        }`}
      >
        {labels[status]}
      </span>
      {payment && (
        <span className={chipClass}>Payment: {payment}</span>
      )}
      {fulfilment && (
        <span className={chipClass}>Fulfilment: {fulfilment}</span>
      )}
    </span>
  );
}

export function formatOrderDate(iso: string): string {
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
  }).format(new Date(iso));
}

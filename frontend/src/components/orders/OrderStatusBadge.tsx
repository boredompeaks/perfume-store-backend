import type { OrderStatus } from "@/lib/types";

const labels: Record<OrderStatus, string> = {
  pending: "Pending payment",
  confirmed: "Confirmed",
  shipped: "Shipped",
  delivered: "Delivered",
  cancelled: "Cancelled",
};

/**
 * Dimension vocabulary is the backend's OWN human labels — nothing here is
 * invented (§0b.1, P5):
 * - `PAYMENT_STATUS_CHOICES` — backend/orders/state.py:130-137
 * - `FULFILMENT_STATUS_CHOICES` — backend/orders/state.py:139-143
 * An unrecognised future value falls through to the raw API string at the
 * call sites below rather than being hidden.
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
 * §5.5 (lines 1429-1436): the backend mirrors `status` from the two
 * dimensions (orders/serializers.py:97-98), but ONE badge cannot say both
 * "pending payment" and "pending, paid, awaiting dispatch" — so each
 * dimension renders as its own chip WHERE IT DIFFERS from what the status
 * word already tells the customer. A chip that merely repeats the status is
 * decoration (P2) and stays home.
 */
const paymentShows: Record<OrderStatus, (payment: string) => boolean> = {
  // "Pending payment" already IS payment=pending; anything else is news
  // (paid-but-pending per §5.5's own example, failed, refunded, …).
  pending: (payment) => payment !== "pending",
  // On a cancelled order only a moved charge/refund is news.
  cancelled: (payment) =>
    payment === "captured" ||
    payment === "partially_refunded" ||
    payment === "refunded",
  // Confirmed/shipped/delivered read as paid (verify captures before it
  // confirms — orders/views/payment.py:540-551); the non-captures — a COD
  // order still due, a failure, a refund — are the second fact.
  confirmed: (payment) => payment !== "captured",
  shipped: (payment) => payment !== "captured",
  delivered: (payment) => payment !== "captured",
};

const fulfilmentShows: Record<OrderStatus, (fulfilment: string) => boolean> = {
  // "Pending payment" already reads as "not yet on its way"; a partial
  // fulfilment at this stage would still be news.
  pending: (fulfilment) => fulfilment !== "unfulfilled",
  cancelled: () => false,
  // "Confirmed" already reads as not-yet-dispatched (the status would say
  // Shipped otherwise), so the unfulfilled default is covered — the pack
  // happening before dispatch (fulfilled / partially fulfilled) is the
  // second fact.
  confirmed: (fulfilment) => fulfilment !== "unfulfilled",
  // Shipped/delivered already imply fulfilled; a split shipment does not.
  shipped: (fulfilment) => fulfilment === "partially_fulfilled",
  delivered: (fulfilment) => fulfilment === "partially_fulfilled",
};

const chipClass =
  "inline-block border border-hairline px-2 py-0.5 text-xs uppercase tracking-wide text-ink-secondary";

/**
 * Tonal treatment is unchanged (§5.5 line 1435, §6): `bg-ink` for a live
 * status, hairline/muted for pending and cancelled, and **no green success
 * styling anywhere** — the dimension chips are always the muted hairline
 * tone; the status word alone carries the fill. State is text + tone, never
 * colour alone.
 */
export default function OrderStatusBadge({
  status,
  paymentStatus,
  fulfilmentStatus,
}: {
  status: OrderStatus;
  /** Optional so non-order-read call sites keep compiling; the two order surfaces always pass them. */
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

import type { OrderStatus } from "@/lib/types";

const labels: Record<OrderStatus, string> = {
  pending: "Pending payment",
  confirmed: "Confirmed",
  shipped: "Shipped",
  delivered: "Delivered",
  cancelled: "Cancelled",
};

export default function OrderStatusBadge({ status }: { status: OrderStatus }) {
  const muted = status === "pending" || status === "cancelled";
  return (
    <span
      className={`inline-block px-2 py-0.5 text-xs uppercase tracking-wide ${
        muted ? "border border-line text-ink-muted" : "bg-ink text-paper"
      }`}
    >
      {labels[status]}
    </span>
  );
}

export function formatOrderDate(iso: string): string {
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
  }).format(new Date(iso));
}

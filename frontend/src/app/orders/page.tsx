import type { Metadata } from "next";
import OrdersListView from "@/components/orders/OrdersListView";

export const metadata: Metadata = {
  title: "Your orders",
  robots: { index: false, follow: false },
};

export default function OrdersPage() {
  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <h1 className="font-display text-4xl tracking-tight">Your orders</h1>
      <div className="mt-8">
        <OrdersListView />
      </div>
    </div>
  );
}

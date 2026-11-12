import type { Metadata } from "next";
import OrdersListView from "@/components/orders/OrdersListView";

export const metadata: Metadata = {
  title: "Your orders",
  robots: { index: false, follow: false },
};

export default function OrdersPage() {
  return (
    <div className="mx-auto max-w-5xl px-4 py-16 sm:px-6 md:py-24">
      <header>
        <p className="text-eyebrow uppercase text-bronze">Order history</p>
        <h1 className="mt-1 font-display text-display">Your orders</h1>
      </header>
      <div className="mt-16">
        <OrdersListView />
      </div>
    </div>
  );
}

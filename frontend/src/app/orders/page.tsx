import type { Metadata } from "next";
import OrdersListView from "@/components/orders/OrdersListView";

export const metadata: Metadata = {
  title: "Your orders",
  robots: { index: false, follow: false },
};

export default function OrdersPage() {
  return (
    // Screen parity (U-W12-T9, maison_ther_vos_commandes): max-w-5xl register,
    // py-16/24, bronze eyebrow over the display serif title (the house
    // eyebrow+serif header from U-W11-T5), then the ledger at the screen's
    // mb-16 seam (the screen's md:mb-20 snaps to the §2.3 cadence's 64px step).
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

import type { Metadata } from "next";
import CheckoutView from "@/components/checkout/CheckoutView";

export const metadata: Metadata = {
  title: "Checkout",
  robots: { index: false, follow: false },
};

export default function CheckoutPage() {
  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      {/* The screen's fabricated folio-number eyebrow is deliberately not
          transcribed. */}
      <h1 className="border-b border-hairline pb-6 font-display text-display tracking-tight">
        Checkout
      </h1>
      <div className="mt-8">
        <CheckoutView />
      </div>
    </div>
  );
}

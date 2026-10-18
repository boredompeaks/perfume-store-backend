import type { Metadata } from "next";
import CheckoutView from "@/components/checkout/CheckoutView";

export const metadata: Metadata = {
  title: "Checkout",
  robots: { index: false, follow: false },
};

export default function CheckoutPage() {
  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      {/* D1 screen: display-scale h1 over the full-width header rule. Its
          right-hand "Folio d'Expédition · N° 0842" eyebrow is PLACEHOLDER
          content, not system (§0b) — a fabricated order number is exactly
          the lie P5 forbids, so it is not transcribed. */}
      <h1 className="border-b border-hairline pb-6 font-display text-display tracking-tight">
        Checkout
      </h1>
      <div className="mt-8">
        <CheckoutView />
      </div>
    </div>
  );
}

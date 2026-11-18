import type { Metadata } from "next";
import CartView from "@/components/cart/CartView";

export const metadata: Metadata = {
  title: "Cart",
  robots: { index: false, follow: false },
};

export default function CartPage() {
  // The h1 lives in CartView so the title band renders in every state; the
  // route is noindex, so there is no server-side heading to lose.
  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <CartView />
    </div>
  );
}

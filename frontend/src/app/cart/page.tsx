import type { Metadata } from "next";
import CartView from "@/components/cart/CartView";

export const metadata: Metadata = {
  title: "Cart",
  robots: { index: false, follow: false },
};

export default function CartPage() {
  // The h1 lives in CartView (U-W12-T7): the screen's title band pairs it
  // with the live unit count and the step rail, and CartView renders the
  // band in every state (skeleton / error / empty / populated), so the
  // heading never disappears. Client components still server-render their
  // initial HTML, and /cart is noindex (above), so nothing is lost.
  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <CartView />
    </div>
  );
}

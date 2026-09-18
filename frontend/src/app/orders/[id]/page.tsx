import type { Metadata } from "next";
import Link from "next/link";
import OrderDetailView from "@/components/orders/OrderDetailView";

export const metadata: Metadata = {
  title: "Order",
  robots: { index: false, follow: false },
};

type SearchParams = Record<string, string | string[] | undefined>;

export default async function OrderPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<SearchParams>;
}) {
  const { id } = await params;
  const sp = await searchParams;
  const paymentParam = Array.isArray(sp.payment) ? sp.payment[0] : sp.payment;
  const orderId = Number(id);

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <div className="mt-0">
        <Link href="/orders" className="text-sm text-ink-muted underline underline-offset-4 transition-colors hover:text-bronze" prefetch={false}>
          ← All orders
        </Link>
      </div>
      <div className="mt-6">
        {Number.isInteger(orderId) && orderId > 0 ? (
          <OrderDetailView
            orderId={orderId}
            paymentSuccess={paymentParam === "success"}
          />
        ) : (
          <p className="text-ink-muted">Order not found.</p>
        )}
      </div>
    </div>
  );
}

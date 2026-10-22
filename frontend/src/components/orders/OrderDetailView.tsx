"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fetchOrder, type OrderRecord } from "@/lib/orders-api";
import { formatINR } from "@/lib/money";
import { loadShipping } from "@/lib/shipping-store";
import {
  trackShipments,
  TrackingMissError,
  TrackingNetworkError,
  type Shipment,
} from "@/lib/tracking-api";
import OrderStatusBadge, {
  formatOrderDate,
  fulfilmentLabels,
  paymentLabels,
} from "./OrderStatusBadge";
import ShipmentPanel from "./ShipmentPanel";
import PaymentLauncher from "../checkout/PaymentLauncher";

/**
 * §5.5 — single order, fed by `GET /api/orders/{id}/` (CHANGE-13), not by
 * the list. The backend answers every miss (unknown id, someone else's
 * order) with the same uniform 404, and the UI does not try to tell those
 * cases apart (§5.5 line 1481-1484); a signed-out caller never reaches it —
 * that state renders the sign-in wall.
 *
 * Screen parity (U-W12-T9, maison_ther_order_detail_nested_tracking): the
 * folio header (bronze registry eyebrow over the serif order title, status
 * chip + timestamp right), the consignment-style progress panel (section
 * rule + counter, serif step labels, hairline square markers on the track),
 * the NESTED shipment tracking panel (waybill rectangle, estimated delivery,
 * checkpoint rail) fed by the existing `GET /api/shipping/track/{number}/`,
 * and the manifest column (items, money rows, serif total on an ink rule).
 * The screen's narrative headline/subtitle and fictional states (DISPATCHED
 * step names, "Active", lot numbers) are mockup drift: the backend's own
 * vocabulary and timestamps are the only data rendered (P5 / §0b.1).
 */
export default function OrderDetailView({
  orderId,
  paymentSuccess,
}: {
  orderId: number;
  paymentSuccess: boolean;
}) {
  const { status: authStatus } = useAuth();
  const { data: order, isPending, isError, error, refetch } = useQuery({
    // Keyed UNDER the "orders" prefix so PaymentLauncher's shared
    // `invalidateQueries({ queryKey: ["orders"] })` (PaymentLauncher.tsx:118)
    // refreshes this read after a successful verify — without it the
    // `?payment=success` banner would read a stale "pending" status. The
    // literal "detail" segment keeps it from ever colliding with the list's
    // numeric `["orders", page]` key.
    queryKey: ["orders", "detail", orderId],
    queryFn: () => fetchOrder(orderId),
    retry: false,
  });

  if (authStatus === "loading" || (authStatus === "authenticated" && isPending)) {
    return (
      <div aria-hidden="true" className="grid gap-10 lg:grid-cols-[1fr_380px]">
        <div className="space-y-4">
          <div className="skeleton h-8 w-40" />
          <div className="skeleton h-4 w-64" />
          <div className="skeleton h-24 w-full" />
          <div className="skeleton h-4 w-48" />
        </div>
        <div className="skeleton h-48 w-full" />
      </div>
    );
  }

  if (authStatus === "anonymous" || (error instanceof ApiError && error.status === 401)) {
    return (
      <div className="border border-hairline p-10 text-center">
        <p className="font-display text-2xl tracking-tight">
          Sign in to view this order.
        </p>
        <Link
          href={`/login?next=/orders/${orderId}`}
          className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Sign in
        </Link>
      </div>
    );
  }

  if (isError || !order) {
    // The uniform 404 (§5.5: the backend deliberately does not distinguish
    // "not yours" / "does not exist"). No id in the copy: `order_number` is
    // the customer-facing reference and it is exactly what a 404 does not
    // hand us, so the panel names nothing it cannot know.
    if (error instanceof ApiError && error.status === 404) {
      return (
        <div className="border border-hairline p-10 text-center">
          <p className="font-display text-2xl tracking-tight">
            Order not found.
          </p>
          <Link
            href="/orders"
            className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
          >
            All orders
          </Link>
        </div>
      );
    }

    // API-down / network failure: designed panel, not bare prose, and — like
    // money states — never animated.
    return (
      <div className="border border-hairline p-10 text-center">
        <p className="text-ink-secondary">Couldn&apos;t load this order.</p>
        <button
          type="button"
          onClick={() => refetch()}
          className="mt-4 bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Try again
        </button>
      </div>
    );
  }

  return (
    <div>
      {/* Screen's top indicator (chip + timestamp, right). The "← All
          orders" half lives in the server segment above this component
          (orders/[id]/page.tsx) so it renders without JS. The timestamp is
          hidden below sm — the screen itself hides it there (code.html
          line 31). */}
      <div className="flex flex-wrap items-center justify-end gap-4">
        <OrderStatusBadge
          status={order.status}
          paymentStatus={order.payment_status}
          fulfilmentStatus={order.fulfilment_status}
        />
        <span className="hidden text-sm text-ink-secondary tabular-nums lining-nums sm:inline">
          {formatOrderDate(order.created_at)}
        </span>
      </div>

      {/* §5.5 item 3 / §7.7 — the page's single h1. First in DOM so the
          payment-success h2 below never precedes it in the heading
          hierarchy; `id` never appears in customer-facing text. The folio
          line is the screen's "Vault Registry #ORD-…" eyebrow WITHOUT the
          number: the h1 below IS the reference, and repeating it would make
          the cancelled-order header read as a receipt narrative. */}
      <header className="mt-6 border-b border-hairline pb-8">
        <p className="text-eyebrow uppercase text-bronze">Vault registry</p>
        <h1 className="mt-2 font-display text-h1">
          Order {order.order_number}
        </h1>
      </header>

      {paymentSuccess && order.status === "confirmed" && (
        <div
          data-testid="payment-success-banner"
          className="mt-8 border border-hairline bg-surface p-6"
          role="status"
        >
          <h2 className="font-display text-h2">
            Payment received — thank you.
          </h2>
          <p className="mt-2 text-sm text-ink-secondary">
            Order {order.order_number} is confirmed. A confirmation summary is
            below.
          </p>
        </div>
      )}

      <div className="mt-10 grid gap-10 lg:grid-cols-[1fr_380px]">
        <div>
          <OrderJourney order={order} />
          <ShipmentTracking orderNumber={order.order_number} />

          {/* Screen's "Items in Consignment" header rule; the count is the
              real line quantity (P5), "flacons" the house vocabulary
              (HomeCarousel "View flacon"). */}
          <section aria-labelledby="order-items-heading" className="mt-12">
            <div className="flex items-baseline justify-between border-b border-hairline pb-3">
              <h2 id="order-items-heading" className="text-eyebrow uppercase text-ink">
                Items
              </h2>
              <span className="text-label-sm uppercase text-bronze">
                {order.items.reduce((s, i) => s + i.quantity, 0)} flacons
              </span>
            </div>
            <ul className="divide-y divide-hairline">
              {order.items.map((item) => (
                <li
                  key={item.id}
                  className="flex items-baseline justify-between gap-3 py-4 text-sm"
                >
                  <span className="min-w-0">
                    <span className="block font-display text-body">
                      {item.product_name} × {item.quantity}
                    </span>
                    {/* sku / variant_name are the frozen checkout snapshot
                        (models.py:440-456): rendered from this row, never a
                        live product read — the item cannot change after the
                        order. Empty today (no variant input yet), so both stay
                        out of the way until the backend fills them. */}
                    {item.variant_name &&
                      item.variant_name !== item.product_name && (
                        <span className="block text-xs text-ink-secondary">
                          {item.variant_name}
                        </span>
                      )}
                    {item.sku && (
                      <span className="block text-xs text-ink-secondary tabular-nums lining-nums">
                        SKU {item.sku}
                      </span>
                    )}
                    <span className="block text-xs text-ink-secondary tabular-nums lining-nums">
                      {formatINR(item.price)} each
                    </span>
                  </span>
                  <span className="text-money tabular-nums lining-nums">
                    {formatINR(item.subtotal)}
                  </span>
                </li>
              ))}
            </ul>

            <dl className="mt-6 space-y-1 text-sm">
              <div className="flex justify-between">
                <dt className="text-ink-secondary">Subtotal</dt>
                <dd className="tabular-nums lining-nums">
                  {/* Display-only sum of the server's own per-line subtotals —
                      no pricing rule runs client-side (P5 / A-13). */}
                  {formatINR(
                    order.items.reduce((s, i) => s + Number(i.subtotal), 0),
                  )}
                </dd>
              </div>
              {Number(order.discount_amount) > 0 && (
                <div className="flex justify-between">
                  <dt className="text-ink-secondary">
                    Coupon {order.coupon ? `(${order.coupon})` : ""}
                  </dt>
                  <dd className="tabular-nums lining-nums">
                    −{formatINR(order.discount_amount)}
                  </dd>
                </div>
              )}
              {/* §5.5 item 6 — the shipping lines the serializer now ships. */}
              <div className="flex justify-between">
                <dt className="text-ink-secondary">Shipping</dt>
                <dd className="tabular-nums lining-nums">
                  {formatINR(order.shipping_amount)}
                </dd>
              </div>
              {order.shipping_method_code && (
                <div className="flex justify-between">
                  <dt className="text-ink-secondary">Delivery option</dt>
                  <dd>{order.shipping_method_code}</dd>
                </div>
              )}
              {/* Screen's total treatment: eyebrow label, ink rule, serif
                  tabular figure (§2.2 permits Playfair on money-adjacent
                  display). */}
              <div className="mt-3 flex items-baseline justify-between border-t border-ink pt-3">
                <dt className="text-eyebrow uppercase text-ink">Total</dt>
                <dd className="font-display text-h3 tabular-nums lining-nums">
                  {formatINR(order.total_amount)}
                </dd>
              </div>
            </dl>
          </section>
        </div>

        <aside className="self-start border border-hairline p-6 text-sm lg:sticky lg:top-24">
          <h2 className="text-eyebrow uppercase text-bronze">Delivery to</h2>
          <address className="mt-3 not-italic leading-relaxed">
            {order.full_name}
            <br />
            {order.address}
            <br />
            {order.city}, {order.state} {order.pincode}
            <br />
            {order.phone}
          </address>

          {payable(order) && (
            <div
              className="mt-6 border-t border-hairline pt-6"
              data-testid="resume-payment-panel"
            >
              <h3 className="text-sm font-medium uppercase tracking-wide text-ink-secondary">
                Payment pending
              </h3>
              <p className="mt-2 text-xs text-ink-secondary">
                This order hasn&apos;t been paid yet. You can complete the
                payment safely — the amount is locked server-side.
              </p>
              <div className="mt-4">
                <PaymentLauncher
                  order={order}
                  contact={loadShipping() ?? {}}
                  buttonLabel="Complete payment"
                />
              </div>
            </div>
          )}
        </aside>
      </div>

      <ShipmentPanel orderNumber={order.order_number} />
    </div>
  );
}

/**
 * §5.5 item 8 says "resume payment when `payment_status` is unpaid" — the
 * backend's vocabulary has no `unpaid`, and its own precondition is the
 * source of truth: `payment/` refuses anything whose `status` is not
 * `pending` (backend/orders/views/payment.py:92-94) and treats a `failed`
 * payment as retryable (orders/state.py:178). So an order is resumable when
 * the machine says it can be paid AND the money has not landed.
 */
function payable(order: OrderRecord): boolean {
  return (
    order.status === "pending" &&
    (order.payment_status === "pending" ||
      order.payment_status === "failed" ||
      order.payment_status === "authorized")
  );
}

/**
 * §5.5 item 4 — the lifecycle timeline. The six timestamps are exactly what
 * `OrderSerializer` ships (backend/orders/serializers.py:131-136); the two
 * machine dimensions ride as current-state rows (serializers.py:97-98).
 * `OrderStatusEvent` rows are NOT in the projection, so nothing reads them.
 *
 * Unreached steps print their label and no date — that is the state, and it
 * is the customer's actual question ("what is still to come", §5.5).
 *
 * Screen parity (U-W12-T9): section rule with a counter right (the screen's
 * "Step 03 of 04" is replaced by the REAL milestone count over the six
 * timestamps — P5, no step machine is invented), serif step labels, square
 * hairline markers on the vertical track (already the house treatment).
 *
 * Accent moment (§5.5 line 1478, P2): the markers settle in on
 * `animation-timeline: view()` via the existing `.settle` class — reading
 * position mapped onto journey position, declared inside
 * `prefers-reduced-motion: no-preference` + `@supports` (globals.css:525-544),
 * so reduced motion and unsupported browsers render every marker already
 * filled, and the same status is printed as text at the top of the page.
 */
function OrderJourney({ order }: { order: OrderRecord }) {
  const steps: {
    label: string;
    value?: string;
    current?: boolean;
    at?: string | null;
  }[] = [
    {
      label: "Payment",
      value: paymentLabels[order.payment_status] ?? order.payment_status,
      current: true,
    },
    {
      label: "Fulfilment",
      value:
        fulfilmentLabels[order.fulfilment_status] ?? order.fulfilment_status,
      current: true,
    },
    { label: "Paid", at: order.paid_at },
    { label: "Fulfilled", at: order.fulfilled_at },
    { label: "Shipped", at: order.shipped_at },
    { label: "Delivered", at: order.delivered_at },
    { label: "Cancelled", at: order.cancelled_at },
    { label: "Refunded", at: order.refunded_at },
  ];

  const milestones = steps.filter((step) => !step.current);
  const reached = milestones.filter((step) => step.at).length;

  return (
    <section aria-labelledby="order-journey-heading">
      <div className="flex items-baseline justify-between border-b border-hairline pb-3">
        <h2
          id="order-journey-heading"
          className="text-eyebrow uppercase text-ink"
        >
          Order journey
        </h2>
        <span className="text-label-sm uppercase tabular-nums text-bronze">
          {reached} of {milestones.length} milestones
        </span>
      </div>
      <ol className="mt-8">
        {steps.map((step, i) => {
          const isReached = step.current ?? Boolean(step.at);
          const last = i === steps.length - 1;
          return (
            <li key={step.label} className="flex gap-4">
              <span aria-hidden="true" className="relative w-2 shrink-0">
                {/* Marker sits at left-0 (its 8px = the 8px column), NOT
                    centred with a translate: `.settle` animates `transform`,
                    and a translated marker would lose its offset mid-flight.
                    The rule below carries the translate; it never animates. */}
                <span
                  className={`settle absolute left-0 top-1.5 h-2 w-2 ${
                    isReached
                      ? "bg-ink"
                      : "border border-hairline bg-surface"
                  }`}
                />
                {!last && (
                  <span className="absolute bottom-0 left-1/2 top-4 w-px -translate-x-1/2 bg-hairline" />
                )}
              </span>
              <span className="flex min-w-0 flex-1 flex-wrap items-baseline justify-between gap-x-4 gap-y-1 pb-6">
                <span
                  className={`font-display text-body ${
                    isReached ? "text-ink" : "text-ink-secondary"
                  }`}
                >
                  {step.label}
                </span>
                {step.current && step.value ? (
                  <span className="border border-hairline px-2 py-0.5 text-xs uppercase tracking-wide text-ink-secondary">
                    {step.value}
                  </span>
                ) : step.at ? (
                  <time
                    dateTime={step.at}
                    className="text-xs text-ink-secondary tabular-nums lining-nums"
                  >
                    {formatOrderDate(step.at)}
                  </time>
                ) : (
                  <span className="sr-only">Not yet</span>
                )}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/**
 * Presentation of a status VALUE, never a new status: the backend's five
 * values (label_created, dispatched, in_transit, delivered, exception)
 * spelled the way its own labels spell them. Underscores to spaces, first
 * letter up; nothing renamed, nothing invented (P5). Mirrors /track-order's
 * humanizer — same data, one vocabulary across both tracking surfaces.
 */
function humanizeStatus(value: string): string {
  const flat = value.replace(/_/g, " ").trim();
  return flat.charAt(0).toUpperCase() + flat.slice(1);
}

function formatInstant(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function formatDeliveryDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString(undefined, { dateStyle: "medium" });
}

/**
 * §5.5 item 5 — shipment tracking, nested under the journey (screen:
 * the tracking panel rides inside the active consignment step). Fed by the
 * SAME endpoint the public tracker uses, `GET /api/shipping/track/
 * {order_number}/` (tracking-api.ts): an authenticated caller's session JWT
 * rides as Bearer and the backend authorises the owner. ONE block per
 * parcel, because a split shipment is explicitly modelled
 * (shipping/views.py:129). Only the six customer-facing fields render —
 * `internal_note` is deliberately absent from the serializer and stays out.
 *
 * The miss is the backend's ONE uniform 404 ("no credential", "wrong token",
 * "no such order", "someone else's order", "no shipment yet" are the same
 * byte) — so this panel prints a single quiet line and never differentiates
 * (the anti-oracle rule). A network failure is a different thing entirely
 * (tracking-api.ts) and gets its own line; neither offers a retry here, the
 * page's own refetch covers the order and this query re-runs on remount.
 */
function ShipmentTracking({ orderNumber }: { orderNumber: string }) {
  const { data, isPending, isError, error } = useQuery({
    queryKey: ["orders", "tracking", orderNumber],
    queryFn: () => trackShipments(orderNumber),
    retry: false,
  });

  const miss = isError && error instanceof TrackingMissError;
  const offline = isError && error instanceof TrackingNetworkError;
  const quiet = miss || (data !== undefined && data.length === 0);

  return (
    <section aria-labelledby="shipment-tracking-heading" className="mt-12">
      <div className="flex items-baseline justify-between border-b border-hairline pb-3">
        <h2
          id="shipment-tracking-heading"
          className="text-eyebrow uppercase text-ink"
        >
          Shipment tracking
        </h2>
        {data && data.length > 0 && (
          <span className="text-label-sm uppercase text-bronze">
            {data.length} {data.length === 1 ? "parcel" : "parcels"}
          </span>
        )}
      </div>
      {isPending && (
        <p role="status" className="mt-4 text-sm text-ink-secondary">
          Looking up your parcel…
        </p>
      )}
      {quiet && (
        <p className="mt-4 text-sm text-ink-secondary">
          Tracking will appear here once your order ships.
        </p>
      )}
      {offline && (
        <p className="mt-4 text-sm text-ink-secondary">
          Tracking is unavailable right now.
        </p>
      )}
      {data && data.length > 0 && (
        <div className="mt-6 space-y-10">
          {data.map((parcel, i) => (
            <ParcelPanel
              key={`${parcel.order_number}-${parcel.tracking_number}-${i}`}
              parcel={parcel}
              index={i}
              total={data.length}
            />
          ))}
        </div>
      )}
    </section>
  );
}

/**
 * One parcel in the screen's nested-tracking treatment: status chip, waybill
 * rectangle (select-all, tabular so digits are unambiguous — §5.5 item 5),
 * carrier, estimated delivery, then the checkpoint rail. Checkpoints keep
 * the API's append order (`ShipmentEvent._meta.ordering = (created_at, id)`,
 * oldest first) with the LATEST scan emphasised — the same semantics
 * /track-order's parcel block already ships; the screen's newest-first list
 * is mockup presentation, not data.
 */
function ParcelPanel({
  parcel,
  index,
  total,
}: {
  parcel: Shipment;
  index: number;
  total: number;
}) {
  const last = parcel.events.length - 1;
  return (
    <article>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <p className="text-eyebrow uppercase text-ink">
          {total > 1 ? `Parcel ${index + 1} of ${total}` : "Consignment"}
        </p>
        <span className="border border-hairline px-2 py-0.5 text-xs uppercase tracking-wide text-ink-secondary">
          {humanizeStatus(parcel.status)}
        </span>
      </div>
      <div className="mt-3 flex flex-wrap items-start justify-between gap-x-8 gap-y-4 border-b border-hairline pb-4">
        <div>
          <p className="text-eyebrow uppercase text-ink-secondary">
            Waybill reference
          </p>
          <p className="mt-1 inline-block select-all border border-hairline px-2.5 py-1 text-sm tabular-nums lining-nums">
            {parcel.tracking_number}
          </p>
          <p className="mt-2 text-sm text-ink-secondary">{parcel.carrier}</p>
        </div>
        {parcel.estimated_delivery && (
          <div className="sm:text-right">
            <p className="text-eyebrow uppercase text-ink-secondary">
              Estimated delivery
            </p>
            <p className="mt-1 text-sm tabular-nums lining-nums">
              {formatDeliveryDate(parcel.estimated_delivery)}
            </p>
          </div>
        )}
      </div>
      {parcel.events.length > 0 && (
        <div className="mt-4">
          <p className="text-eyebrow uppercase text-bronze">Checkpoints</p>
          <ol className="mt-3 space-y-3 border-l border-hairline pl-4">
            {parcel.events.map((event, i) => (
              <li
                key={`${event.status}-${event.occurred_at}-${i}`}
                className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:gap-4"
              >
                <span className="w-36 shrink-0 text-xs text-ink-secondary tabular-nums lining-nums">
                  {formatInstant(event.occurred_at)}
                </span>
                <span
                  className={
                    i === last
                      ? "text-sm font-medium text-ink"
                      : "text-sm text-ink-secondary"
                  }
                >
                  {humanizeStatus(event.status)}
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </article>
  );
}

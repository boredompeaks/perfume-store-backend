"use client";

import { useQuery } from "@tanstack/react-query";
import { safeExternalUrl } from "@/lib/shipping-api";
import {
  trackShipments,
  TrackingMissError,
  TrackingNetworkError,
  type Shipment,
} from "@/lib/tracking-api";

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
 * This is the order page's ONLY tracking panel. D-02's separate
 * `ShipmentPanel` queried the same endpoint and duplicated the whole block;
 * its `provider` and `tracking_url` treatments are carried here so one
 * panel still shows everything a customer may follow.
 *
 * The miss is the backend's ONE uniform 404 ("no credential", "wrong token",
 * "no such order", "someone else's order", "no shipment yet" are the same
 * byte) — so this panel prints a single quiet line and never differentiates
 * (the anti-oracle rule). A network failure is a different thing entirely
 * (tracking-api.ts) and gets its own line; neither offers a retry here, the
 * page's own refetch covers the order and this query re-runs on remount.
 */
export default function ShipmentTracking({
  orderNumber,
}: {
  orderNumber: string;
}) {
  const { data, isPending, isError, error } = useQuery({
    queryKey: ["orders", "tracking", orderNumber],
    queryFn: () => trackShipments(orderNumber),
    retry: false,
  });

  const miss = isError && error instanceof TrackingMissError;
  const offline = isError && error instanceof TrackingNetworkError;
  const quiet = miss || (data !== undefined && data.length === 0);

  return (
    <section
      aria-labelledby="shipment-tracking-heading"
      className="mt-12"
      data-testid="shipment-tracking"
    >
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
 * carrier, provider, a safe external tracking link, estimated delivery, then
 * the checkpoint rail. Checkpoints keep the API's append order
 * (`ShipmentEvent._meta.ordering = (created_at, id)`, oldest first) with the
 * LATEST scan emphasised — the same semantics /track-order's parcel block
 * already ships; the screen's newest-first list is mockup presentation, not
 * data.
 *
 * `tracking_url` is operator-configured (a provider template), so it is
 * untrusted at render time: `safeExternalUrl` admits only absolute http(s)
 * URLs and every other value produces no anchor at all.
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
  const href = safeExternalUrl(parcel.tracking_url);
  return (
    <article data-testid="shipment-parcel">
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
          {parcel.provider && (
            <p className="mt-1 text-sm text-ink-secondary">
              via {parcel.provider}
            </p>
          )}
          {href && (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-3 inline-block text-sm underline underline-offset-4 transition-colors hover:text-bronze"
            >
              Track this parcel
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          )}
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

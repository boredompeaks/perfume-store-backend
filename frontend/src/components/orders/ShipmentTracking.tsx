"use client";

import { useQuery } from "@tanstack/react-query";
import { safeExternalUrl } from "@/lib/shipping-api";
import {
  trackShipments,
  TrackingMissError,
  TrackingNetworkError,
  type Shipment,
} from "@/lib/tracking-api";

/** Mirrors /track-order's humanizer: same data, one vocabulary. */
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
 * Same endpoint as the public tracker; the session JWT authorises the owner.
 * One block per parcel because split shipments are explicitly modelled, and
 * only customer-facing fields render (`internal_note` stays out). The miss is
 * the backend's uniform 404; a network failure is separate and retryable.
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
 * Checkpoints keep the API's oldest-first append order with the latest scan
 * emphasised. `tracking_url` is operator-configured, so only absolute http(s)
 * URLs become anchors via `safeExternalUrl`.
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

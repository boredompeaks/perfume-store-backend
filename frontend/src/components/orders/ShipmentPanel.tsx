"use client";

import { useQuery } from "@tanstack/react-query";
import {
  fetchShipments,
  safeExternalUrl,
  shipmentStatusLabel,
} from "@/lib/shipping-api";
import { formatOrderDate } from "./OrderStatusBadge";

function formatTimestamp(iso: string): string {
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(iso));
}

/**
 * D-02: the order detail page's Shipments panel.
 *
 * Reads the customer tracking endpoint by order NUMBER (never the pk). A
 * 404 and an order with no parcels yet are the same customer-facing fact —
 * "nothing to track yet" — so both render the empty state instead of an
 * error; the backend deliberately makes them byte-identical. A parcel's
 * tracking link is rendered only when `safeExternalUrl` admits it and is
 * always an external tab with `noopener noreferrer`.
 */
export default function ShipmentPanel({ orderNumber }: { orderNumber: string }) {
  const { data, isPending, isError } = useQuery({
    queryKey: ["shipments", orderNumber],
    queryFn: () => fetchShipments(orderNumber),
    retry: false,
  });

  const shipments = data ?? [];
  const isEmpty = !isPending && (isError || shipments.length === 0);

  return (
    <section
      aria-labelledby="shipments-heading"
      className="mt-8 border border-line p-6"
      data-testid="shipment-panel"
    >
      <h3
        id="shipments-heading"
        className="text-sm font-medium uppercase tracking-wide text-ink-muted"
      >
        Shipments
      </h3>

      {isPending && (
        <p role="status" className="mt-3 text-sm text-ink-muted">
          Loading tracking…
        </p>
      )}

      {isEmpty && (
        <p data-testid="shipments-empty" className="mt-3 text-sm text-ink-muted">
          Tracking will appear here once your order ships.
        </p>
      )}

      {shipments.length > 0 && (
        <ul className="mt-4 space-y-6">
          {shipments.map((shipment, index) => {
            const href = safeExternalUrl(shipment.tracking_url);
            return (
              <li
                key={`${shipment.tracking_number}-${index}`}
                data-testid="shipment-parcel"
                className="border-t border-line pt-4 first:border-t-0 first:pt-0"
              >
                <div className="flex flex-wrap items-center gap-3">
                  <span className="inline-block bg-ink px-2 py-0.5 text-xs uppercase tracking-wide text-paper">
                    {shipmentStatusLabel(shipment.status)}
                  </span>
                  <span className="text-sm text-ink-muted">
                    {shipment.carrier || "Carrier pending"}
                  </span>
                  {shipment.provider && (
                    <span className="text-sm text-ink-muted">
                      via {shipment.provider}
                    </span>
                  )}
                </div>

                <dl className="mt-3 space-y-1 text-sm">
                  <div className="flex justify-between gap-4">
                    <dt className="text-ink-muted">Tracking number</dt>
                    <dd>{shipment.tracking_number}</dd>
                  </div>
                  {shipment.estimated_delivery && (
                    <div className="flex justify-between gap-4">
                      <dt className="text-ink-muted">Estimated delivery</dt>
                      <dd>
                        <time dateTime={shipment.estimated_delivery}>
                          {formatOrderDate(shipment.estimated_delivery)}
                        </time>
                      </dd>
                    </div>
                  )}
                </dl>

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

                {shipment.events.length > 0 && (
                  <div className="mt-4">
                    <h4 className="text-xs font-medium uppercase tracking-wide text-ink-muted">
                      Timeline
                    </h4>
                    <ol className="mt-2 space-y-1 border-l border-line pl-4 text-sm">
                      {shipment.events.map((event, eventIndex) => (
                        <li key={`${event.occurred_at}-${eventIndex}`}>
                          <time
                            dateTime={event.occurred_at}
                            className="text-ink-muted"
                          >
                            {formatTimestamp(event.occurred_at)}
                          </time>
                          {" — "}
                          {shipmentStatusLabel(event.status)}
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

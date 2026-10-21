import { apiFetch } from "./api";
import type { Shipment } from "./types";

/**
 * D-02: the customer tracking read behind `/orders/[id]`.
 *
 * Keyed on the customer-facing order number, never the pk. The bearer is
 * sent when one exists so the backend scopes the read to the signed-in
 * account's own orders; the endpoint itself is credential-guarded either
 * way (`shipping.views._trackable_shipments`), and a miss is one uniform
 * 404 — the panel renders its empty state for that.
 */
export function fetchShipments(orderNumber: string): Promise<Shipment[]> {
  const path = `/api/shipping/track/${encodeURIComponent(orderNumber)}/`;
  return apiFetch<{ shipments: Shipment[] }>(path, { auth: true }).then(
    (body) => body.shipments,
  );
}

/**
 * The only URLs the panel will turn into a link.
 *
 * `tracking_url` is operator-configured data (a provider template), so it is
 * untrusted at render time: scheme-check it here and never rely on React to
 * neutralise `javascript:` in an href. Anything that is not an absolute
 * http(s) URL is not a link at all, which is why `new URL` is used rather
 * than a prefix test (it rejects malformed and protocol-relative values).
 */
export function safeExternalUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const parsed = new URL(raw);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? parsed.toString()
      : null;
  } catch {
    return null;
  }
}

const SHIPMENT_STATUS_LABELS: Record<string, string> = {
  label_created: "Label created",
  dispatched: "Dispatched",
  in_transit: "In transit",
  delivered: "Delivered",
  exception: "Exception",
};

/** Human label for a parcel/event status; unknown values stay readable. */
export function shipmentStatusLabel(status: string): string {
  return SHIPMENT_STATUS_LABELS[status] ?? status.replace(/_/g, " ");
}

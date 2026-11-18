/**
 * Guest token header wins when supplied, otherwise a session JWT rides as
 * Bearer. Every failure is the backend's one uniform 404 (anti-oracle), and a
 * network failure is separate. Own transport because apiFetch cannot attach
 * the guest header.
 */
import { API_BASE } from "./config";
import { getAccessToken } from "./tokens";

export const GUEST_ORDER_TOKEN_HEADER = "X-Guest-Order-Token";
/** Mirrors shipping/views.py GUEST_TOKEN_MAX_LENGTH; longer is ignored. */
export const GUEST_TOKEN_MAX_LENGTH = 64;

/** One customer-visible step of the trail. */
export type ShipmentEvent = {
  status: string;
  occurred_at: string;
};

/** One parcel as far as a customer may see it. */
export type Shipment = {
  order_number: string;
  status: string;
  carrier: string;
  /** Provider slug code, or null when the parcel is unlinked. */
  provider: string | null;
  tracking_number: string;
  /** Operator-configured URL - render only through safeExternalUrl(). */
  tracking_url: string | null;
  estimated_delivery: string | null;
  events: ShipmentEvent[];
};

export type TrackingResponse = {
  shipments: Shipment[];
};

/** The uniform miss: server message verbatim, one class for every HTTP error. */
export class TrackingMissError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TrackingMissError";
  }
}

/** Fetch failed outright — no response at all. The only retryable outcome. */
export class TrackingNetworkError extends Error {
  constructor() {
    super("network");
    this.name = "TrackingNetworkError";
  }
}

/**
 * Look up every parcel the caller is entitled to see; an explicit guest token
 * wins over an in-memory session JWT.
 */
export async function trackShipments(
  orderNumber: string,
  guestToken?: string,
): Promise<Shipment[]> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = (guestToken ?? "").trim();
  if (token) {
    headers[GUEST_ORDER_TOKEN_HEADER] = token;
  } else {
    const jwt = getAccessToken();
    if (jwt) headers.Authorization = `Bearer ${jwt}`;
  }

  let res: Response;
  try {
    res = await fetch(
      `${API_BASE}/api/shipping/track/${encodeURIComponent(
        orderNumber.trim(),
      )}/`,
      { method: "GET", headers, credentials: "include" },
    );
  } catch {
    throw new TrackingNetworkError();
  }

  if (!res.ok) {
    // The backend's message verbatim; the fallback keeps the uniform shape
    // when the body is missing or unparseable.
    let message = "";
    try {
      const body = (await res.json()) as { error?: unknown };
      if (body && typeof body.error === "string") message = body.error;
    } catch {
      // empty or non-JSON body
    }
    throw new TrackingMissError(message || "Shipment not found");
  }

  const data = (await res.json()) as TrackingResponse;
  return Array.isArray(data?.shipments) ? data.shipments : [];
}

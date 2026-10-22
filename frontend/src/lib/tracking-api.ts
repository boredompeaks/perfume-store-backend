/**
 * Customer shipment tracking — GET /api/shipping/track/<order_number>/.
 *
 * Verified against backend/shipping/views.py `ShipmentTrackingView` and
 * `shipping/urls.py` (the number alone authorizes nothing). The credential is
 * exactly one of two things, and this module sends the one the caller has:
 *
 *  - `X-Guest-Order-Token: <token>` — the guest token minted at checkout
 *    (SPEC-1-B04), when the customer supplies it. Backend cap: 64 chars;
 *    an over-length or wrong token is NOT an error there, it is the same
 *    uniform miss as every other failure, so nothing is pre-checked here.
 *  - `Authorization: Bearer <jwt>` — only when NO token was supplied and a
 *    session access token exists; an authenticated caller owns the order by
 *    account (`order__user`).
 *
 * This deliberately does NOT use `apiFetch`, which cannot attach the guest
 * header. Same error discipline, own transport.
 *
 * The miss is ONE answer (`_tracking_lookup_miss`): a 404 whose body is
 * `{"error": "Shipment not found"}`, byte-identical for "no credential",
 * "wrong token", "no such order", "someone else's order" and "no shipment
 * yet". `TrackingMissError` carries that message verbatim and the page must
 * never differentiate the cases — the backend spent audit cycles making sure
 * it cannot be used as an order-existence oracle, and a UI that guesses
 * would undo that. A network failure is a different thing entirely and gets
 * its own error class (the only case the page offers "Try again" for).
 *
 * No auto-fetch: this module has no side effects and is called only from an
 * explicit form submit.
 */
import { API_BASE } from "./config";
import { getAccessToken } from "./tokens";

export const GUEST_ORDER_TOKEN_HEADER = "X-Guest-Order-Token";
/** Mirrors shipping/views.py GUEST_TOKEN_MAX_LENGTH; longer is ignored. */
export const GUEST_TOKEN_MAX_LENGTH = 64;

/** One customer-visible step of the trail (ShipmentEventSerializer). */
export type ShipmentEvent = {
  status: string;
  occurred_at: string;
};

/** One parcel as far as a customer may see it (ShipmentTrackingSerializer). */
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
 * Look up every parcel of `orderNumber` the caller is entitled to see.
 * `guestToken` wins when present (the customer typed it for this lookup);
 * otherwise an in-memory session JWT rides as Bearer, if one exists.
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
    // The backend's message verbatim. The fallback only fires when an error
    // body is missing or unparseable (e.g. a proxy page), and echoes the
    // backend's own uniform string so the miss box stays one shape.
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

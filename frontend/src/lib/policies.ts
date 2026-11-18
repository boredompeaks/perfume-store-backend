import { API_BASE } from "./config";

/**
 * The only place the return window may be read: a number renders only when
 * `GET /api/settings/` publishes a positive integer. Absent, malformed, `0`
 * or a dead backend yields `{}` — never a fabricated default.
 */
export type ReturnWindow = { days?: number };

/**
 * Server-side only, with the same 60s cache as `getContactSettings` so React
 * dedupes the shared endpoint within one request.
 */
export async function getReturnWindow(): Promise<ReturnWindow> {
  try {
    const res = await fetch(`${API_BASE}/api/settings/`, {
      next: { revalidate: 60 },
    });
    if (!res.ok) return {};
    const data: unknown = await res.json();
    const days =
      typeof data === "object" && data !== null
        ? (data as { return_window_days?: unknown }).return_window_days
        : undefined;
    if (typeof days === "number" && Number.isInteger(days) && days > 0) {
      return { days };
    }
    return {};
  } catch {
    return {};
  }
}

/**
 * Single edit point for the dispatch/delivery copy; consumers render these
 * verbatim. Replace with an API binding once the settings endpoint publishes
 * those fields (never invent a number).
 */
export const shippingPolicy = {
  /** Orders are "typically dispatched within … of payment confirmation". */
  dispatchWindow: "2–4 business days",
  /** Delivery "usually takes a further … depending on your location". */
  deliveryWindow: "2–7 days",
} as const;

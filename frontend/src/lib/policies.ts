import { API_BASE } from "./config";

/**
 * The return-window policy the storefront is allowed to state, and the ONLY
 * place that reads it (spec A-12.D.2: the refs published three contradictory
 * windows, so no copy module may carry its own number — a claim renders the
 * value the API publishes or it renders no number at all).
 *
 * `days` is present ONLY when `GET /api/settings/` answers with a positive
 * integer — the column is a PositiveIntegerField, so anything else (a string,
 * a fractional day count) is malformed data, not a smaller claim. It also
 * deliberately excludes the store's own closed state: the
 * backend resolves a published `0` to "returns closed", and "within 0 days"
 * is not a sentence the copy may say, so callers drop the number exactly as
 * they do when the field is absent. A non-ok response, a malformed body or a
 * dead backend all yield `{}` — never a default, because a fabricated
 * fallback is the defect this module exists to prevent.
 */
export type ReturnWindow = { days?: number };

/**
 * Server-side only: fetch the resolved return window with the same 60s cache
 * as `getContactSettings` (same endpoint, so React dedupes the two within a
 * request). `async` server components `await` this and branch their copy on
 * `days`.
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
 * The dispatch and delivery windows `/shipping` may state — the ONLY place
 * they are written (same single-source rule as the return window above: the
 * refs published contradictory delivery claims, so no page may type its own
 * number — the numbers below are the policy copy, verbatim).
 *
 * SINGLE EDIT POINT for this copy. `GET /api/settings/` answers today with
 * contact channels plus `return_window_days` only — there is no dispatch
 * field yet (spec A-12.G11). When the future `dispatch_window_days` /
 * `delivery_window_days` fields land on that endpoint, replace this const
 * with an async binding shaped like `getReturnWindow`: same endpoint, same
 * 60s cache, and "state a number only when the API publishes a positive
 * integer", with no fabricated fallback when the field is absent. Until then
 * these are client-side policy constants; consumers import them from here
 * and render them verbatim.
 */
export const shippingPolicy = {
  /** Orders are "typically dispatched within … of payment confirmation". */
  dispatchWindow: "2–4 business days",
  /** Delivery "usually takes a further … depending on your location". */
  deliveryWindow: "2–7 days",
} as const;

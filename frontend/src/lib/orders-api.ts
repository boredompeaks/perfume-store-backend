import { API_BASE } from "./config";
import { ApiError, apiFetch } from "./api";
import { getGuestToken, setGuestToken } from "./guest-token";
import type { CouponPreview, Order, OrderItem, PaymentSession } from "./types";

/**
 * The order READ record — every field the customer-facing order surfaces
 * (list rows, detail header, lifecycle timeline, totals) consume. Every
 * member is verified field-by-field against the backend's projections; a
 * field the serializers do not ship is deliberately absent here rather than
 * invented (§0b.1):
 *
 * - `OrderSerializer.fields` — backend/orders/serializers.py:78-137:
 *   `order_number` :80, `payment_status` / `fulfilment_status` :97-98,
 *   `shipping_amount` / `shipping_method_code` :112-120, `currency` :124,
 *   the six lifecycle timestamps :131-136.
 * - `OrderItemSerializer.fields` — backend/orders/serializers.py:16-29:
 *   `sku` :20, `variant_name` :21, `currency` :28.
 *
 * NOT here: `OrderStatusEvent` rows. The detail serializer never exposes
 * them, so §5.5's lifecycle timeline is fed by the six timestamps below —
 * exactly what the spec names, nothing more.
 */
export type OrderItemRecord = OrderItem & {
  /** Frozen at checkout (backend/orders/models.py:448-451); empty string today, never a live lookup. */
  sku: string;
  /** Frozen at checkout (models.py:453-456); mirrors the product name until variants ship. */
  variant_name: string;
  currency: string;
};

export type OrderRecord = Omit<Order, "items"> & {
  items: OrderItemRecord[];
  /** Customer-facing reference ORD-YYYY-NNNNNN; `id` stays in the URL/API only (§5.5, §12-G4). */
  order_number: string;
  payment_status: string;
  fulfilment_status: string;
  shipping_amount: string;
  /** Frozen delivery-option code — `""` (never null) when the order was priced with no shipping configured (models.py:257-262). */
  shipping_method_code: string;
  currency: string;
  paid_at: string | null;
  fulfilled_at: string | null;
  shipped_at: string | null;
  delivered_at: string | null;
  cancelled_at: string | null;
  refunded_at: string | null;
};

/**
 * The guest read credential's only wire home (backend
 * orders/views/checkout.py GUEST_TOKEN_HEADER). Header, never a path or
 * query: a URL lands in access logs, history and Referer, and this value is
 * the whole authorization for the order's PII.
 */
export const GUEST_TOKEN_HEADER = "X-Guest-Order-Token";

/**
 * Session-cookie coupon preview; no JWT needed. Explicit-submit only — the
 * backend endpoint is unthrottled (V-11), so never per keystroke.
 */
export function applyCoupon(code: string): Promise<CouponPreview> {
  return apiFetch<CouponPreview>("/api/orders/apply-coupon/", {
    method: "POST",
    body: { code },
  });
}

export type CheckoutPayload = {
  full_name: string;
  phone: string;
  address: string;
  city: string;
  state: string;
  pincode: string;
  coupon_code?: string;
  /**
   * The delivery OPTION chosen on the checkout delivery step (spec 5.4 /
   * backend orders/views/checkout.py SHIPPING_METHOD_FIELD): a method CODE
   * from `GET /api/shipping/estimate/` (the `method` slug), never an amount.
   * The server prices it; the client never sends money.
   */
  shipping_method?: string;
};

type CheckoutResponse = Order & {
  /**
   * Present only on a response that actually mints a guest credential
   * (spec §12-G1 / CHANGE-11). Verified against the current backend:
   * OrderSerializer never carries `guest_token`, and the TIER1-GUEST-DEADEND
   * refusal answers every anonymous submission with 403 and mints nothing —
   * so today this field is always absent and the store below never fires.
   * It is read-if-present, never fabricated.
   */
  guest_token?: string;
};

/**
 * Creates the Order — server computes the total; client never sends amounts.
 *
 * The JWT rides only when one exists (`auth: true` attaches it if held), so the
 * signed-in request is byte-identical to before; an anonymous caller sends
 * no Authorization header and the backend decides (its verified contract
 * refuses guests with one uniform 403). On a success that carries a guest
 * token, the credential is stashed for the guest order read below.
 *
 * `idempotencyKey` (spec 5.4 step 2, backend IDEMPOTENCY_KEY_HEADER) is the
 * caller's per-attempt key: stable across retries of ONE order attempt, so a
 * replay returns the original order instead of minting a second one (length
 * cap 128 server-side). Omitted when the caller has no attempt to key.
 */
export async function checkout(
  payload: CheckoutPayload,
  idempotencyKey?: string,
): Promise<Order> {
  const order = await apiFetch<CheckoutResponse>("/api/orders/checkout/", {
    method: "POST",
    body: payload,
    auth: true,
    headers: idempotencyKey
      ? { "Idempotency-Key": idempotencyKey }
      : undefined,
  });
  if (order.guest_token) setGuestToken(order.guest_token);
  return order;
}

/** Idempotent per order — safe to call again for a retry of an unpaid order. */
export function createPayment(orderId: number): Promise<PaymentSession> {
  return apiFetch<PaymentSession>("/api/orders/payment/", {
    method: "POST",
    body: { order_id: orderId },
    auth: true,
  });
}

export type VerifyResult = {
  message: string;
  order_id: number;
  status: string;
  razorpay_payment_id: string;
};

export function verifyPayment(payload: {
  razorpay_order_id: string;
  razorpay_payment_id: string;
  razorpay_signature: string;
  order_id: number;
}): Promise<VerifyResult> {
  return apiFetch<VerifyResult>("/api/orders/payment/verify/", {
    method: "POST",
    body: payload,
    auth: true,
  });
}

/**
 * Customer order history (SPEC-9-04): paginated, newest first, using the
 * house page-number envelope (same shape as the products listing).
 * Unwrapped here so list consumers keep working on page 1; a pager UI can
 * adopt `fetchOrderHistoryPage` later. The single-order endpoint
 * (GET /api/orders/<id>/, owner-checked) now exists per the same spec item.
 */
export type OrderHistoryPage = {
  count: number;
  total_pages: number;
  current_page: number;
  next_page: boolean;
  previous_page: boolean;
  results: OrderRecord[];
};

export function fetchOrderHistoryPage(
  page = 1,
  pageSize?: number,
): Promise<OrderHistoryPage> {
  const params = new URLSearchParams({ page: String(page) });
  if (pageSize !== undefined) params.set("page_size", String(pageSize));
  return apiFetch<OrderHistoryPage>(`/api/orders/?${params}`, { auth: true });
}

export function fetchOrders(): Promise<OrderRecord[]> {
  return fetchOrderHistoryPage().then((page) => page.results);
}

/**
 * CHANGE-13 / §5.5: the detail page is fed by its OWN keyed read —
 * `GET /api/orders/<id>/` (backend/orders/views/order.py:84). Owner-checked:
 * the caller's order only, one uniform 404 for unknown-or-foreign ids
 * (order.py:91-95 — the UI must not distinguish those cases), 401 when
 * signed out.
 */
export function fetchOrder(orderId: number): Promise<OrderRecord> {
  return apiFetch<OrderRecord>(`/api/orders/${orderId}/`, { auth: true });
}

/**
 * A guest's OWN order: GET /api/orders/guest/ with the token alone (it is
 * globally unique, so it identifies exactly one order), or
 * GET /api/orders/guest/<order_number>/ when the caller also has the number
 * as a cross-check. The credential rides the X-Guest-Order-Token header only.
 *
 * Every miss — no stored token, a wrong token, an unknown number — surfaces
 * the backend's one uniform 404 "Order not found" (an anti-oracle answer:
 * it must not confirm which orders exist), mirrored locally when no token is
 * held so the two cases are indistinguishable to the caller.
 *
 * Transport: apiFetch has no per-request header option, and making this
 * bearer credential a generic knob would be the wrong default — the one
 * request that carries it builds its own RequestInit, so the header stays
 * at this single call site.
 */
export async function fetchGuestOrder(orderNumber?: string): Promise<Order> {
  const token = getGuestToken();
  if (!token) throw new ApiError(404, "Order not found");

  const path = orderNumber
    ? `/api/orders/guest/${encodeURIComponent(orderNumber)}/`
    : "/api/orders/guest/";
  const res = await fetch(`${API_BASE}${path}`, {
    method: "GET",
    headers: { [GUEST_TOKEN_HEADER]: token },
    credentials: "include",
  });

  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body: unknown = await res.json();
      if (
        body !== null &&
        typeof body === "object" &&
        typeof (body as { error?: unknown }).error === "string"
      ) {
        message = (body as { error: string }).error;
      }
    } catch {
      // empty body — keep the status fallback
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as Order;
}

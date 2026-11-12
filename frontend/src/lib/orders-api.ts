import { API_BASE } from "./config";
import { ApiError, apiFetch } from "./api";
import { getGuestToken, setGuestToken } from "./guest-token";
import type { CouponPreview, Order, OrderItem, PaymentSession } from "./types";

/**
 * Every field is verified against the backend's order projections; anything
 * the serializers do not ship is deliberately absent rather than invented.
 */
export type OrderItemRecord = OrderItem & {
  /** Frozen at checkout; empty string today. */
  sku: string;
  /** Frozen at checkout; mirrors the product name until variants ship. */
  variant_name: string;
  currency: string;
};

export type OrderRecord = Omit<Order, "items"> & {
  items: OrderItemRecord[];
  /** Customer-facing reference; `id` stays in the URL/API only. */
  order_number: string;
  payment_status: string;
  fulfilment_status: string;
  shipping_amount: string;
  /** Frozen delivery-option code; `""` (never null) when no shipping was configured. */
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
 * Header only, never a path or query: a URL lands in access logs, history and
 * Referer, and this value is the whole authorization for the order's PII.
 */
export const GUEST_TOKEN_HEADER = "X-Guest-Order-Token";

/**
 * Explicit-submit only: the endpoint is unthrottled. The bearer is sent when
 * one exists so a signed-in customer prices their account cart.
 */
export function applyCoupon(code: string): Promise<CouponPreview> {
  return apiFetch<CouponPreview>("/api/orders/apply-coupon/", {
    method: "POST",
    body: { code },
    auth: true,
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
  /** Server-priced method CODE; the client never sends money. */
  shipping_method?: string;
};

type CheckoutResponse = Order & {
  /** Read-if-present; the current backend never mints this. */
  guest_token?: string;
};

/**
 * Server computes the total. The JWT attaches only when held; anonymous
 * callers send no Authorization header and the backend answers one uniform
 * 403. A returned guest token is stashed for the guest order read.
 *
 * `idempotencyKey` is stable across retries of one attempt, so a replay
 * returns the original order (server cap 128 chars).
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

/** Order history uses the house page-number envelope. */
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
 * Owner-checked: one uniform 404 for unknown and foreign ids (the UI must not
 * distinguish them), 401 when signed out.
 */
export function fetchOrder(orderId: number): Promise<OrderRecord> {
  return apiFetch<OrderRecord>(`/api/orders/${orderId}/`, { auth: true });
}

/**
 * A guest's own order, identified by the token alone (or with the order
 * number as a cross-check). Every miss returns the backend's one uniform 404,
 * mirrored locally when no token is held so the cases stay indistinguishable.
 *
 * Own transport because apiFetch cannot attach the guest header, and making
 * this credential a generic knob would be the wrong default.
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
      // empty body: keep the status fallback
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as Order;
}

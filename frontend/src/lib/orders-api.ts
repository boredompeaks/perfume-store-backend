import { apiFetch } from "./api";
import type { CouponPreview, Order, PaymentSession } from "./types";

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
};

/** Creates the Order — server computes the total; client never sends amounts. */
export function checkout(payload: CheckoutPayload): Promise<Order> {
  return apiFetch<Order>("/api/orders/checkout/", {
    method: "POST",
    body: payload,
    auth: true,
  });
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

/** Unpaginated list, newest first. No single-order endpoint exists (BACKEND_REQUESTS). */
export function fetchOrders(): Promise<Order[]> {
  return apiFetch<Order[]>("/api/orders/", { auth: true });
}

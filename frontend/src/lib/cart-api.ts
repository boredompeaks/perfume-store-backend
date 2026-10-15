import { apiFetch } from "./api";
import type { Cart } from "./types";

/**
 * GET /api/cart/ body — the house cart shape plus the persisted coupon
 * field (R-9.3.5; the serializer emits `coupon_code`, a code string or
 * null). Declared here, not in types.ts, to avoid colliding with the
 * pending backend field drops there.
 */
export type CartWithCoupon = Cart & { coupon_code: string | null };

/** GET creates the session + cart lazily on the backend (documented side effect). */
export function getCart(): Promise<CartWithCoupon> {
  return apiFetch<CartWithCoupon>("/api/cart/");
}

export function addToCart(product_id: number, quantity: number): Promise<Cart> {
  return apiFetch<Cart>("/api/cart/", {
    method: "POST",
    body: { product_id, quantity },
  });
}

export function patchCartItem(itemId: number, quantity: number): Promise<Cart> {
  return apiFetch<Cart>(`/api/cart/${itemId}/`, {
    method: "PATCH",
    body: { quantity },
  });
}

export function deleteCartItem(itemId: number): Promise<Cart> {
  return apiFetch<Cart>(`/api/cart/${itemId}/`, { method: "DELETE" });
}

/**
 * POST /api/cart/coupon/ — persist a coupon on the session cart (R-9.3.5).
 * Returns the full cart body (with coupon_code set). Session-cookie auth
 * with CSRF, like every cart mutation. Errors: 400 {error} for a missing
 * code or the uniform invalid-coupon rejection, 404 {error} when the
 * session has no cart.
 */
export function applyCartCoupon(code: string): Promise<CartWithCoupon> {
  return apiFetch<CartWithCoupon>("/api/cart/coupon/", {
    method: "POST",
    body: { code },
  });
}

/**
 * DELETE /api/cart/coupon/ — remove the persisted coupon. Idempotent on
 * the backend: succeeds even when no coupon is set, returning the cart
 * body so the client can re-render without a follow-up GET.
 */
export function removeCartCoupon(): Promise<CartWithCoupon> {
  return apiFetch<CartWithCoupon>("/api/cart/coupon/", { method: "DELETE" });
}

export function cartCount(cart: Cart | undefined | null): number {
  return cart ? cart.items.reduce((n, item) => n + item.quantity, 0) : 0;
}

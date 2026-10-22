import { apiFetch } from "./api";
import type { Cart } from "./types";

/**
 * D-04a/D-04b: the cart a request owns follows the credential. Sending the
 * access token (when one exists) makes these calls resolve the ACCOUNT cart;
 * a signed-out browser sends none and keeps its session cart. Without this
 * the post-login refetch would keep reading the — now merged away — guest
 * cart and render an empty basket.
 */
const CART_AUTH = { auth: true } as const;

/**
 * GET /api/cart/ body — the house cart shape plus the persisted coupon
 * field (R-9.3.5; the serializer emits `coupon_code`, a code string or
 * null).
 */
export type CartWithCoupon = Cart & { coupon_code: string | null };

/** GET creates the resolved cart lazily on the backend (documented side effect). */
export function getCart(): Promise<CartWithCoupon> {
  return apiFetch<CartWithCoupon>("/api/cart/", CART_AUTH);
}

export function addToCart(product_id: number, quantity: number): Promise<Cart> {
  return apiFetch<Cart>("/api/cart/", {
    ...CART_AUTH,
    method: "POST",
    body: { product_id, quantity },
  });
}

export function patchCartItem(itemId: number, quantity: number): Promise<Cart> {
  return apiFetch<Cart>(`/api/cart/${itemId}/`, {
    ...CART_AUTH,
    method: "PATCH",
    body: { quantity },
  });
}

export function deleteCartItem(itemId: number): Promise<Cart> {
  return apiFetch<Cart>(`/api/cart/${itemId}/`, {
    ...CART_AUTH,
    method: "DELETE",
  });
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
    ...CART_AUTH,
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
  return apiFetch<CartWithCoupon>("/api/cart/coupon/", {
    ...CART_AUTH,
    method: "DELETE",
  });
}

export function cartCount(cart: Cart | undefined | null): number {
  return cart ? cart.items.reduce((n, item) => n + item.quantity, 0) : 0;
}

import { apiFetch } from "./api";
import type { Cart } from "./types";

/**
 * Sending the access token when one exists resolves the account cart;
 * signed-out browsers send none and keep the session cart.
 */
const CART_AUTH = { auth: true } as const;

/** The cart body plus the persisted coupon code (string or null). */
export type CartWithCoupon = Cart & { coupon_code: string | null };

/** GET lazily creates the resolved cart on the backend. */
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
 * Persists the coupon and returns the full cart body. CSRF applies like every
 * cart mutation; 400 for an invalid code, 404 when the session has no cart.
 */
export function applyCartCoupon(code: string): Promise<CartWithCoupon> {
  return apiFetch<CartWithCoupon>("/api/cart/coupon/", {
    ...CART_AUTH,
    method: "POST",
    body: { code },
  });
}

/**
 * Idempotent: succeeds even with no coupon set and returns the cart body so
 * the client can re-render without a follow-up GET.
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

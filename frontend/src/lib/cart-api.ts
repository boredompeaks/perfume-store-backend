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

/** GET creates the resolved cart lazily on the backend (documented side effect). */
export function getCart(): Promise<Cart> {
  return apiFetch<Cart>("/api/cart/", CART_AUTH);
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

export function cartCount(cart: Cart | undefined | null): number {
  return cart ? cart.items.reduce((n, item) => n + item.quantity, 0) : 0;
}

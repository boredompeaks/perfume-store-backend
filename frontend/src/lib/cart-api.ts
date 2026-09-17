import { apiFetch } from "./api";
import type { Cart } from "./types";

/** GET creates the session + cart lazily on the backend (documented side effect). */
export function getCart(): Promise<Cart> {
  return apiFetch<Cart>("/api/cart/");
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

export function cartCount(cart: Cart | undefined | null): number {
  return cart ? cart.items.reduce((n, item) => n + item.quantity, 0) : 0;
}

"use client";

import { useQuery } from "@tanstack/react-query";
import { useAuth, type AuthStatus } from "./auth";
import { getCart } from "./cart-api";

/**
 * The access token is memory-only, so a cart read during the async auth
 * bootstrap would cache an anonymous basket and show an empty cart after
 * sign-in. Reads stay gated on the settled bootstrap, and the identity in the
 * key gives each identity its own cache slot while keeping the ["cart"]
 * prefix invalidation working.
 */
export const cartQueryKey = (status: AuthStatus) => ["cart", status] as const;

export function cartQueryOptions(status: AuthStatus) {
  return {
    queryKey: cartQueryKey(status),
    queryFn: getCart,
    enabled: status !== "loading",
    staleTime: 15_000,
    retry: false,
  };
}

/** The one cart read used by the header badge, the cart page and checkout. */
export function useCart() {
  const { status } = useAuth();
  return useQuery(cartQueryOptions(status));
}

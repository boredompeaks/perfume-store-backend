"use client";

import { useQuery } from "@tanstack/react-query";
import { useAuth, type AuthStatus } from "./auth";
import { getCart } from "./cart-api";

/**
 * D-04b cycle 1: the access token is memory-only, so on a full page load the
 * auth bootstrap settles asynchronously ("loading" -> "anonymous" |
 * "authenticated"). A cart GET fired during "loading" leaves with no bearer,
 * resolves (and lazily creates) the anonymous session cart, and caches that
 * empty basket — the post-refresh render then reuses it even though the
 * backend had already merged the guest basket into the account (observed as
 * an empty cart page after sign-in).
 *
 * Two guards, both required:
 *  - `enabled` keeps every cart read behind the settled bootstrap;
 *  - the identity segment in the key gives each identity its own cache slot,
 *    so an anonymous result is never served to an authenticated session (and
 *    the login/register ["cart"] prefix invalidation still matches).
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

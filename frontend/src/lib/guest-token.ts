/**
 * Guest order-token storage (U-W4-F1): the `X-Guest-Order-Token` credential
 * for a guest's own order, held in sessionStorage per the user-approved G1
 * decision (ui-ledger: tab-scoped; standing decision, reversible).
 *
 * The rules this module's shape enforces:
 * - The token never enters a URL, query string or log line. It travels only
 *   in the X-Guest-Order-Token request header (backend:
 *   orders/views/checkout.py GUEST_TOKEN_HEADER); nothing here writes to
 *   console, and callers pass the value straight to fetch.
 * - sessionStorage is tab-scoped: closing the tab ends the credential.
 *   Deliberately NO tab/storage/page-lifecycle listeners — the one clear
 *   point is an explicit logout (AuthProvider.logout), never an event.
 * - SSR: there is no window during server render, so every accessor degrades
 *   to "no token" instead of throwing. Storage being unavailable (private
 *   mode) behaves the same way: absent, never a crash.
 */

/**
 * Key constant, non-secret by construction — the VALUE is the credential,
 * never the key. Namespaced like the `aurel.session` hint: the token is
 * scoped to one API origin, not to a white-label brand.
 */
export const GUEST_TOKEN_STORAGE_KEY = "aurel.guest-order";

export function getGuestToken(): string | null {
  try {
    if (typeof window === "undefined") return null;
    const value = window.sessionStorage.getItem(GUEST_TOKEN_STORAGE_KEY);
    return value && value.trim() !== "" ? value : null;
  } catch {
    return null;
  }
}

/** Stores the credential verbatim; blank input is never a usable token. */
export function setGuestToken(token: string): void {
  if (!token || token.trim() === "") return;
  try {
    if (typeof window === "undefined") return;
    window.sessionStorage.setItem(GUEST_TOKEN_STORAGE_KEY, token);
  } catch {
    // Storage unavailable: behave as "no token" rather than fail the caller.
  }
}

/** The explicit-logout clear point. No other caller, no event binding. */
export function clearGuestToken(): void {
  try {
    if (typeof window === "undefined") return;
    window.sessionStorage.removeItem(GUEST_TOKEN_STORAGE_KEY);
  } catch {
    // ignore — the tab's storage is going away anyway
  }
}

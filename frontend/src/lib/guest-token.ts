/**
 * Guest order credential in sessionStorage (tab-scoped): header-only, never a
 * URL or log, no lifecycle listeners (explicit logout is the one clear point),
 * and every accessor degrades to "no token" during SSR or when storage fails.
 */

/** Non-secret key; the stored VALUE is the credential. */
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

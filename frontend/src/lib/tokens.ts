import { API_BASE } from "./config";
import { csrfTokenFromCookie } from "./api";

/**
 * Refresh-token storage contract (SPEC-17-02, R-17.12): the refresh token
 * never enters JS-readable storage — the backend sets it as an HttpOnly
 * cookie scoped to /api/ and reads it back on refresh/logout. Only the
 * short-lived access token is held here, in memory. "aurel.session" is a
 * NON-SECRET boolean hint that a refresh cookie may exist; it gates the
 * boot-time refresh probe so anonymous page loads don't burn the 'auth'
 * throttle budget. It must never hold a token.
 */
const SESSION_HINT_KEY = "aurel.session";

let accessToken: string | null = null;
let refreshInFlight: Promise<string | null> | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

/** Memory-only by design: no token value is ever persisted. */
export function setTokens(access: string): void {
  accessToken = access;
  markSession();
}

export function clearTokens(): void {
  accessToken = null;
  try {
    localStorage.removeItem(SESSION_HINT_KEY);
  } catch {
    // ignore
  }
}

/** Non-secret hint that the backend may have set a refresh cookie. */
export function hasSessionHint(): boolean {
  try {
    return localStorage.getItem(SESSION_HINT_KEY) === "1";
  } catch {
    return false;
  }
}

function markSession(): void {
  try {
    localStorage.setItem(SESSION_HINT_KEY, "1");
  } catch {
    // Storage unavailable (private mode) — session-only auth.
  }
}

/** Single-flight: concurrent 401s share one refresh call. */
export function refreshAccessToken(): Promise<string | null> {
  if (!refreshInFlight) {
    refreshInFlight = doRefresh().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

async function doRefresh(): Promise<string | null> {
  try {
    // No token in the body: the backend reads its own HttpOnly cookie and
    // answers with the new access token (rotation re-sets the cookie
    // server-side). The auth-throttled endpoint also bounds this call.
    // The CSRF header rides along (SPEC-17-03): a browser that built a
    // guest cart sends its sessionid cookie on this POST too, so the
    // backend's CSRF gate applies to it exactly like any other mutation.
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    const csrfToken = csrfTokenFromCookie();
    if (csrfToken) headers["X-CSRFToken"] = csrfToken;
    const res = await fetch(`${API_BASE}/api/accounts/token/refresh/`, {
      method: "POST",
      headers,
      credentials: "include",
      body: JSON.stringify({}),
    });
    if (!res.ok) {
      // The server rejected the session (dead/expired cookie): the hint is
      // a lie now — clear it. Transient network failures keep state.
      clearTokens();
      return null;
    }
    const data = (await res.json()) as { access?: string };
    if (!data.access) return null;
    accessToken = data.access;
    markSession();
    return data.access;
  } catch {
    return null;
  }
}

/** The JWT payload carries only user_id — the backend has no /me endpoint yet. */
export function decodeJwtUserId(token: string): number | null {
  try {
    const part = token.split(".")[1];
    const json = atob(part.replace(/-/g, "+").replace(/_/g, "/"));
    const payload = JSON.parse(json) as { user_id?: number };
    return typeof payload.user_id === "number" ? payload.user_id : null;
  } catch {
    return null;
  }
}

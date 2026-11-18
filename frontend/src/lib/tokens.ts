import { API_BASE } from "./config";
import { csrfTokenFromCookie } from "./api";

/**
 * The refresh token is an HttpOnly cookie and never enters JS-readable
 * storage; only the short-lived access token lives here in memory.
 * "aurel.session" is a non-secret hint that gates the boot refresh probe.
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
    // No token in the body: the backend reads its own HttpOnly cookie. The
    // CSRF header rides along because a guest session cookie may exist too.
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
      // The server rejected the session: the hint is stale. Transient
      // network failures keep state.
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

/** The JWT carries only user_id; there is no /me endpoint. */
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

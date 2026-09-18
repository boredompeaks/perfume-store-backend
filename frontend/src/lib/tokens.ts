import { API_BASE } from "./config";

const REFRESH_STORAGE_KEY = "aurel.refresh";

let accessToken: string | null = null;
let refreshInFlight: Promise<string | null> | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

export function setTokens(access: string, refresh?: string): void {
  accessToken = access;
  if (refresh) {
    try {
      localStorage.setItem(REFRESH_STORAGE_KEY, refresh);
    } catch {
      // Storage unavailable (private mode) — session-only auth.
    }
  }
}

export function clearTokens(): void {
  accessToken = null;
  try {
    localStorage.removeItem(REFRESH_STORAGE_KEY);
  } catch {
    // ignore
  }
}

export function getStoredRefreshToken(): string | null {
  try {
    return localStorage.getItem(REFRESH_STORAGE_KEY);
  } catch {
    return null;
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
  const refresh = getStoredRefreshToken();
  if (!refresh) return null;
  try {
    const res = await fetch(`${API_BASE}/api/accounts/token/refresh/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ refresh }),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { access?: string };
    if (!data.access) return null;
    accessToken = data.access;
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

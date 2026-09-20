import { API_BASE } from "./config";
import { clearTokens, getAccessToken, refreshAccessToken } from "./tokens";

export type FieldErrors = Record<string, string[]>;

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public fieldErrors?: FieldErrors,
    public extras?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type Opts = {
  method?: string;
  body?: unknown;
  /** Attach the JWT and enable silent-refresh-on-401. */
  auth?: boolean;
  /** Next server-fetch caching (ISR) for server-component data calls. */
  revalidate?: number;
};

const MUTATION_METHODS = new Set(["POST", "PATCH", "PUT", "DELETE"]);

/**
 * CSRF slot: if the backend ever starts issuing a `csrftoken` cookie
 * (e.g. SessionAuthentication lands per BACKEND_REQUESTS), mutations attach
 * `X-CSRFToken` automatically. Today the backend never issues one, so this is
 * a no-op — but it means the CSRF fix needs zero frontend retrofit.
 */
function csrfTokenFromCookie(): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

/**
 * Normalizes DRF error shapes:
 * {error: string, code: string, details: {...}} (SPEC-9-03 envelope)
 * | {error: string} | {detail: string} | {field: [msgs], …} | {password: [msgs]}
 * Envelope `details` content is promoted: arrays become fieldErrors, scalars
 * become extras (e.g. minimum_order_amount). Legacy flat shapes still parse.
 */
function parseErrorBody(
  body: unknown,
  fallback: string,
): {
  message: string;
  fieldErrors?: FieldErrors;
  extras?: Record<string, unknown>;
} {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const fieldErrors: FieldErrors = {};
    const extras: Record<string, unknown> = {};
    let first = "";
    const takeValue = (key: string, value: unknown) => {
      if (Array.isArray(value)) {
        const msgs = value.map(String);
        fieldErrors[key] = msgs;
        if (!first) first = msgs[0];
      } else if (
        (key === "error" || key === "detail") &&
        typeof value === "string"
      ) {
        if (!first) first = value;
      } else {
        extras[key] = value;
      }
    };
    for (const [key, value] of Object.entries(b)) {
      if (
        key === "details" &&
        value &&
        typeof value === "object" &&
        !Array.isArray(value)
      ) {
        // SPEC-9-03 envelope: field errors and scalar context live here.
        for (const [dk, dv] of Object.entries(value as Record<string, unknown>)) {
          takeValue(dk, dv);
        }
      } else {
        takeValue(key, value);
      }
    }
    const result: {
      message: string;
      fieldErrors?: FieldErrors;
      extras?: Record<string, unknown>;
    } = { message: first || fallback };
    if (Object.keys(fieldErrors).length > 0) result.fieldErrors = fieldErrors;
    if (Object.keys(extras).length > 0) result.extras = extras;
    return result;
  }
  return { message: fallback };
}

/**
 * Every request sends credentials: 'include' — the cart is a Django session
 * cookie and checkout is broken without it (see PLAN.md §1).
 */
export async function apiFetch<T>(
  path: string,
  opts: Opts = {},
  isRetry = false,
): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.auth) {
    const token = getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  if (MUTATION_METHODS.has(method)) {
    const csrfToken = csrfTokenFromCookie();
    if (csrfToken) headers["X-CSRFToken"] = csrfToken;
  }

  const init: RequestInit & { next?: { revalidate: number } } = {
    method,
    headers,
    credentials: "include",
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  };
  if (opts.revalidate !== undefined) init.next = { revalidate: opts.revalidate };

  const res = await fetch(`${API_BASE}${path}`, init);

  // Single silent refresh, then one retry. No loops, no silent failures —
  // a failed refresh clears tokens and the caller handles logged-out state.
  if (res.status === 401 && opts.auth && !isRetry) {
    const refreshed = await refreshAccessToken();
    if (refreshed) return apiFetch<T>(path, opts, true);
    clearTokens();
  }

  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      // empty body
    }
    const { message, fieldErrors, extras } = parseErrorBody(
      body,
      `Request failed (${res.status})`,
    );
    throw new ApiError(res.status, message, fieldErrors, extras);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

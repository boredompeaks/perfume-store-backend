// The session cookie is Lax, so the API must be same-site with the page.
// Defaulting to a fixed loopback spelling (127.0.0.1 vs localhost) made every
// request mint a fresh empty cart, so resolve from the page's own host.
const DEV_API_PORT = 8000;

export type PageLocation = { protocol: string; hostname: string };

/**
 * `page` is the browser's location (null during SSR, where the server talks
 * to the API over loopback regardless of the visitor's hostname).
 */
export function resolveApiBase(
  override: string | undefined,
  page: PageLocation | null,
): string {
  if (override) return override;
  if (!page) return `http://localhost:${DEV_API_PORT}`;
  return `${page.protocol}//${page.hostname}:${DEV_API_PORT}`;
}

export const API_BASE = resolveApiBase(
  process.env.NEXT_PUBLIC_API_BASE_URL,
  typeof window === "undefined"
    ? null
    : { protocol: window.location.protocol, hostname: window.location.hostname },
);

/** Media paths arrive relative (serialized without request context). */
export function mediaUrl(path: string | null | undefined): string | null {
  if (!path) return null;
  return path.startsWith("http") ? path : `${API_BASE}${path}`;
}

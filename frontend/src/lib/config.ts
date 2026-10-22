// The cart lives in the session cookie, and a Lax cookie is only sent when
// the API is SAME-SITE with the page. Defaulting the API to a fixed loopback
// spelling (127.0.0.1) silently broke every page opened at the other one
// (localhost): each request arrived without `sessionid`, so the backend
// minted a brand-new, empty cart on every call. Without an explicit override
// the API is therefore addressed on the host the page itself was served from,
// which keeps the pair same-site whichever loopback spelling is used.
const DEV_API_PORT = 8000;

export type PageLocation = { protocol: string; hostname: string };

/**
 * Resolve the API origin.
 *
 * `page` is the browser's `location` (null during SSR, where the server talks
 * to the API over loopback regardless of the hostname a visitor used).
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

/**
 * Product image paths come back relative ("/media/…") because the backend
 * serializes without a request context. Resolve against the API origin.
 */
export function mediaUrl(path: string | null | undefined): string | null {
  if (!path) return null;
  return path.startsWith("http") ? path : `${API_BASE}${path}`;
}

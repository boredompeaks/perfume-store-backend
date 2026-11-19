// task: T-20261119-006
//
// The single storefront CSP. `frontend/src/proxy.ts` is its only writer:
// next.config.mjs intentionally sets no CSP so a static headers() entry cannot
// drift from this per-request policy.
//
// The policy is report-only by default. Set CSP_ENFORCEMENT=enforce to enforce
// it on the interaction prefixes below; every other route stays report-only.
// Flipping the switch is not enough on its own: Next can only stamp inline
// scripts with the per-request nonce while a route renders per request, so each
// of these segment layouts must also export
// `export const dynamic = "force-dynamic"` before enforcement is safe:
// frontend/src/app/login/layout.tsx,
// frontend/src/app/register/layout.tsx,
// frontend/src/app/forgot-password/layout.tsx,
// frontend/src/app/forgot-username/layout.tsx,
// frontend/src/app/reset-password/layout.tsx,
// frontend/src/app/verify-email/layout.tsx,
// frontend/src/app/resend-verification/layout.tsx,
// frontend/src/app/checkout/layout.tsx,
// frontend/src/app/orders/layout.tsx.
// Static pages are served from the build cache with a stale (or absent) nonce,
// so enforcing on them would block their bootstrap scripts.

export const SENSITIVE_PREFIXES = [
  "login",
  "register",
  "forgot-password",
  "forgot-username",
  "reset-password",
  "verify-email",
  "resend-verification",
  "checkout",
  "orders",
] as const;

export type CspMode = "report" | "enforce";

export const CSP_ENFORCEMENT_ENV = "CSP_ENFORCEMENT";
export const CSP_MODE_DEFAULT: CspMode = "report";

// One switch for the whole storefront: only "enforce" arms the sensitive
// prefixes; every other value, including unset, is the report-only default.
export function cspMode(): CspMode {
  return process.env[CSP_ENFORCEMENT_ENV] === "enforce"
    ? "enforce"
    : CSP_MODE_DEFAULT;
}

function matchesPrefix(pathname: string, prefix: string): boolean {
  const path = pathname.split("?")[0].replace(/\/+$/, "") || "/";
  return path === `/${prefix}` || path.startsWith(`/${prefix}/`);
}

export function enforceFor(
  pathname: string,
  mode: CspMode = cspMode(),
): CspMode {
  if (mode !== "enforce") {
    return "report";
  }
  return SENSITIVE_PREFIXES.some((prefix) => matchesPrefix(pathname, prefix))
    ? "enforce"
    : "report";
}

export function cspHeaderName(mode: CspMode): string {
  return mode === "enforce"
    ? "Content-Security-Policy"
    : "Content-Security-Policy-Report-Only";
}

// Dev API on 8000, storefront on 3000 (3001 when taken): all loopback spellings
// of the same local stack. A configured origin replaces the set. Kept in sync
// with the image-optimizer list in next.config.mjs.
function apiOrigins(): string[] {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL;
  return configured
    ? [configured]
    : [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
      ];
}

// Sentry POSTs envelopes to the DSN's ingest origin and connect-src is an
// allowlist: a missing origin drops the report silently after beforeSend. It is
// derived from the DSN so regions can differ; an unset/malformed DSN adds
// nothing.
function sentryOrigin(): string | null {
  const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;
  try {
    return dsn ? new URL(dsn).origin : null;
  } catch {
    return null;
  }
}

export function buildCsp(nonce: string): string {
  const origins = apiOrigins();
  const sentry = sentryOrigin();
  return [
    "default-src 'self'",
    // Razorpay Checkout loads its script from checkout.razorpay.com; without it
    // the SPA reports "Could not load the payment gateway" (CSP-blocked).
    `script-src 'self' 'nonce-${nonce}' https://checkout.razorpay.com`,
    `style-src 'self' 'nonce-${nonce}'`,
    // React writes theme and carousel geometry as style attributes, which
    // cannot carry a nonce; scope the allowance to attributes, not elements.
    "style-src-attr 'unsafe-inline'",
    "font-src 'self'",
    `img-src 'self' ${origins.join(" ")} data: https://checkout.razorpay.com`,
    `connect-src 'self' ${origins.join(" ")} https://api.razorpay.com https://lumberjack.razorpay.com${
      sentry ? ` ${sentry}` : ""
    }`,
    "frame-src 'self' https://api.razorpay.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "upgrade-insecure-requests",
  ].join("; ");
}

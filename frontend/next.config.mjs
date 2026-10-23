import { initOpenNextCloudflareForDev } from "@opennextjs/cloudflare";
await initOpenNextCloudflareForDev();
// src/lib/config.ts addresses the API on the host the page was served from,
// so development has two valid origins (localhost and 127.0.0.1). A single
// hardcoded origin here would have the CSP block every request from the other
// spelling.
const configuredApiOrigin = process.env.NEXT_PUBLIC_API_BASE_URL;
// Development runs the API on 8000 and the storefront on 3000 (Playwright and
// `next dev`), with 3001 as the fallback port when 3000 is taken. They are all
// loopback spellings of the same local stack, so CSP and the image optimizer
// admit the whole set. A configured origin (staging/production) replaces it.
const devOrigins = [
  "http://localhost:8000",
  "http://127.0.0.1:8000",
  "http://localhost:3000",
  "http://127.0.0.1:3000",
  "http://localhost:3001",
];
const apiOrigins = configuredApiOrigin ? [configuredApiOrigin] : devOrigins;
// Razorpay Checkout loads from checkout.razorpay.com and talks to
// api.razorpay.com; the payment modal is a frame. Without these hosts the
// SPA reports "Could not load the payment gateway" (script blocked by CSP).
//
// Sentry's browser SDK POSTs envelopes to the DSN's ingest origin, and
// connect-src is an allowlist. Without the exact origin every report is
// blocked before it leaves the page, so the quota guard in src/lib/sentry.ts
// would never see an event and the error boundary would silently report
// nothing. The origin is derived from the configured DSN rather than
// hardcoded so staging and production can use different Sentry regions; an
// unset or malformed DSN adds nothing (the SDK is not initialised then).
const sentryDsn = process.env.NEXT_PUBLIC_SENTRY_DSN;
let sentryOrigin = null;
try {
  sentryOrigin = sentryDsn ? new URL(sentryDsn).origin : null;
} catch {
  sentryOrigin = null;
}
const scriptSrc = ["'self'", "'unsafe-inline'", "https://checkout.razorpay.com"];
const connectSrc = [
  "'self'",
  ...apiOrigins,
  "https://api.razorpay.com",
  "https://lumberjack.razorpay.com",
  ...(sentryOrigin ? [sentryOrigin] : []),
];
const frameSrc = ["'self'", "https://api.razorpay.com"];
const imgSrc = [
  "'self'",
  ...apiOrigins,
  "data:",
  "https://checkout.razorpay.com",
];
const csp = [
  "default-src 'self'",
  `script-src ${scriptSrc.join(" ")}`,
  "style-src 'self' 'unsafe-inline'",
  "font-src 'self'",
  `img-src ${imgSrc.join(" ")}`,
  `connect-src ${connectSrc.join(" ")}`,
  `frame-src ${frameSrc.join(" ")}`,
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "upgrade-insecure-requests",
].join("; ");
const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];
const nextConfig = {
  async headers() { return [{ source: "/:path*", headers: securityHeaders }]; },
  images: {
    remotePatterns: apiOrigins.map((origin) => {
      const { protocol, hostname, port } = new URL(origin);
      return {
        protocol: protocol.replace(":", ""),
        hostname,
        ...(port ? { port } : {}),
      };
    }),
    // §10.3 / U-W15-T17 (W9-T3 deferred F2): AVIF first, WebP fallback.
    // Without this key Next's default is ["image/webp"] only, so the
    // optimizer never offers AVIF even to a browser that asks for it.
    // No new dependency: next@16 declares sharp ^0.35.4 as its own
    // dependency (node_modules/sharp 0.35.5, libheif + aom, AVIF encode
    // verified in this tree), so the Accept-header negotiation costs 0
    // lines outside this file. The browser picks the best format per
    // request; every URL stays same-origin (/_next/image) under the
    // existing `img-src 'self'` CSP.
    formats: ["image/avif", "image/webp"],
  },
};
export default nextConfig;
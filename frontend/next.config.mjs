import { initOpenNextCloudflareForDev } from "@opennextjs/cloudflare";
await initOpenNextCloudflareForDev();
// The API follows the page host, so both loopback spellings are valid; a single
// hardcoded origin would let the CSP block requests from the other.
const configuredApiOrigin = process.env.NEXT_PUBLIC_API_BASE_URL;
// Dev API on 8000, storefront on 3000 (3001 when taken): all loopback spellings
// of the same local stack, admitted by CSP and the image optimizer. A configured
// origin replaces the set.
const devOrigins = [
  "http://localhost:8000",
  "http://127.0.0.1:8000",
  "http://localhost:3000",
  "http://127.0.0.1:3000",
  "http://localhost:3001",
];
const apiOrigins = configuredApiOrigin ? [configuredApiOrigin] : devOrigins;
// Razorpay Checkout loads its script and frames api.razorpay.com; without these
// hosts the SPA reports "Could not load the payment gateway" (CSP-blocked).
//
// Sentry POSTs envelopes to the DSN's ingest origin and connect-src is an
// allowlist: a missing origin drops the report silently after beforeSend. It is
// derived from the DSN so regions can differ; an unset/malformed DSN adds
// nothing.
const sentryDsn = process.env.NEXT_PUBLIC_SENTRY_DSN;
let sentryOrigin = null;
try {
  sentryOrigin = sentryDsn ? new URL(sentryDsn).origin : null;
} catch {
  sentryOrigin = null;
}
const scriptSrc = ["'self'", "https://checkout.razorpay.com"];
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
  "style-src 'self'",
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
// COEP is deliberately absent: require-corp would block Razorpay's
// cross-origin script/frame and Sentry's ingest, which send no CORP header.
// COOP allows the gateway's payment popups; CORP only stops other origins
// embedding storefront responses.
//
// The CSP is report-only: Next prerenders most routes at build time, so a
// per-request nonce cannot reach their inline scripts and an enforcing
// script-src would block them. Enforce this exact policy once every route is
// dynamically rendered and a per-request nonce is set in frontend/src/proxy.ts
// (the location Next 16 discovers while appDir is src/app).
const securityHeaders = [
  { key: "Content-Security-Policy-Report-Only", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin-allow-popups" },
  { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
];
const nextConfig = {
  poweredByHeader: false,
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
    // AVIF first, WebP fallback: Next defaults to WebP only, so the optimizer
    // never offers AVIF. No new dependency (next@16 ships sharp), and URLs stay
    // same-origin under the existing img-src 'self'.
    formats: ["image/avif", "image/webp"],
  },
};
export default nextConfig;
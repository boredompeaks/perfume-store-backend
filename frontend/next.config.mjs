import { initOpenNextCloudflareForDev } from "@opennextjs/cloudflare";
await initOpenNextCloudflareForDev();
// The API follows the page host, so both loopback spellings are valid; the
// image optimizer needs every origin a page can request from.
const configuredApiOrigin = process.env.NEXT_PUBLIC_API_BASE_URL;
// Dev API on 8000, storefront on 3000 (3001 when taken): all loopback spellings
// of the same local stack reachable by the image optimizer. A configured origin
// replaces the set. The CSP's copy of this list lives in src/lib/csp.ts so the
// policy has one owner.
const devOrigins = [
  "http://localhost:8000",
  "http://127.0.0.1:8000",
  "http://localhost:3000",
  "http://127.0.0.1:3000",
  "http://localhost:3001",
];
const apiOrigins = configuredApiOrigin ? [configuredApiOrigin] : devOrigins;
// COEP is deliberately absent: require-corp would block Razorpay's
// cross-origin script/frame and Sentry's ingest, which send no CORP header.
// COOP allows the gateway's payment popups; CORP only stops other origins
// embedding storefront responses.
// The CSP is owned by frontend/src/proxy.ts and frontend/src/lib/csp.ts: it
// needs a per-request nonce, which a static headers() entry cannot provide.
// Keeping any CSP here would create a second, conflicting writer.
const securityHeaders = [
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
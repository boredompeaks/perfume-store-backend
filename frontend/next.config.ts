import type { NextConfig } from "next";

const apiOrigin = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";
const { protocol, hostname, port } = new URL(apiOrigin);

// Content-Security-Policy [SPEC-17-06, R-17.17]. The app loads no third
// party scripts: all JS is Next's own (inline runtime/bootstrapper + the
// /_next/static chunks), fonts are self-hosted through next/font (the
// Google Fonts download happens at build time; only local .woff2 files
// are served), and images come from this origin plus the API's media
// host (next/image remotePatterns below). 'unsafe-inline' stays in
// script-src because Next's App Router injects inline bootstrapping
// scripts on every page that cannot be nonced without forking the
// framework's document rendering; everything else is locked to 'self'
// plus the API origin the app is actually configured to talk to.
const scriptSrc = ["'self'", "'unsafe-inline'"];
const connectSrc = ["'self'", apiOrigin];
const imgSrc = ["'self'", apiOrigin, "data:"];
// The API base URL is trusted config, so no scheme injection can reach
// this string, but keep the directive sane when it contains no port.
const csp = [
  "default-src 'self'",
  `script-src ${scriptSrc.join(" ")}`,
  "style-src 'self' 'unsafe-inline'",
  "font-src 'self'",
  `img-src ${imgSrc.join(" ")}`,
  `connect-src ${connectSrc.join(" ")}`,
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
  // Redundant with frame-ancestors 'none' above, but still honoured by
  // legacy browsers that never learned CSP frame-ancestors.
  { key: "X-Frame-Options", value: "DENY" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=()",
  },
];

const nextConfig: NextConfig = {
  async headers() {
    return [
      {
        source: "/:path*",
        headers: securityHeaders,
      },
    ];
  },
  images: {
    remotePatterns: [
      {
        protocol: protocol.replace(":", "") as "http" | "https",
        hostname,
        ...(port ? { port } : {}),
      },
    ],
  },
};

export default nextConfig;

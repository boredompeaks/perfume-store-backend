import { initOpenNextCloudflareForDev } from "@opennextjs/cloudflare";
await initOpenNextCloudflareForDev();
const apiOrigin = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";
const { protocol, hostname, port } = new URL(apiOrigin);
const csp = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "font-src 'self'",
  `img-src 'self' ${apiOrigin} data:`,
  `connect-src 'self' ${apiOrigin}`,
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
  images: { remotePatterns: [{ protocol: protocol.replace(":", ""), hostname, ...(port ? { port } : {}) }] },
};
export default nextConfig;
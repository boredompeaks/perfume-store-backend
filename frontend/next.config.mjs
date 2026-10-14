import { initOpenNextCloudflareForDev } from "@opennextjs/cloudflare";
await initOpenNextCloudflareForDev();
const apiOrigin = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";
const { protocol, hostname, port } = new URL(apiOrigin);
// Razorpay Checkout loads from checkout.razorpay.com and talks to
// api.razorpay.com; the payment modal is a frame. Without these hosts the
// SPA reports "Could not load the payment gateway" (script blocked by CSP).
const scriptSrc = ["'self'", "'unsafe-inline'", "https://checkout.razorpay.com"];
const connectSrc = [
  "'self'",
  apiOrigin,
  "https://api.razorpay.com",
  "https://lumberjack.razorpay.com",
];
const frameSrc = ["'self'", "https://api.razorpay.com"];
const imgSrc = ["'self'", apiOrigin, "data:", "https://checkout.razorpay.com"];
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
  images: { remotePatterns: [{ protocol: protocol.replace(":", ""), hostname, ...(port ? { port } : {}) }] },
};
export default nextConfig;
import { redirect } from "next/navigation";

// Module scope so `next build` fails when the origin is unset; no localhost
// fallback, which would send visitors to 127.0.0.1.
const rawOrigin = process.env.NEXT_PUBLIC_ADMIN_ORIGIN?.trim();
if (!rawOrigin) {
  throw new Error(
    "NEXT_PUBLIC_ADMIN_ORIGIN is required — `next build` fails when it is unset. " +
      "Set the absolute origin of the Django admin in .env.local (see .env.local.example).",
  );
}

let adminUrl: string;
try {
  adminUrl = new URL("/admin/", rawOrigin).href;
} catch {
  throw new Error(
    `NEXT_PUBLIC_ADMIN_ORIGIN must be an absolute origin (https://host), got: ${rawOrigin}`,
  );
}

export const metadata = {
  robots: { index: false, follow: false },
};

export default function AdminRedirectPage() {
  // 307, never 308: the env-driven origin can move between deploys, so a
  // cached permanent redirect must not pin browsers to a stale admin host.
  redirect(adminUrl);
}

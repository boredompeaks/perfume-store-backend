import { redirect } from "next/navigation";

/**
 * /admin is a redirect NON-surface (U-W5-T1 / BE-010): the Django admin is
 * served by the API origin (backend `config/urls.py` mounts `admin.site.urls`
 * at `admin/`, settings installs `config.admin.MFAAdminConfig`), so the
 * storefront carries no chrome here — only the hop.
 *
 * Fail-if-unset mirrors BE-007's stated site-url rule ("prod build FAILS if
 * unset"); no such check exists in-tree, so this is the smallest equivalent —
 * the one module that requires the value, checked at module scope. `next build`
 * evaluates it while collecting page data for /admin, so an unset var fails the
 * build. NO localhost default: a missing origin must break loudly, never send a
 * visitor to 127.0.0.1.
 */
const rawOrigin = process.env.NEXT_PUBLIC_ADMIN_ORIGIN?.trim();
if (!rawOrigin) {
  throw new Error(
    "NEXT_PUBLIC_ADMIN_ORIGIN is required — `next build` fails when it is unset. " +
      "Set the absolute origin of the Django admin in .env.local (see .env.local.example).",
  );
}

let adminUrl: string;
try {
  // The backend mounts the admin at /admin/ (config/urls.py); a seeded value
  // that already carries that path normalizes to the same URL.
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
  // Plain redirect() = 307 Temporary Redirect (never permanentRedirect/308):
  // the origin is env-driven and can move between deploys, so a cached
  // permanent redirect must never pin browsers to a stale admin host.
  redirect(adminUrl);
}

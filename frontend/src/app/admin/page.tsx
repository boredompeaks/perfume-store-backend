import { redirect } from "next/navigation";

// The admin is Django's, served on the API origin — the storefront has no
// /admin of its own, so send the visit there instead of a confusing 404.
const ADMIN_URL =
  process.env.ADMIN_URL ?? "http://127.0.0.1:8000/admin/";

export const metadata = {
  robots: { index: false, follow: false },
};

export default function AdminRedirectPage() {
  redirect(ADMIN_URL);
}

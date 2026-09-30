import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import MfaEnrollForm from "@/components/auth/MfaEnrollForm";

/**
 * SPEC-20-13: the enrollment surface the admin login page links to when
 * mandatory MFA (R-17.9) refuses an unenrolled privileged account. It posts
 * to the same /api/accounts/mfa/setup/ + /mfa/confirm/ endpoints the
 * bootstrap window already allowed, so no new API surface is involved.
 */
export const metadata: Metadata = {
  title: "Set up multi-factor authentication",
  robots: { index: false, follow: false },
};

export default function MfaEnrollPage() {
  return (
    <AuthShell title="Set up MFA">
      <MfaEnrollForm />
    </AuthShell>
  );
}
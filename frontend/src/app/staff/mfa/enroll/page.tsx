import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import MfaEnrollForm from "@/components/auth/MfaEnrollForm";

// Enrollment surface for mandatory MFA; reuses the existing /mfa/setup/ and
// /mfa/confirm/ endpoints, so no new API surface is involved.
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
import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import EmailOnlyForm from "@/components/auth/EmailOnlyForm";

export const metadata: Metadata = {
  title: "Resend verification email",
  robots: { index: false, follow: false },
};

export default function ResendVerificationPage() {
  return (
    <AuthShell title="Resend verification email">
      <EmailOnlyForm
        path="/api/accounts/resend-verification/"
        submitLabel="Send email"
        pendingLabel="Sending…"
      />
    </AuthShell>
  );
}

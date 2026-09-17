import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import EmailOnlyForm from "@/components/auth/EmailOnlyForm";

export const metadata: Metadata = {
  title: "Reset your password",
  robots: { index: false, follow: false },
};

export default function ForgotPasswordPage() {
  return (
    <AuthShell title="Reset your password">
      <EmailOnlyForm
        path="/api/accounts/password-reset/"
        submitLabel="Send reset link"
        pendingLabel="Sending…"
      />
    </AuthShell>
  );
}

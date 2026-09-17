import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import EmailOnlyForm from "@/components/auth/EmailOnlyForm";

export const metadata: Metadata = {
  title: "Forgot username",
  robots: { index: false, follow: false },
};

export default function ForgotUsernamePage() {
  return (
    <AuthShell title="Forgot username">
      <EmailOnlyForm
        path="/api/accounts/forgot-username/"
        submitLabel="Email me my username"
        pendingLabel="Sending…"
      />
    </AuthShell>
  );
}

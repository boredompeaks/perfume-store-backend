import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import GuestOnly from "@/components/auth/GuestOnly";
import LoginForm from "@/components/auth/LoginForm";

/**
 * The privileged sign-in surface (SPEC-20-13). It exists so an unenrolled
 * staff account has somewhere to finish setup: after the enrollment page
 * activates a device the user continues here, where the authentication-code
 * field is shown by default instead of being hidden behind a "(staff with MFA
 * only)" hint nobody can evaluate before signing in.
 */
export const metadata: Metadata = {
  title: "Staff sign in",
  robots: { index: false, follow: false },
};

export default function StaffLoginPage() {
  return (
    <AuthShell title="Staff sign in">
      <GuestOnly>
        <LoginForm variant="staff" />
      </GuestOnly>
    </AuthShell>
  );
}
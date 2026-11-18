import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import GuestOnly from "@/components/auth/GuestOnly";
import LoginForm from "@/components/auth/LoginForm";

// Unenrolled staff finish setup here; the authentication-code field shows by
// default instead of hiding behind a post-sign-in hint.
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
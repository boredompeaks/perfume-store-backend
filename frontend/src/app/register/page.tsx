import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import GuestOnly from "@/components/auth/GuestOnly";
import RegisterForm from "@/components/auth/RegisterForm";

export const metadata: Metadata = {
  title: "Create account",
  robots: { index: false, follow: false },
};

export default function RegisterPage() {
  return (
    <AuthShell title="Create account">
      <GuestOnly>
        <RegisterForm />
      </GuestOnly>
    </AuthShell>
  );
}

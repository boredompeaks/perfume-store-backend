import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import GuestOnly from "@/components/auth/GuestOnly";
import RegisterForm from "@/components/auth/RegisterForm";
import { safeNext } from "@/lib/redirect";

export const metadata: Metadata = {
  title: "Create account",
  robots: { index: false, follow: false },
};

type SearchParams = Record<string, string | string[] | undefined>;

// /checkout funnels anonymous visitors here with ?next=; safeNext keeps it an
// internal path and GuestOnly skips the form for signed-in visitors.
export default async function RegisterPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const next = safeNext(Array.isArray(sp.next) ? sp.next[0] : sp.next);

  return (
    <AuthShell title="Create account">
      <GuestOnly next={next}>
        <RegisterForm />
      </GuestOnly>
    </AuthShell>
  );
}

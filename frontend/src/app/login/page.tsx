import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import GuestOnly from "@/components/auth/GuestOnly";
import LoginForm from "@/components/auth/LoginForm";
import { safeNext } from "@/lib/redirect";

export const metadata: Metadata = {
  title: "Sign in",
  robots: { index: false, follow: false },
};

type SearchParams = Record<string, string | string[] | undefined>;

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const next = safeNext(Array.isArray(sp.next) ? sp.next[0] : sp.next);

  return (
    <AuthShell title="Sign in">
      <GuestOnly next={next}>
        <LoginForm next={next} />
      </GuestOnly>
    </AuthShell>
  );
}

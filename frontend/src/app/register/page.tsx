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

/**
 * D2 (U-W10-T2): /checkout funnels anonymous visitors here with
 * `?next=/checkout`, so this door reads the param exactly the way /login
 * does — `safeNext` keeps it an internal path, `GuestOnly` hands it to a
 * visitor who is already signed in (router.replace, no history entry), and
 * the param stays in the URL for the rest of this page's navigation. The
 * account-created state itself is the server's (email verification comes
 * first); the leg that completes after sign-in is the login door's.
 */
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

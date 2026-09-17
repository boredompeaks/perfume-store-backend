import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import VerifyEmailClient from "@/components/auth/VerifyEmailClient";

export const metadata: Metadata = {
  title: "Verify email",
  robots: { index: false, follow: false },
};

type SearchParams = Record<string, string | string[] | undefined>;

export default async function VerifyEmailPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const uid = Array.isArray(sp.uid) ? sp.uid[0] : sp.uid;
  const token = Array.isArray(sp.token) ? sp.token[0] : sp.token;

  return (
    <AuthShell title="Verify email">
      <VerifyEmailClient uid={uid} token={token} />
    </AuthShell>
  );
}

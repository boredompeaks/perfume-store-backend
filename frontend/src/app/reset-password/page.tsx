import type { Metadata } from "next";
import AuthShell from "@/components/auth/AuthShell";
import ResetPasswordClient from "@/components/auth/ResetPasswordClient";

export const metadata: Metadata = {
  title: "Reset password",
  robots: { index: false, follow: false },
};

type SearchParams = Record<string, string | string[] | undefined>;

export default async function ResetPasswordPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const uid = Array.isArray(sp.uid) ? sp.uid[0] : sp.uid;
  const token = Array.isArray(sp.token) ? sp.token[0] : sp.token;

  return (
    <AuthShell title="Reset password">
      <ResetPasswordClient uid={uid} token={token} />
    </AuthShell>
  );
}

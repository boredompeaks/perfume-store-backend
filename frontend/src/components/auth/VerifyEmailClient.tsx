"use client";

import { useEffect, useState } from "react";
import { verifyEmail } from "@/lib/accounts-api";

type State =
  | { kind: "working" }
  | { kind: "verified" }
  | { kind: "invalid" };

export default function VerifyEmailClient({
  uid,
  token,
}: {
  uid?: string;
  token?: string;
}) {
  const [state, setState] = useState<State>({ kind: "working" });

  useEffect(() => {
    if (!uid || !token) {
      setState({ kind: "invalid" });
      return;
    }
    let cancelled = false;
    verifyEmail(uid, token)
      .then(() => {
        if (!cancelled) setState({ kind: "verified" });
      })
      .catch(() => {
        if (!cancelled) setState({ kind: "invalid" });
      });
    return () => {
      cancelled = true;
    };
  }, [uid, token]);

  if (state.kind === "working") {
    return (
      <p className="text-ink-muted" role="status" aria-live="polite">
        Verifying your email…
      </p>
    );
  }

  if (state.kind === "verified") {
    return (
      <div className="border border-line p-8">
        <h2 className="font-display text-2xl tracking-tight">
          Email verified.
        </h2>
        <p className="mt-3 text-ink-muted">
          Your account is active. You can sign in now.
        </p>
        <a
          href="/login"
          className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Go to sign in
        </a>
      </div>
    );
  }

  return (
    <div className="border border-line p-8">
      <h2 className="font-display text-2xl tracking-tight">
        This link is invalid or expired.
      </h2>
      <p className="mt-3 text-ink-muted">
        Verification links are one-time. Request a fresh email and open the
        newest link.
      </p>
      <a
        href="/resend-verification"
        className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
      >
        Request a new email
      </a>
    </div>
  );
}

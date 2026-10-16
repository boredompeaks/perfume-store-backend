"use client";

import { useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { verifyEmail } from "@/lib/accounts-api";

type State =
  | { kind: "working" }
  /** Query carried no uid/token: the link was truncated before it reached us. */
  | { kind: "incomplete" }
  /** Server accepted — message renders verbatim. */
  | { kind: "verified"; message: string }
  /** Server refused (uniform 400) — message renders verbatim, + resend link. */
  | { kind: "rejected"; message: string }
  /** Network failure — says nothing about the link's validity. */
  | { kind: "unreachable" };

/**
 * §5.10 `/verify-email`. Posts uid + token on mount. The server's answer —
 * success or the uniform invalid/expired 400 — renders verbatim; this UI
 * never rewrites it. A network failure is its own state: it must not claim
 * the link is invalid when we never reached the server.
 */
export default function VerifyEmailClient({
  uid,
  token,
}: {
  uid?: string;
  token?: string;
}) {
  const [state, setState] = useState<State>(
    uid && token ? { kind: "working" } : { kind: "incomplete" },
  );

  useEffect(() => {
    if (!uid || !token) return;
    let cancelled = false;
    verifyEmail(uid, token)
      .then((res) => {
        if (!cancelled) setState({ kind: "verified", message: res.message });
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError) {
          // Invalid/expired link (and any other server refusal): the
          // server's own words, verbatim.
          setState({ kind: "rejected", message: err.message });
        } else {
          setState({ kind: "unreachable" });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [uid, token]);

  if (state.kind === "working") {
    return (
      <p className="text-ink-secondary" role="status" aria-live="polite">
        Verifying your email…
      </p>
    );
  }

  if (state.kind === "verified") {
    return (
      <div className="border border-hairline p-8">
        {/* Server copy, verbatim. */}
        <p role="status" aria-live="polite" className="font-display text-2xl tracking-tight">
          {state.message}
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

  if (state.kind === "rejected") {
    return (
      <div className="border border-hairline p-8">
        {/* Server copy, verbatim — the uniform 400, never rewritten. */}
        <p role="alert" className="font-display text-2xl tracking-tight">
          {state.message}
        </p>
        <p className="mt-3 text-ink-secondary">
          Request a fresh email and open the newest link.
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

  if (state.kind === "unreachable") {
    return (
      <div className="border border-hairline p-8">
        <h2 className="font-display text-2xl tracking-tight">
          We couldn&apos;t reach the server.
        </h2>
        <p className="mt-3 text-ink-secondary">
          Your link may still be good. Check your connection and try opening
          it again.
        </p>
      </div>
    );
  }

  return (
    <div className="border border-hairline p-8">
      <h2 className="font-display text-2xl tracking-tight">
        This verification link is incomplete.
      </h2>
      <p className="mt-3 text-ink-secondary">
        Open the link from your most recent verification email — it carries
        the pieces this page needs.
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

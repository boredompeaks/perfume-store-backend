"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
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

  /**
   * Screen v_rification_de_courriel (refined, PRIMARY): every state renders
   * inside the register panel with a status line over a hairline. The status
   * names only what this client knows (checked / not checked / confirmed /
   * unconfirmed); the server's own words carry the reason, verbatim below it,
   * so the chrome never characterises the failure (no expiry or invalidity
   * claim beyond what the server said).
   */
  let statusLabel: string;
  let statusTone: string;
  let body: ReactNode;

  if (state.kind === "working") {
    statusLabel = "Status: checking";
    statusTone = "text-ink-secondary";
    body = (
      <p role="status" aria-live="polite" className="mt-4 text-ink-secondary">
        Verifying your email…
      </p>
    );
  } else if (state.kind === "verified") {
    statusLabel = "Status: confirmed";
    statusTone = "text-bronze";
    body = (
      <div>
        {/* Server copy, verbatim. */}
        <p role="status" aria-live="polite" className="mt-4 text-ink">
          {state.message}
        </p>
        <a
          href="/login"
          className="mt-6 block w-full bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
        >
          Go to sign in
        </a>
      </div>
    );
  } else if (state.kind === "rejected") {
    statusLabel = "Status: unconfirmed";
    statusTone = "text-danger";
    body = (
      <div>
        {/* Server copy, verbatim — the uniform 400, never rewritten. */}
        <p role="alert" className="mt-4 font-display text-2xl tracking-tight">
          {state.message}
        </p>
        <p className="mt-3 text-ink-secondary">
          Request a fresh email and open the newest link.
        </p>
        <a
          href="/resend-verification"
          className="mt-6 block w-full bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
        >
          Request a new email
        </a>
      </div>
    );
  } else if (state.kind === "unreachable") {
    statusLabel = "Status: not checked";
    statusTone = "text-ink-secondary";
    body = (
      <div>
        <h2 className="mt-4 font-display text-2xl tracking-tight">
          We couldn&apos;t reach the server.
        </h2>
        <p className="mt-3 text-ink-secondary">
          Your link may still be good. Check your connection and try opening
          it again.
        </p>
      </div>
    );
  } else {
    statusLabel = "Status: link incomplete";
    statusTone = "text-ink-secondary";
    body = (
      <div>
        <h2 className="mt-4 font-display text-2xl tracking-tight">
          This verification link is incomplete.
        </h2>
        <p className="mt-3 text-ink-secondary">
          Open the link from your most recent verification email. It carries
          the pieces this page needs.
        </p>
        <a
          href="/resend-verification"
          className="mt-6 block w-full bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
        >
          Request a new email
        </a>
      </div>
    );
  }

  return (
    <div>
      {/* Refined screen: a short centred rule, then the protocol micro-line,
          then the register panel. */}
      <div className="flex flex-col items-center gap-3">
        <span aria-hidden className="h-px w-10 bg-hairline" />
        <p className="text-eyebrow uppercase text-ink-secondary">
          Account verification
        </p>
      </div>
      <div className="mt-8 border border-hairline bg-surface p-8">
        <p className={`border-b border-hairline pb-3 text-eyebrow uppercase ${statusTone}`}>
          {statusLabel}
        </p>
        {body}
      </div>
      {/* Refined screen footer: hairline rule, then the underlined pair with a
          dot separator. */}
      <div className="mt-8 flex items-center justify-center gap-3 border-t border-hairline pt-6 text-sm text-ink-secondary">
        <Link
          href="/login"
          className="underline underline-offset-4 transition-colors hover:text-bronze"
        >
          Return to sign in
        </Link>
        <span aria-hidden className="text-hairline">
          •
        </span>
        <Link
          href="/contact"
          className="underline underline-offset-4 transition-colors hover:text-bronze"
        >
          Contact support
        </Link>
      </div>
    </div>
  );
}

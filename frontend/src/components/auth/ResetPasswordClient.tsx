"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { resetPassword } from "@/lib/accounts-api";
import { inputClass } from "@/lib/ui";

type State =
  /** uid + token present — the form. */
  | { kind: "form" }
  /** Query carried no uid/token: the link was truncated before it reached us. */
  | { kind: "incomplete" }
  /** Server answered — message renders verbatim (invalid/expired link). */
  | { kind: "rejected"; message: string }
  /** Server accepted — message renders verbatim. */
  | { kind: "done"; message: string };

/**
 * §5.10 `/reset-password`. The token arrives in the query; the server is the
 * only judge of whether it is valid. Failures (invalid/expired token) render
 * the server's message verbatim — this UI never rewrites it and never adds
 * copy that branches on account existence. `validate_password` errors arrive
 * as `{password: [msgs]}` and render per message under the password field
 * (AddressForm is the reference implementation for field-error wiring).
 */
export default function ResetPasswordClient({
  uid,
  token,
}: {
  uid?: string;
  token?: string;
}) {
  const [state, setState] = useState<State>(
    uid && token ? { kind: "form" } : { kind: "incomplete" },
  );
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [fieldErrors, setFieldErrors] = useState<string[]>([]);
  const [mismatch, setMismatch] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setFieldErrors([]);
    setMismatch(false);
    if (password !== confirm) {
      setMismatch(true);
      return;
    }
    setPending(true);
    try {
      const res = await resetPassword(uid as string, token as string, password);
      setState({ kind: "done", message: res.message });
    } catch (err) {
      if (err instanceof ApiError && err.fieldErrors?.password) {
        // Server-side password policy failures — one <p> per message.
        setFieldErrors(err.fieldErrors.password);
      } else if (err instanceof ApiError) {
        // Invalid/expired link (and any other server refusal): the server's
        // own words, verbatim.
        setState({ kind: "rejected", message: err.message });
      } else {
        // Network failure — the link may be perfectly good. Say nothing
        // about the link; ask for a retry.
        setFormError("We couldn't reach the server. Check your connection and try again.");
      }
    } finally {
      setPending(false);
    }
  };

  let body: ReactNode;

  if (state.kind === "incomplete") {
    body = (
      // Screen nouveau_mot_de_passe state panel: white plate on paper, message
      // line, full-width ink CTA inside the panel.
      <div className="border border-hairline bg-surface p-8">
        <h2 className="font-display text-2xl tracking-tight">
          This reset link is incomplete.
        </h2>
        <p className="mt-3 text-ink-secondary">
          Open the link from your most recent reset email. It carries the
          pieces this page needs. If it still fails, request a fresh one.
        </p>
        <a
          href="/forgot-password"
          className="mt-6 block w-full bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
        >
          Request a new link
        </a>
      </div>
    );
  } else if (state.kind === "rejected") {
    body = (
      <div className="border border-hairline bg-surface p-8">
        {/* Server copy, verbatim — never rewritten client-side. */}
        <p role="alert" className="font-display text-2xl tracking-tight">
          {state.message}
        </p>
        <p className="mt-3 text-ink-secondary">
          Request a fresh email and open the newest link.
        </p>
        <a
          href="/forgot-password"
          className="mt-6 block w-full bg-ink px-6 py-3 text-center text-sm text-paper transition-colors hover:bg-bronze"
        >
          Request a new link
        </a>
      </div>
    );
  } else if (state.kind === "done") {
    body = (
      <div className="border border-hairline bg-surface p-8">
        {/* Server copy, verbatim. */}
        <p role="status" aria-live="polite" className="font-display text-2xl tracking-tight">
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
  } else {
    body = (
      <div>
        {/* Screen nouveau_mot_de_passe: one line of subcopy under the heading,
            then labels-outside fields and the full-width ink CTA. */}
        <p className="text-ink-secondary">
          Choose a new password for your account.
        </p>
        <form onSubmit={onSubmit} className="mt-6 space-y-4" noValidate>
          <div>
            <label htmlFor="reset-password" className="mb-1 block text-sm">
              New password
            </label>
            <input
              id="reset-password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className={`${inputClass} w-full`}
              required
              minLength={8}
              aria-invalid={fieldErrors.length > 0 ? true : undefined}
              aria-describedby={fieldErrors.length > 0 ? "reset-password-err" : undefined}
            />
            {fieldErrors.length > 0 && (
              <div id="reset-password-err">
                {fieldErrors.map((msg) => (
                  <p key={msg} role="alert" className="mt-1 text-sm text-danger">
                    {msg}
                  </p>
                ))}
              </div>
            )}
          </div>
          <div>
            <label htmlFor="reset-confirm" className="mb-1 block text-sm">
              Confirm new password
            </label>
            <input
              id="reset-confirm"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              className={`${inputClass} w-full`}
              required
              aria-invalid={mismatch ? true : undefined}
              aria-describedby={mismatch ? "reset-confirm-err" : undefined}
            />
            {mismatch && (
              <p id="reset-confirm-err" role="alert" className="mt-1 text-sm text-danger">
                Passwords don&apos;t match.
              </p>
            )}
          </div>
          {formError && (
            <p role="alert" className="text-sm text-danger">
              {formError}
            </p>
          )}
          <button
            type="submit"
            disabled={pending}
            className="w-full bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-60"
          >
            {pending ? "Updating…" : "Set new password"}
          </button>
        </form>
      </div>
    );
  }

  return (
    <div>
      {body}
      {/* Both screens close the column the same way: hairline rule, then the
          underlined link stack. */}
      <div className="mt-8 border-t border-hairline pt-6 text-center text-sm text-ink-secondary">
        <p>
          <Link
            href="/login"
            className="underline underline-offset-4 transition-colors hover:text-bronze"
          >
            Return to sign in
          </Link>
        </p>
        <p className="mt-2">
          <Link
            href="/contact"
            className="underline underline-offset-4 transition-colors hover:text-bronze"
          >
            Contact support
          </Link>
        </p>
      </div>
    </div>
  );
}

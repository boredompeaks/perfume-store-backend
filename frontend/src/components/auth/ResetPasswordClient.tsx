"use client";

import { useState } from "react";
import { ApiError } from "@/lib/api";
import { resetPassword } from "@/lib/accounts-api";
import { inputClass } from "@/lib/ui";

type State =
  | { kind: "form" }
  | { kind: "done" }
  | { kind: "invalid" };

export default function ResetPasswordClient({
  uid,
  token,
}: {
  uid?: string;
  token?: string;
}) {
  const [state, setState] = useState<State>(
    uid && token ? { kind: "form" } : { kind: "invalid" },
  );
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [fieldErrors, setFieldErrors] = useState<string[]>([]);
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setFieldErrors([]);
    if (password !== confirm) {
      setFormError("Passwords don't match.");
      return;
    }
    setPending(true);
    try {
      await resetPassword(uid as string, token as string, password);
      setState({ kind: "done" });
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.fieldErrors?.password) {
          setFieldErrors(err.fieldErrors.password);
        } else {
          // Invalid or expired link.
          setState({ kind: "invalid" });
        }
      } else {
        setFormError("Something went wrong. Try again.");
      }
    } finally {
      setPending(false);
    }
  };

  if (state.kind === "invalid") {
    return (
      <div className="border border-line p-8">
        <h2 className="font-display text-2xl tracking-tight">
          This reset link is invalid or expired.
        </h2>
        <p className="mt-3 text-ink-muted">
          Reset links are one-time. Request a fresh email and open the newest
          link.
        </p>
        <a
          href="/forgot-password"
          className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Request a new link
        </a>
      </div>
    );
  }

  if (state.kind === "done") {
    return (
      <div className="border border-line p-8">
        <h2 className="font-display text-2xl tracking-tight">
          Password updated.
        </h2>
        <p className="mt-3 text-ink-muted">
          You can sign in with your new password now.
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
    <form onSubmit={onSubmit} className="space-y-4" noValidate>
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
        />
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
        />
        {fieldErrors.map((msg) => (
          <p key={msg} role="alert" className="mt-1 text-sm text-bronze">
            {msg}
          </p>
        ))}
        {formError && (
          <p role="alert" className="mt-1 text-sm text-bronze">
            {formError}
          </p>
        )}
      </div>
      <button
        type="submit"
        disabled={pending}
        className="w-full bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-60"
      >
        {pending ? "Updating…" : "Set new password"}
      </button>
    </form>
  );
}

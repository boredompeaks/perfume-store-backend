"use client";

import { useState } from "react";
import { apiFetch, ApiError } from "@/lib/api";

const inputClass =
  "w-full border border-line bg-surface px-3 py-2 text-sm placeholder:text-ink-muted/70 focus:outline-none";

type State =
  | { kind: "idle" }
  | { kind: "sent"; message: string }
  | { kind: "error"; message: string };

/**
 * Shared form for the enumeration-safe email flows (resend verification,
 * forgot username, request password reset). The backend answers uniformly
 * regardless of account existence — the message is shown verbatim, and the
 * UI never branches on it.
 */
export default function EmailOnlyForm({
  path,
  submitLabel,
  pendingLabel,
}: {
  path: string;
  submitLabel: string;
  pendingLabel: string;
}) {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<State>({ kind: "idle" });
  const [pending, setPending] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setPending(true);
    try {
      const res = await apiFetch<{ message?: string }>(path, {
        method: "POST",
        body: { email: email.trim() },
      });
      setState({ kind: "sent", message: res.message ?? "Done. Check your inbox." });
    } catch (err) {
      setState({
        kind: "error",
        message:
          err instanceof ApiError
            ? err.message
            : "Network error. Try again.",
      });
    } finally {
      setPending(false);
    }
  };

  if (state.kind === "sent") {
    return (
      <div className="border border-line p-8">
        <p className="text-ink-muted" role="status" aria-live="polite">
          {state.message}
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4" noValidate>
      <div>
        <label htmlFor="flow-email" className="mb-1 block text-sm">
          Email
        </label>
        <input
          id="flow-email"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className={inputClass}
          required
        />
      </div>
      {state.kind === "error" && (
        <p role="alert" className="text-sm text-bronze">
          {state.message}
        </p>
      )}
      <button
        type="submit"
        disabled={pending || !email.trim()}
        className="w-full bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-60"
      >
        {pending ? pendingLabel : submitLabel}
      </button>
    </form>
  );
}

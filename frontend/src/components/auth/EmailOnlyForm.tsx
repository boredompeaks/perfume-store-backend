"use client";

import Link from "next/link";
import { useState } from "react";
import { apiFetch, ApiError } from "@/lib/api";
import { inputClass } from "@/lib/ui";

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

  return (
    <div>
      {state.kind === "sent" ? (
        // §5.10: the success state is a field-border panel, so the swap from
        // form to message is a visible container change rather than vanished
        // fields. The server's message renders verbatim — the backend answers
        // uniformly whether or not the account exists, and the UI never
        // branches on it (uniform-response rule, conventions.md Security).
        <div className="border border-field-border p-8">
          <p className="text-ink" role="status" aria-live="polite">
            {state.message}
          </p>
        </div>
      ) : (
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
              className={`${inputClass} w-full`}
              required
            />
          </div>
          {state.kind === "error" && (
            <p role="alert" className="text-sm text-danger">
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
      )}

      {/* Screen r_cup_ration_de_compte: hairline rule under the CTA, then the
          underlined link stack. This block renders under both the form and the
          sent panel, so the way onward never disappears with the fields. */}
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

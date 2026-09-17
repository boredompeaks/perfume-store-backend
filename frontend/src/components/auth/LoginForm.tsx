"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const inputClass =
  "w-full border border-line bg-surface px-3 py-2 text-sm placeholder:text-ink-muted/70 focus:outline-none";

export default function LoginForm({ next }: { next?: string }) {
  const router = useRouter();
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setPending(true);
    setError(null);
    try {
      await login(username.trim(), password);
      // Cart is session-based and survives login; go where the user was headed.
      router.push(next ?? "/");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? "We couldn't sign you in with those details."
          : "Sign-in failed. Check your connection and try again.",
      );
    } finally {
      setPending(false);
    }
  };

  return (
    <div>
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <div>
          <label htmlFor="login-username" className="mb-1 block text-sm">
            Username
          </label>
          <input
            id="login-username"
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className={inputClass}
            required
          />
        </div>
        <div>
          <label htmlFor="login-password" className="mb-1 block text-sm">
            Password
          </label>
          <input
            id="login-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={inputClass}
            required
          />
        </div>

        {error && (
          <p role="alert" className="text-sm text-bronze" aria-live="polite">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={pending}
          className="w-full bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-60"
        >
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>

      {/*
        These links are ALWAYS rendered, never conditioned on the error.
        The backend's 401 is deliberately ambiguous (wrong credentials vs
        unverified email — enumeration-safe by design); branching the UI on
        the failure reason would leak that signal back.
      */}
      <div className="mt-6 space-y-2 border-t border-line pt-6 text-sm text-ink-muted">
        <p>
          New here?{" "}
          <Link href="/register" className="underline underline-offset-4 transition-colors hover:text-bronze">
            Create an account
          </Link>
        </p>
        <p>
          Waiting for a verification email?{" "}
          <Link href="/resend-verification" className="underline underline-offset-4 transition-colors hover:text-bronze">
            Resend it
          </Link>
        </p>
        <p className="flex gap-4">
          <Link href="/forgot-username" className="underline underline-offset-4 transition-colors hover:text-bronze">
            Forgot username?
          </Link>
          <Link href="/forgot-password" className="underline underline-offset-4 transition-colors hover:text-bronze">
            Forgot password?
          </Link>
        </p>
      </div>
    </div>
  );
}

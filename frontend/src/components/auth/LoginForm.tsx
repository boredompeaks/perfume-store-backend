"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { inputClass } from "@/lib/ui";

export default function LoginForm({
  next,
  variant = "customer",
}: {
  next?: string;
  /**
   * Which sign-in door this is. SPEC-20-10: the storefront ("customer") door
   * renders NO authentication-code field at all — customers never
   * authenticate with one, and offering it there was unknowable noise for
   * the overwhelming majority of sign-ins. The "staff" door renders it,
   * plainly, because that is where [R-17.9] demands it.
   */
  variant?: "customer" | "staff";
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setPending(true);
    setError(null);
    try {
      // The surface picks both the endpoint and whether a code may ride
      // along (lib/mfa.ts): the customer body can never grow a totp key.
      await login(variant, username.trim(), password, totp.trim() || undefined);
      // D-04b: the backend merged the guest basket into the account cart at
      // sign-in, so the held cart query is stale — refetch it before the
      // destination renders the header badge from it.
      await queryClient.invalidateQueries({ queryKey: ["cart"] });
      // Go where the user was headed. Awaiting keeps "Signing in…" visible
      // until the destination is rendering (the backend's PBKDF2 check costs
      // ~0.7s — honest feedback beats a dead-looking button).
      await router.push(next ?? "/");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? // The staff door reports MFA problems under `totp`; the customer
            // door has no such field, so its answers (a uniform 401, or the
            // "this is a staff account" refusal) arrive as the envelope's
            // own text.
            variant === "staff"
            ? (err.fieldErrors?.totp?.[0] ?? err.message)
            : err.message
          : "Sign-in failed. Check your connection and try again.",
      );
    } finally {
      setPending(false);
    }
  };

  return (
    <div>
      <form onSubmit={onSubmit} className="space-y-5" noValidate>
        <div>
          {/* W13-T10 (design parity): labels are the screen's uppercase
              small treatment — `--text-label-sm` (11px, 0.08em) in
              `--color-ink-secondary`, outside the field, never bronze (one
              accent moment per page: the shell's eyebrow). */}
          <label
            htmlFor="login-username"
            className="mb-2 block text-label-sm uppercase text-ink-secondary"
          >
            Username
          </label>
          <input
            id="login-username"
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className={`${inputClass} w-full`}
            required
          />
        </div>
        <div>
          <label
            htmlFor="login-password"
            className="mb-2 block text-label-sm uppercase text-ink-secondary"
          >
            Password
          </label>
          <input
            id="login-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={`${inputClass} w-full`}
            required
          />
        </div>
        {variant === "staff" && (
          <div>
            {/* SPEC-17-05 (R-17.9): the second factor the backend demands
                for privileged accounts on this door. It does not exist on
                the storefront door — see SPEC-20-10. */}
            <label
              htmlFor="login-totp"
              className="mb-2 block text-label-sm uppercase text-ink-secondary"
            >
              Authentication code
            </label>
            <input
              id="login-totp"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={totp}
              onChange={(e) => setTotp(e.target.value)}
              className={`${inputClass} w-full`}
              required
            />
          </div>
        )}

        {/* CHANGE-2: validation and failure copy is `--color-danger`, never
            the accent — bronze must never mean "this failed". The message
            itself is the server's, verbatim (401 ambiguity, 429 throttle
            wording); the non-ApiError branch is the network-down state. */}
        {error && (
          <p role="alert" className="text-sm text-danger" aria-live="polite">
            {error}
          </p>
        )}

        {/* The screen's full-width ink button: `--text-label` uppercase on
            the AddressForm CTA construction (§5.9 harmonisation). */}
        <button
          type="submit"
          disabled={pending}
          className="w-full bg-ink px-8 py-4 text-label uppercase text-paper transition-colors hover:bg-bronze disabled:opacity-60"
        >
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>

      {variant === "staff" && (
        <p className="mt-6 border-t border-hairline pt-6 text-sm text-ink-secondary">
          Setting up for the first time?{" "}
          <Link
            href="/staff/mfa/enroll"
            className="underline underline-offset-4 transition-colors hover:text-bronze"
          >
            Enroll your device
          </Link>
        </p>
      )}

      {/*
        These links are ALWAYS rendered, never conditioned on the error.
        The backend's 401 is deliberately ambiguous (wrong credentials vs
        unverified email — enumeration-safe by design); branching the UI on
        the failure reason would leak that signal back. They are also
        customer-only now that the form has two surfaces: registration and
        email recovery are storefront concerns (SPEC-20-10).

        W13-T10 (design parity): the stack is centred under a hairline rule,
        per the sign-in screen. "New here? Create an account" keeps the
        shipped honest English (the screen's "Do not possess an account?"
        reads as translated boilerplate); "Resend verification email" and
        the "Forgot your" pair follow the screen's wording.
      */}
      {variant === "customer" && (
        <nav
          aria-label="Account recovery"
          className="mt-8 flex flex-col items-center gap-3 border-t border-hairline pt-8 text-center text-sm text-ink-secondary"
        >
          <p>
            New here?{" "}
            <Link
              href="/register"
              className="underline underline-offset-4 transition-colors hover:text-bronze"
            >
              Create an account
            </Link>
          </p>
          <Link
            href="/resend-verification"
            className="underline underline-offset-4 transition-colors hover:text-bronze"
          >
            Resend verification email
          </Link>
          <Link
            href="/forgot-username"
            className="underline underline-offset-4 transition-colors hover:text-bronze"
          >
            Forgot your username
          </Link>
          <Link
            href="/forgot-password"
            className="underline underline-offset-4 transition-colors hover:text-bronze"
          >
            Forgot your password
          </Link>
          <p>
            Staff member?{" "}
            <Link
              href="/staff/login"
              className="underline underline-offset-4 transition-colors hover:text-bronze"
            >
              Staff sign in
            </Link>
          </p>
        </nav>
      )}
    </div>
  );
}

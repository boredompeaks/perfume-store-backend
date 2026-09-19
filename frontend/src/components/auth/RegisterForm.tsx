"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { register, usernameAvailable } from "@/lib/accounts-api";
import { inputClass } from "@/lib/ui";

type Availability =
  | { state: "idle" }
  | { state: "checking" }
  | { state: "ok" }
  | { state: "taken" }
  | { state: "short" };

export default function RegisterForm() {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [registered, setRegistered] = useState<{ emailFailed: boolean } | null>(null);
  const [availability, setAvailability] = useState<Availability>({ state: "idle" });
  const latestCheck = useRef(0);

  // Live username availability — debounced, 3-char minimum (matches backend).
  useEffect(() => {
    const trimmed = username.trim();
    if (trimmed.length < 3) {
      setAvailability({ state: "short" });
      return;
    }
    const requestId = ++latestCheck.current;
    setAvailability({ state: "checking" });
    const timer = setTimeout(() => {
      usernameAvailable(trimmed)
        .then((res) => {
          if (latestCheck.current !== requestId) return;
          setAvailability(res.available ? { state: "ok" } : { state: "taken" });
        })
        .catch(() => {
          if (latestCheck.current !== requestId) return;
          setAvailability({ state: "idle" }); // check is a courtesy; server re-validates
        });
    }, 400);
    return () => clearTimeout(timer);
  }, [username]);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setPending(true);
    setFormError(null);
    setFieldErrors({});
    try {
      await register(username.trim(), email.trim(), password);
      setRegistered({ emailFailed: false });
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 503) {
          // Backend created the account but the verification email failed.
          setRegistered({ emailFailed: true });
        } else if (err.fieldErrors) {
          setFieldErrors(err.fieldErrors);
        } else {
          setFormError(err.message);
        }
      } else {
        setFormError("Registration failed. Check your connection and try again.");
      }
    } finally {
      setPending(false);
    }
  };

  if (registered) {
    return (
      <div className="border border-line p-8">
        <h2 className="font-display text-2xl tracking-tight">
          {registered.emailFailed
            ? "Account created — one step left."
            : "Check your email."}
        </h2>
        <p className="mt-3 text-ink-muted">
          {registered.emailFailed
            ? "We created your account, but the verification email could not be sent just now. You can request it again below."
            : `We sent a verification link to ${email}. Open it to activate your account — sign-in stays locked until then.`}
        </p>
        <Link
          href="/resend-verification"
          className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          {registered.emailFailed
            ? "Resend verification email"
            : "Resend verification email"}
        </Link>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4" noValidate>
      <div>
        <label htmlFor="reg-username" className="mb-1 block text-sm">
          Username
        </label>
        <input
          id="reg-username"
          type="text"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          aria-describedby="reg-username-hint"
          className={`${inputClass} w-full`}
          required
        />
        <p id="reg-username-hint" className="mt-1 text-sm text-ink-muted" aria-live="polite">
          {availability.state === "checking"
            ? "Checking availability…"
            : availability.state === "ok"
              ? "Available."
              : availability.state === "taken"
                ? "This username is already taken."
                : availability.state === "short" && username.trim().length > 0
                  ? "At least 3 characters."
                  : ""}
        </p>
        {fieldErrors.username?.map((msg) => (
          <p key={msg} role="alert" className="mt-1 text-sm text-bronze">
            {msg}
          </p>
        ))}
      </div>

      <div>
        <label htmlFor="reg-email" className="mb-1 block text-sm">
          Email
        </label>
        <input
          id="reg-email"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className={`${inputClass} w-full`}
          required
        />
        {fieldErrors.email?.map((msg) => (
          <p key={msg} role="alert" className="mt-1 text-sm text-bronze">
            {msg}
          </p>
        ))}
      </div>

      <div>
        <label htmlFor="reg-password" className="mb-1 block text-sm">
          Password
        </label>
        <input
          id="reg-password"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className={`${inputClass} w-full`}
          required
          minLength={8}
        />
        <p className="mt-1 text-sm text-ink-muted">At least 8 characters.</p>
        {fieldErrors.password?.map((msg) => (
          <p key={msg} role="alert" className="mt-1 text-sm text-bronze">
            {msg}
          </p>
        ))}
      </div>

      {formError && (
        <p role="alert" className="text-sm text-bronze" aria-live="polite">
          {formError}
        </p>
      )}

      <button
        type="submit"
        disabled={pending}
        className="w-full bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-60"
      >
        {pending ? "Creating account…" : "Create account"}
      </button>

      <p className="text-sm text-ink-muted">
        Already have an account?{" "}
        <Link href="/login" className="underline underline-offset-4 transition-colors hover:text-bronze">
          Sign in
        </Link>
      </p>
    </form>
  );
}

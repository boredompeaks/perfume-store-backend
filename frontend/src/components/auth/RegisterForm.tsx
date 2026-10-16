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

/**
 * The account-created terminal state. `message` is the SERVER's copy,
 * rendered verbatim in both branches (§5.9): the 201 message on success,
 * the 503 `error` when the account exists but the verification email could
 * not be sent. The 503 is a state of its own — not a failure of the form —
 * so it renders in a status panel (role="status"), distinct from the
 * role="alert" error surfaces, with the §5.9-named link to
 * /resend-verification.
 */
type Registered = { emailFailed: boolean; message: string };

export default function RegisterForm() {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [registered, setRegistered] = useState<Registered | null>(null);
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
      const res = await register(username.trim(), email.trim(), password);
      setRegistered({ emailFailed: false, message: res.message });
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 503) {
          // Backend created the account but the verification email failed —
          // its `error` string is the copy for that state, verbatim.
          setRegistered({ emailFailed: true, message: err.message });
        } else if (err.fieldErrors) {
          setFieldErrors(err.fieldErrors);
        } else {
          // Includes the 429 throttle wording: whatever the server said,
          // shown exactly as said (server copy verbatim, §5.9 / P5).
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
      <div className="border border-hairline p-8" role="status" aria-live="polite">
        <p className="text-ink">{registered.message}</p>
        <Link
          href="/resend-verification"
          className="mt-6 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Resend verification email
        </Link>
      </div>
    );
  }

  const hasUsernameError = Boolean(fieldErrors.username?.length);
  const hasEmailError = Boolean(fieldErrors.email?.length);
  const hasPasswordError = Boolean(fieldErrors.password?.length);

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
          aria-invalid={hasUsernameError ? true : undefined}
          aria-describedby={
            hasUsernameError ? "reg-username-hint reg-username-err" : "reg-username-hint"
          }
          className={`${inputClass} w-full`}
          required
        />
        <p id="reg-username-hint" className="mt-1 text-sm text-ink-secondary" aria-live="polite">
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
        {hasUsernameError && (
          <div id="reg-username-err">
            {fieldErrors.username?.map((msg) => (
              <p key={msg} role="alert" className="mt-1 text-sm text-danger">
                {msg}
              </p>
            ))}
          </div>
        )}
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
          aria-invalid={hasEmailError ? true : undefined}
          aria-describedby={hasEmailError ? "reg-email-err" : undefined}
          className={`${inputClass} w-full`}
          required
        />
        {hasEmailError && (
          <div id="reg-email-err">
            {fieldErrors.email?.map((msg) => (
              <p key={msg} role="alert" className="mt-1 text-sm text-danger">
                {msg}
              </p>
            ))}
          </div>
        )}
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
          aria-invalid={hasPasswordError ? true : undefined}
          aria-describedby={hasPasswordError ? "reg-password-err" : undefined}
          className={`${inputClass} w-full`}
          required
          minLength={8}
        />
        {/* Static hint only — the real policy is the server's
            validate_password (SPEC-1-03); its messages arrive under
            `password` and render per message below, never paraphrased. */}
        <p className="mt-1 text-sm text-ink-secondary">At least 8 characters.</p>
        {hasPasswordError && (
          <div id="reg-password-err">
            {fieldErrors.password?.map((msg) => (
              <p key={msg} role="alert" className="mt-1 text-sm text-danger">
                {msg}
              </p>
            ))}
          </div>
        )}
      </div>

      {formError && (
        <p role="alert" className="text-sm text-danger" aria-live="polite">
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

      <p className="text-sm text-ink-secondary">
        Already have an account?{" "}
        <Link href="/login" className="underline underline-offset-4 transition-colors hover:text-bronze">
          Sign in
        </Link>
      </p>
    </form>
  );
}

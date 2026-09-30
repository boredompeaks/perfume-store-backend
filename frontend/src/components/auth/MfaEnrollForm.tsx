"use client";

import Link from "next/link";
import { useState } from "react";
import { apiFetch, ApiError } from "@/lib/api";
import {
  enrollmentConfirmRequest,
  enrollmentSetupRequest,
  isPlausibleCode,
  type MfaRequest,
} from "@/lib/mfa";
import { inputClass } from "@/lib/ui";

type Phase =
  | { kind: "credentials" }
  | { kind: "scan"; secret: string; otpauthUri: string }
  | { kind: "enabled" };

/**
 * SPEC-20-13: the enrollment surface an unenrolled privileged account needs.
 *
 * Mandatory MFA (R-17.9) refuses its login at both doors, and the login
 * page sends blocked accounts here. Both steps prove the first factor with
 * username+password (the backend's bootstrap window, open precisely while
 * no device is active) because such an account cannot hold a JWT yet — it
 * could not have logged in to get one.
 *
 * The password stays in component state between the two steps and is never
 * persisted or logged; the secret is rendered once, from the setup response,
 * and the component holds nothing after the device is confirmed.
 */
export default function MfaEnrollForm() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [phase, setPhase] = useState<Phase>({ kind: "credentials" });
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const message = (err: unknown, fallback: string) =>
    err instanceof ApiError
      ? (err.fieldErrors?.code?.[0] ?? err.message)
      : fallback;

  const send = async (request: MfaRequest) => {
    setPending(true);
    setError(null);
    try {
      return await apiFetch<Record<string, string>>(request.path, {
        method: request.method,
        body: request.body,
      });
    } finally {
      setPending(false);
    }
  };

  const onSetup = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const res = await send(enrollmentSetupRequest({ username, password }));
      setPhase({
        kind: "scan",
        secret: res.secret,
        otpauthUri: res.otpauth_uri,
      });
    } catch (err) {
      setError(
        message(
          err,
          "We couldn't start enrollment. Check your connection and try again.",
        ),
      );
    }
  };

  const onConfirm = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await send(enrollmentConfirmRequest({ username, password }, code));
      setPhase({ kind: "enabled" });
      setCode("");
      setPassword("");
    } catch (err) {
      setError(
        message(err, "We couldn't confirm that code. Try again in a moment."),
      );
    }
  };

  if (phase.kind === "enabled") {
    return (
      <div className="space-y-4" role="status">
        <p className="text-sm">
          Multi-factor authentication is on. Sign in with your username,
          password and a fresh code from your authenticator app.
        </p>
        <Link
          href="/staff/login"
          className="underline underline-offset-4 transition-colors hover:text-bronze"
        >
          Continue to staff sign in
        </Link>
      </div>
    );
  }

  if (phase.kind === "scan") {
    return (
      <form onSubmit={onConfirm} className="space-y-6" noValidate>
        <div>
          <h2 className="font-display text-xl">Scan, then confirm</h2>
          <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-ink-muted">
            <li>Open your authenticator app and add a new account.</li>
            <li>Add the account with the setup key below.</li>
            <li>Enter the 6-digit code it shows to finish.</li>
          </ol>
        </div>

        {/* The provisioning URI and the bare secret both stay visible: the
            URI is what the app's "enter a setup key" flow pastes, the
            secret is what it asks for. */}
        <div className="space-y-3 border border-line p-4 text-sm">
          <p>
            <span className="block text-ink-muted">Setup key</span>
            <code className="block break-all">{phase.secret}</code>
          </p>
          <p>
            <span className="block text-ink-muted">Setup link</span>
            <code className="block break-all">{phase.otpauthUri}</code>
          </p>
        </div>

        <div>
          <label htmlFor="enroll-code" className="mb-1 block text-sm">
            6-digit code from your app
          </label>
          <input
            id="enroll-code"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            className={`${inputClass} w-full`}
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
          disabled={pending || !isPlausibleCode(code)}
          className="w-full bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-60"
        >
          {pending ? "Confirming…" : "Confirm and finish"}
        </button>
      </form>
    );
  }

  return (
    <form onSubmit={onSetup} className="space-y-4" noValidate>
      <p className="text-sm text-ink-muted">
        Multi-factor authentication is mandatory for staff accounts. Sign in
        with your credentials once to generate a secret, then scan it with
        your authenticator app.
      </p>
      <div>
        <label htmlFor="enroll-username" className="mb-1 block text-sm">
          Username
        </label>
        <input
          id="enroll-username"
          type="text"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          className={`${inputClass} w-full`}
          required
        />
      </div>
      <div>
        <label htmlFor="enroll-password" className="mb-1 block text-sm">
          Password
        </label>
        <input
          id="enroll-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className={`${inputClass} w-full`}
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
        disabled={pending || !username.trim() || !password}
        className="w-full bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-60"
      >
        {pending ? "Generating…" : "Generate my setup key"}
      </button>
    </form>
  );
}
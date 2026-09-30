"use client";

import Link from "next/link";
import { useState } from "react";
import { apiFetch, ApiError } from "@/lib/api";
import {
  enrollmentConfirmRequest,
  enrollmentSetupRequest,
  isPlausibleCode,
  qrImageSource,
  type MfaRequest,
  type MfaSetup,
} from "@/lib/mfa";
import { inputClass } from "@/lib/ui";

type Phase =
  | { kind: "credentials" }
  | { kind: "scan"; setup: MfaSetup }
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

  const send = async <T,>(request: MfaRequest): Promise<T> => {
    setPending(true);
    setError(null);
    try {
      return await apiFetch<T>(request.path, {
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
      const setup = await send<MfaSetup>(
        enrollmentSetupRequest({ username, password }),
      );
      setPhase({ kind: "scan", setup });
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
      await send<{ enabled: boolean }>(
        enrollmentConfirmRequest({ username, password }, code),
      );
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
    const qr = qrImageSource(phase.setup);
    return (
      <form onSubmit={onConfirm} className="space-y-6" noValidate>
        <div>
          <h2 className="font-display text-xl">Scan, then confirm</h2>
          <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-ink-muted">
            <li>Open your authenticator app and add a new account.</li>
            <li>
              {qr
                ? "Scan the code, or type the setup key by hand."
                : "Enter the setup key below by hand."}
            </li>
            <li>Enter the 6-digit code it shows to finish.</li>
          </ol>
        </div>

        {qr && (
          <img
            src={qr}
            alt="QR code for adding this account to an authenticator app"
            width={192}
            height={192}
            className="border border-line-strong bg-surface p-2"
          />
        )}

        {/* The manual fallback is always present: SPEC-20-9 renders the QR
            from the provisioning URI the backend already returns, but plenty
            of authenticator flows cannot scan (no camera, a desktop app that
            wants the key), and the secret is the last resort. */}
        <div className="space-y-3 border border-line p-4 text-sm">
          <p>
            <span className="block text-ink-muted">Setup key</span>
            <code className="block break-all">{phase.setup.secret}</code>
          </p>
          <p>
            <span className="block text-ink-muted">Setup link</span>
            <code className="block break-all">{phase.setup.otpauth_uri}</code>
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
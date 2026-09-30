/**
 * MFA (R-17.9) request shapes and the enrollment flow they drive.
 *
 * SPEC-20-13: mandatory MFA refuses an unenrolled privileged login, which
 * left such an account with no way to enroll — the endpoints existed but
 * nothing in the UI reached them. This module is the pure half of the
 * enrollment surface (the React half is MfaEnrollForm): which endpoint a
 * step posts to and what body it sends, so the rules are unit-testable
 * without a DOM and so the two steps cannot drift apart.
 *
 * The setup/confirm bodies carry the caller's OWN credentials, not a token:
 * an account with no confirmed device can never hold a JWT (login demands
 * the factor it does not have yet), which is exactly why the backend's
 * `_authorize_enrollment` bootstrap window exists. Both steps send the same
 * proof so the pair reads as one flow.
 *
 * Nothing here ever stores or logs a secret: `secret` / `otpauth_uri` are
 * read off the setup response, handed to the UI once, and dropped.
 */

export const MFA_SETUP_PATH = "/api/accounts/mfa/setup/";
export const MFA_CONFIRM_PATH = "/api/accounts/mfa/confirm/";
export const MFA_STATUS_PATH = "/api/accounts/mfa/status/";

/** RFC 6238 codes are 6 digits; the backend accepts them space-separated. */
export const TOTP_DIGITS = 6;

export type Credentials = { username: string; password: string };

export type MfaRequest = {
  path: string;
  method: "POST";
  body: Record<string, string>;
};

/**
 * Step 1: mint the secret. `code` is only needed when a device is ALREADY
 * active (the backend's re-enrollment guard re-proves the factor); during
 * the bootstrap window there is no device, so it is left out entirely
 * rather than sent empty.
 */
export function enrollmentSetupRequest(
  creds: Credentials,
  code?: string,
): MfaRequest {
  const body: Record<string, string> = {
    username: creds.username.trim(),
    password: creds.password,
  };
  const normalized = normalizeCode(code);
  if (normalized) body.code = normalized;
  return { path: MFA_SETUP_PATH, method: "POST", body };
}

/** Step 2: prove possession of the secret to activate the device. */
export function enrollmentConfirmRequest(
  creds: Credentials,
  code: string,
): MfaRequest {
  return {
    path: MFA_CONFIRM_PATH,
    method: "POST",
    body: {
      username: creds.username.trim(),
      password: creds.password,
      code: normalizeCode(code),
    },
  };
}

/** Digits only, spaces dropped — what the authenticator app displays. */
export function normalizeCode(code: string | undefined): string {
  return (code ?? "").replace(/\s+/g, "");
}

/**
 * Client-side shape check so an obvious typo never spends a request from
 * the shared 'auth' throttle budget. The server re-verifies regardless —
 * this is ergonomics, not a security gate.
 */
export function isPlausibleCode(code: string): boolean {
  return new RegExp(`^\\d{${TOTP_DIGITS}}$`).test(normalizeCode(code));
}
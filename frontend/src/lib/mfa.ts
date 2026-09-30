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

/** What POST /api/accounts/mfa/setup/ answers with (SPEC-20-9 adds the QR). */
export type MfaSetup = {
  secret: string;
  otpauth_uri: string;
  qr_data_uri?: string | null;
};

/**
 * The backend's inline QR prefix. An <img src> is the only thing allowed to
 * consume the field, so anything else is dropped rather than rendered: an
 * unexpected scheme must never reach the DOM as an image source.
 */
export const QR_DATA_URI_PREFIX = "data:image/svg+xml;base64,";

/**
 * The scannable code for the provisioning URI, or null when the response
 * carries no usable artifact — the manual setup key beside it always works,
 * so a missing or unexpected QR degrades to typing rather than to a dead
 * step.
 */
export function qrImageSource(setup: Pick<MfaSetup, "qr_data_uri">): string | null {
  const artifact = setup.qr_data_uri;
  return artifact?.startsWith(QR_DATA_URI_PREFIX) ? artifact : null;
}

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

/**
 * SPEC-20-10: the two sign-in doors. `staff` is the privileged surface that
 * enforces R-17.9 (`/api/accounts/login/`); `customer` is the storefront
 * one, which declares no `totp` field at all and refuses privileged
 * accounts server-side.
 */
export type LoginSurface = "customer" | "staff";

export const LOGIN_PATHS: Record<LoginSurface, string> = {
  customer: "/api/accounts/storefront/login/",
  staff: "/api/accounts/login/",
};

export function loginEndpoint(surface: LoginSurface): string {
  return LOGIN_PATHS[surface];
}

/**
 * The body for a login POST. `totp` is carried on the staff door only —
 * even if a caller passes one, the customer body can never grow the field,
 * so the storefront form cannot offer an authentication code it has no way
 * to honour (and the server ignores one if a crafted request sends it).
 */
export function buildLoginBody(
  surface: LoginSurface,
  creds: Credentials,
  code?: string,
): Record<string, string> {
  const body: Record<string, string> = {
    username: creds.username.trim(),
    password: creds.password,
  };
  const normalized = normalizeCode(code);
  if (surface === "staff" && normalized) body.totp = normalized;
  return body;
}
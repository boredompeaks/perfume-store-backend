/**
 * Enrollment bodies carry credentials, not a JWT: an account with no confirmed
 * device can never hold one. Secrets are handed to the UI once and never
 * stored or logged here.
 */

export const MFA_SETUP_PATH = "/api/accounts/mfa/setup/";
export const MFA_CONFIRM_PATH = "/api/accounts/mfa/confirm/";

/** RFC 6238 codes are 6 digits; the backend accepts them space-separated. */
export const TOTP_DIGITS = 6;

export type Credentials = { username: string; password: string };

export type MfaRequest = {
  path: string;
  method: "POST";
  body: Record<string, string>;
};

/** Response shape of the setup endpoint. */
export type MfaSetup = {
  secret: string;
  otpauth_uri: string;
  qr_data_uri?: string | null;
};

/**
 * Only an inline SVG data URI may reach an `<img src>`; anything else would
 * put an unexpected scheme in the DOM.
 */
export const QR_DATA_URI_PREFIX = "data:image/svg+xml;base64,";

/**
 * Null when no usable artifact: the manual key beside it always works, so a
 * missing or unexpected QR degrades to typing rather than a dead step.
 */
export function qrImageSource(setup: Pick<MfaSetup, "qr_data_uri">): string | null {
  const artifact = setup.qr_data_uri;
  return artifact?.startsWith(QR_DATA_URI_PREFIX) ? artifact : null;
}

/**
 * `code` is needed only when a device is already active (the backend
 * re-proves the factor); during enrollment it is omitted rather than empty.
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
 * Client-side shape check to avoid spending the shared auth throttle budget
 * on obvious typos; the server re-verifies regardless.
 */
export function isPlausibleCode(code: string): boolean {
  return new RegExp(`^\\d{${TOTP_DIGITS}}$`).test(normalizeCode(code));
}

/**
 * The staff surface enforces the TOTP factor; the customer surface declares
 * no `totp` field and refuses privileged accounts server-side.
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
 * `totp` is staff-only; the customer body can never grow the field even when
 * a caller passes one.
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
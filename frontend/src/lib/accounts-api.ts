import { apiFetch } from "./api";

export type RegisterResult = { message: string; user: { id: number; username: string; email: string } };

export function register(username: string, email: string, password: string) {
  return apiFetch<RegisterResult>("/api/accounts/register/", {
    method: "POST",
    body: { username, email, password },
  });
}

export function usernameAvailable(username: string) {
  return apiFetch<{ available: boolean; message: string }>(
    `/api/accounts/username-available/?username=${encodeURIComponent(username)}`,
  );
}

export function verifyEmail(uid: string, token: string) {
  return apiFetch<{ message: string }>("/api/accounts/verify-email/", {
    method: "POST",
    body: { uid, token },
  });
}

export function resendVerification(email: string) {
  return apiFetch<{ message: string }>(
    "/api/accounts/resend-verification/",
    { method: "POST", body: { email } },
  );
}

export function forgotUsername(email: string) {
  return apiFetch<{ message: string }>("/api/accounts/forgot-username/", {
    method: "POST",
    body: { email },
  });
}

export function requestPasswordReset(email: string) {
  return apiFetch<{ message: string }>("/api/accounts/password-reset/", {
    method: "POST",
    body: { email },
  });
}

export function resetPassword(uid: string, token: string, password: string) {
  return apiFetch<{ message: string }>(
    "/api/accounts/password-reset/confirm/",
    { method: "POST", body: { uid, token, password } },
  );
}

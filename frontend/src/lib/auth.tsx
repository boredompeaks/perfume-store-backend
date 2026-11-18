"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import { apiFetch } from "./api";
import { clearGuestToken } from "./guest-token";
import { buildLoginBody, loginEndpoint, type LoginSurface } from "./mfa";
import { forgetShipping } from "./shipping-store";
import {
  clearTokens,
  decodeJwtUserId,
  hasSessionHint,
  refreshAccessToken,
  setTokens,
} from "./tokens";

export type AuthStatus = "loading" | "authenticated" | "anonymous";

type AuthContextValue = {
  status: AuthStatus;
  userId: number | null;
  /** The customer door has no MFA field; the staff door enforces TOTP. */
  login: (
    surface: LoginSurface,
    username: string,
    password: string,
    totp?: string,
  ) => Promise<void>;
  logout: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [userId, setUserId] = useState<number | null>(null);

  // The refresh token is HttpOnly, so a non-secret hint decides whether the
  // one throttled refresh probe is worth making; no hint means anonymous.
  useEffect(() => {
    if (!hasSessionHint()) {
      setStatus("anonymous");
      return;
    }
    refreshAccessToken().then((access) => {
      if (access) {
        setUserId(decodeJwtUserId(access));
        setStatus("authenticated");
      } else {
        clearTokens();
        setStatus("anonymous");
      }
    });
  }, []);

  const login = useCallback(
    async (
      surface: LoginSurface,
      username: string,
      password: string,
      totp?: string,
    ) => {
      // Only the access token is returned; the refresh token stays an
      // HttpOnly cookie and never enters JS-readable storage.
      const data = await apiFetch<{ access: string }>(loginEndpoint(surface), {
        method: "POST",
        body: buildLoginBody(surface, { username, password }, totp),
      });
      setTokens(data.access);
      setUserId(decodeJwtUserId(data.access));
      setStatus("authenticated");
    },
    [],
  );

  const logout = useCallback(() => {
    // Only the backend can revoke the HttpOnly cookie, so this call is
    // best-effort; local state clears regardless so the UI never traps users.
    void apiFetch("/api/accounts/logout/", { method: "POST", auth: true }).catch(
      () => {},
    );
    clearTokens();
    // Saved checkout details are PII and must not outlive logout.
    forgetShipping();
    // Explicit logout is the only clear point for the guest order token.
    clearGuestToken();
    setUserId(null);
    setStatus("anonymous");
  }, []);

  return (
    <AuthContext.Provider value={{ status, userId, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

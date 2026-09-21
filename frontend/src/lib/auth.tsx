"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import { apiFetch } from "./api";
import { forgetShipping } from "./shipping-store";
import {
  clearTokens,
  decodeJwtUserId,
  hasSessionHint,
  refreshAccessToken,
  setTokens,
} from "./tokens";

type AuthStatus = "loading" | "authenticated" | "anonymous";

type AuthContextValue = {
  status: AuthStatus;
  userId: number | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [userId, setUserId] = useState<number | null>(null);

  // Bootstrap: the refresh token lives in an HttpOnly cookie we cannot
  // read, so a non-secret hint decides whether a refresh probe is worth
  // one throttled call. No hint (first visit, logged out) = anonymous
  // without any request.
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

  const login = useCallback(async (username: string, password: string) => {
    // The login response carries only the access token: the refresh token
    // is set as an HttpOnly cookie by the backend (R-17.12) and never
    // enters JS-readable storage.
    const data = await apiFetch<{ access: string }>(
      "/api/accounts/login/",
      { method: "POST", body: { username, password } },
    );
    setTokens(data.access);
    setUserId(decodeJwtUserId(data.access));
    setStatus("authenticated");
  }, []);

  const logout = useCallback(() => {
    // The refresh token lives only in an HttpOnly cookie, so the backend
    // must do the revoking: this call blacklists the cookie token and
    // clears the cookie. Best-effort — local state clears regardless of
    // the outcome (dead cookie, offline) so the UI never traps the user.
    void apiFetch("/api/accounts/logout/", { method: "POST", auth: true }).catch(
      () => {},
    );
    clearTokens();
    // PII: never leave saved checkout details behind on a logged-out device.
    forgetShipping();
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

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
  getStoredRefreshToken,
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

  // Bootstrap: exchange a stored refresh token for a fresh access token.
  useEffect(() => {
    if (!getStoredRefreshToken()) {
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
    const data = await apiFetch<{ access: string; refresh: string }>(
      "/api/accounts/login/",
      { method: "POST", body: { username, password } },
    );
    setTokens(data.access, data.refresh);
    setUserId(decodeJwtUserId(data.access));
    setStatus("authenticated");
  }, []);

  const logout = useCallback(() => {
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

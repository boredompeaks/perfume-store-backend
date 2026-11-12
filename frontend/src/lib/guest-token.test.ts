import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  GUEST_TOKEN_STORAGE_KEY,
  clearGuestToken,
  getGuestToken,
  setGuestToken,
} from "./guest-token";

/**
 * Storage rules only: round trip, blank rejection, explicit-logout clear, and
 * the never-logged / never-a-URL source guards.
 */

/**
 * Low-entropy on purpose: a random-looking fixture trips the high-entropy
 * secret detector even in tests.
 */
const TOKEN = "test-guest-token-fixture";

function installFakeSessionStorage() {
  const store = new Map<string, string>();
  vi.stubGlobal("window", {
    sessionStorage: {
      getItem: (key: string) => (store.has(key) ? store.get(key)! : null),
      setItem: (key: string, value: string) => {
        store.set(key, String(value));
      },
      removeItem: (key: string) => {
        store.delete(key);
      },
    },
  });
  return store;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("guest-token storage (sessionStorage, G1)", () => {
  it("pins the storage key so a rename is a deliberate, visible change", () => {
    expect(GUEST_TOKEN_STORAGE_KEY).toBe("aurel.guest-order");
  });

  it("round-trips the credential verbatim", () => {
    installFakeSessionStorage();
    setGuestToken(TOKEN);
    expect(getGuestToken()).toBe(TOKEN);
  });

  it("returns null when nothing is stored", () => {
    installFakeSessionStorage();
    expect(getGuestToken()).toBeNull();
  });

  it("never stores a blank value — it is not a usable credential", () => {
    const store = installFakeSessionStorage();
    setGuestToken("");
    setGuestToken("   ");
    expect(getGuestToken()).toBeNull();
    expect(store.size).toBe(0);
  });

  it("clears on the explicit-logout clear point", () => {
    installFakeSessionStorage();
    setGuestToken(TOKEN);
    clearGuestToken();
    expect(getGuestToken()).toBeNull();
  });

  it("degrades to no-token without a window (SSR / storage-less)", () => {
    // No `window`: every accessor must no-op.
    expect(getGuestToken()).toBeNull();
    expect(() => setGuestToken(TOKEN)).not.toThrow();
    expect(() => clearGuestToken()).not.toThrow();
    expect(getGuestToken()).toBeNull();
  });
});

describe("never-rules (source guards)", () => {
  const guestTokenSource = readFileSync(
    fileURLToPath(new URL("./guest-token.ts", import.meta.url)),
    "utf8",
  );
  const ordersApiSource = readFileSync(
    fileURLToPath(new URL("./orders-api.ts", import.meta.url)),
    "utf8",
  );

  it("the token is never logged — the module writes to no console", () => {
    expect(guestTokenSource).not.toMatch(/console\./);
  });

  it("the token never touches a URL surface (location/history/query)", () => {
    expect(guestTokenSource).not.toMatch(
      /location\.(search|hash|href)|pushState|replaceState|URLSearchParams/,
    );
  });

  it("storage lives only in guest-token — orders-api never touches sessionStorage", () => {
    expect(ordersApiSource).not.toMatch(/sessionStorage/);
  });
});

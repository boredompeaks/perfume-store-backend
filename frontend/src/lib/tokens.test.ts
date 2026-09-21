import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Token-storage contract (SPEC-17-02, R-17.12): the refresh token never
 * touches JS-readable storage. Only the short-lived access token lives in
 * memory; localStorage may hold nothing but the non-secret session hint.
 */
const storage = new Map<string, string>();
const writes: Array<[string, string]> = [];

vi.stubGlobal("localStorage", {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => {
    writes.push([key, value]);
    storage.set(key, value);
  },
  removeItem: (key: string) => void storage.delete(key),
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  storage.clear();
  writes.length = 0;
  vi.resetModules();
});

describe("token store", () => {
  it("setTokens keeps the access token in memory only", async () => {
    const tokens = await import("./tokens");

    tokens.setTokens("access-1");

    expect(tokens.getAccessToken()).toBe("access-1");
    // The session hint (boolean "1") is written — no token value ever is.
    expect(storage.get("aurel.session")).toBe("1");
    for (const [key, value] of writes) {
      expect(key).toBe("aurel.session");
      expect(value).toBe("1");
    }
  });

  it("clearTokens wipes the memory token and the session hint", async () => {
    const tokens = await import("./tokens");

    tokens.setTokens("access-1");
    tokens.clearTokens();

    expect(tokens.getAccessToken()).toBeNull();
    expect(tokens.hasSessionHint()).toBe(false);
    expect(storage.size).toBe(0);
  });

  it("refresh posts an empty body with credentials and adopts the new access token", async () => {
    const calls: Array<{ input: string; init: RequestInit }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        calls.push({ input: String(input), init: init ?? {} });
        return jsonResponse({ access: "access-2" });
      }),
    );
    const tokens = await import("./tokens");

    tokens.setTokens("access-1");
    const refreshed = await tokens.refreshAccessToken();

    expect(refreshed).toBe("access-2");
    expect(tokens.getAccessToken()).toBe("access-2");
    const { init } = calls[0];
    expect(calls[0].input).toContain("/api/accounts/token/refresh/");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual({});
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });

  it("a rejected refresh clears the session hint and access token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "Token is invalid" }, 401)),
    );
    const tokens = await import("./tokens");

    tokens.setTokens("access-1");
    const refreshed = await tokens.refreshAccessToken();

    expect(refreshed).toBeNull();
    expect(tokens.getAccessToken()).toBeNull();
    expect(tokens.hasSessionHint()).toBe(false);
  });

  it("a transient network failure keeps the session hint", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network down");
      }),
    );
    const tokens = await import("./tokens");

    tokens.setTokens("access-1");
    const refreshed = await tokens.refreshAccessToken();

    expect(refreshed).toBeNull();
    expect(tokens.hasSessionHint()).toBe(true);
  });

  it("concurrent refresh calls share one request (single-flight)", async () => {
    let refreshCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        refreshCalls++;
        return jsonResponse({ access: "access-2" });
      }),
    );
    const tokens = await import("./tokens");

    const [a, b] = await Promise.all([
      tokens.refreshAccessToken(),
      tokens.refreshAccessToken(),
    ]);

    expect(a).toBe("access-2");
    expect(b).toBe("access-2");
    expect(refreshCalls).toBe(1);
  });

  it("decodeJwtUserId extracts the user id from an access token", async () => {
    const tokens = await import("./tokens");
    const payload = btoa(JSON.stringify({ user_id: 42 }))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");

    expect(tokens.decodeJwtUserId(`h.${payload}.s`)).toBe(42);
    expect(tokens.decodeJwtUserId("garbage")).toBeNull();
  });
});

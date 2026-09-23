import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Silent-refresh contract tests: a 401 must trigger exactly ONE refresh
 * (single-flight, even for concurrent 401s), retry once with the new token,
 * and clear tokens on refresh failure — no loops, no silent failures.
 * Since SPEC-17-02 the refresh token rides an HttpOnly cookie: tests seed
 * only the in-memory access token + the non-secret session hint, never a
 * stored token.
 */
const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => void storage.set(key, value),
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
  vi.resetModules();
});

async function seedSession() {
  const tokens = await import("./tokens");
  tokens.setTokens("access-1");
  return tokens;
}

describe("apiFetch silent refresh", () => {
  it("on 401: refreshes once, retries with the new token, returns data", async () => {
    let refreshCalls = 0;
    let orderCalls = 0;
    let lastAuth: string | undefined;
    let refreshCredentials: RequestCredentials | undefined;
    let refreshBody: string | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/token/refresh/")) {
          refreshCalls++;
          refreshCredentials = init?.credentials;
          refreshBody = init?.body as string;
          return jsonResponse({ access: "access-2" });
        }
        if (url.includes("/api/orders/")) {
          orderCalls++;
          lastAuth = (init?.headers as Record<string, string>)?.Authorization;
          if (orderCalls === 1) return jsonResponse({ detail: "gone" }, 401);
          // SPEC-9-04: the history endpoint returns the house page-number
          // envelope; fetchOrders unwraps `results` for list consumers.
          return jsonResponse({
            count: 1,
            total_pages: 1,
            current_page: 1,
            next_page: false,
            previous_page: false,
            results: [{ id: 7 }],
          });
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );

    await seedSession();
    const { fetchOrders } = await import("./orders-api");

    const orders = await fetchOrders();

    expect(orders).toEqual([{ id: 7 }]);
    expect(refreshCalls).toBe(1);
    // R-17.12: the cookie is the credential — the body carries no token.
    expect(refreshCredentials).toBe("include");
    expect(JSON.parse(refreshBody ?? "{}")).toEqual({});
    expect(orderCalls).toBe(2);
    expect(lastAuth).toBe("Bearer access-2");
  });

  it("concurrent 401s share one refresh call (single-flight)", async () => {
    let refreshCalls = 0;
    let orderCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url.includes("/token/refresh/")) {
          refreshCalls++;
          return jsonResponse({ access: "access-2" });
        }
        orderCalls++;
        return jsonResponse({ detail: "gone" }, 401);
      }),
    );

    await seedSession();
    const { fetchOrders } = await import("./orders-api");

    const results = await Promise.allSettled([fetchOrders(), fetchOrders()]);

    // Both retries get the SAME shared access token → still 401 (the mock
    // always 401s orders) → ApiError, but only ONE refresh happened.
    // orderCalls = 2 requests × (initial + single retry) = 4.
    expect(results.every((r) => r.status === "rejected")).toBe(true);
    expect(refreshCalls).toBe(1);
    expect(orderCalls).toBe(4);
  });

  it("failed refresh: clears tokens, surfaces the 401, no retry", async () => {
    let orderCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url.includes("/token/refresh/")) {
          return jsonResponse({ detail: "bad refresh token" }, 401);
        }
        orderCalls++;
        return jsonResponse({ detail: "gone" }, 401);
      }),
    );

    await seedSession();
    const { fetchOrders } = await import("./orders-api");
    const tokens = await import("./tokens");

    await expect(fetchOrders()).rejects.toMatchObject({ status: 401 });

    expect(orderCalls).toBe(1);
    expect(tokens.getAccessToken()).toBeNull();
    expect(tokens.hasSessionHint()).toBe(false);
    expect(storage.size).toBe(0);
  });

  it("4xx responses surface field errors and scalar extras", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          { error: "Minimum order amount is required", minimum_order_amount: "500.00" },
          400,
        ),
      ),
    );
    const { applyCoupon } = await import("./orders-api");

    const err = await applyCoupon("X").catch((e) => e);

    expect(err.status).toBe(400);
    expect(err.message).toBe("Minimum order amount is required");
    expect(err.extras?.minimum_order_amount).toBe("500.00");
  });
});

describe("CSRF header on mutations (SPEC-17-03, R-17.18)", () => {
  /**
   * The backend's SessionCartCSRFAuthentication gate requires the
   * double-submit pair on every session-cookie mutation: the browser
   * sends the csrftoken cookie itself, the SPA must send the matching
   * X-CSRFToken header — read from the cookie, never invented.
   */
  function stubDocument(cookie: string) {
    vi.stubGlobal("document", { cookie });
  }

  it("mutations attach X-CSRFToken read from the csrftoken cookie", async () => {
    stubDocument("csrftoken=tok-123; sessionid=abc");
    let headers: Record<string, string> = {};
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
        headers = (init?.headers ?? {}) as Record<string, string>;
        return jsonResponse({});
      }),
    );
    const { apiFetch } = await import("./api");

    await apiFetch("/api/cart/", { method: "POST", body: { quantity: 1 } });

    expect(headers["X-CSRFToken"]).toBe("tok-123");
  });

  it("safe methods never send the header", async () => {
    stubDocument("csrftoken=tok-123");
    let headers: Record<string, string> = {};
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
        headers = (init?.headers ?? {}) as Record<string, string>;
        return jsonResponse({});
      }),
    );
    const { apiFetch } = await import("./api");

    await apiFetch("/api/cart/");

    expect(headers["X-CSRFToken"]).toBeUndefined();
  });

  it("no csrftoken cookie means no header, not a fake one", async () => {
    stubDocument("sessionid=abc");
    let headers: Record<string, string> = {};
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
        headers = (init?.headers ?? {}) as Record<string, string>;
        return jsonResponse({});
      }),
    );
    const { apiFetch } = await import("./api");

    await apiFetch("/api/cart/", { method: "DELETE" });

    expect(headers["X-CSRFToken"]).toBeUndefined();
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Silent-refresh contract tests: a 401 must trigger exactly ONE refresh
 * (single-flight, even for concurrent 401s), retry once with the new token,
 * and clear tokens on refresh failure — no loops, no silent failures.
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

describe("apiFetch silent refresh", () => {
  it("on 401: refreshes once, retries with the new token, returns data", async () => {
    let refreshCalls = 0;
    let orderCalls = 0;
    let lastAuth: string | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/token/refresh/")) {
          refreshCalls++;
          return jsonResponse({ access: "access-2" });
        }
        if (url.includes("/api/orders/")) {
          orderCalls++;
          lastAuth = (init?.headers as Record<string, string>)?.Authorization;
          if (orderCalls === 1) return jsonResponse({ detail: "gone" }, 401);
          return jsonResponse([{ id: 7 }]);
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );

    storage.set("aurel.refresh", "refresh-1");
    const { fetchOrders } = await import("./orders-api");

    const orders = await fetchOrders();

    expect(orders).toEqual([{ id: 7 }]);
    expect(refreshCalls).toBe(1);
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

    storage.set("aurel.refresh", "refresh-1");
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

    storage.set("aurel.refresh", "refresh-1");
    const { fetchOrders } = await import("./orders-api");
    const { getStoredRefreshToken } = await import("./tokens");

    await expect(fetchOrders()).rejects.toMatchObject({ status: 401 });

    expect(orderCalls).toBe(1);
    expect(getStoredRefreshToken()).toBeNull();
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

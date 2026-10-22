import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { applyCartCoupon, deleteCartItem, removeCartCoupon } from "./cart-api";

/**
 * D-04a/D-04b: cart calls must carry the access token when one exists so the
 * backend resolves the ACCOUNT cart. Without it the post-login refetch keeps
 * reading the guest cart (which the login merge has already emptied/adopted)
 * and the merged basket never renders.
 */
const storage = new Map<string, string>();

const ok = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

const err = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status });

const cartBody = {
  id: 1,
  items: [],
  coupon_code: "SAVE10",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const emptyCart = { id: 1, items: [], coupon_code: null };

beforeEach(() => {
  storage.clear();
  vi.resetModules();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => void storage.set(key, value),
    removeItem: (key: string) => void storage.delete(key),
  });
});

// The bearer tests below stub `fetch` per test; clearing it here keeps the
// two suites independent no matter which one a filtered run starts with.
afterEach(() => {
  vi.unstubAllGlobals();
});

async function seedToken() {
  const tokens = await import("./tokens");
  tokens.setTokens("access-1");
}

describe("cart requests carry the account credential", () => {
  it("every cart call sends the bearer when signed in", async () => {
    const seen: Array<{ url: string; auth?: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        seen.push({
          url: String(input),
          auth: (init?.headers as Record<string, string>)?.Authorization,
        });
        return jsonResponse(emptyCart);
      }),
    );
    await seedToken();
    const api = await import("./cart-api");

    await api.getCart();
    await api.addToCart(1, 2);
    await api.patchCartItem(5, 3);
    await api.deleteCartItem(5);

    expect(seen).toHaveLength(4);
    for (const call of seen) {
      expect(call.auth).toBe("Bearer access-1");
    }
  });

  it("the coupon preview sends the bearer so it prices the account cart", async () => {
    let auth: string | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
        auth = (init?.headers as Record<string, string>)?.Authorization;
        return jsonResponse({
          coupon: "SAVE10",
          subtotal: "1000.00",
          discount: "100.00",
          final_total: "900.00",
        });
      }),
    );
    await seedToken();
    const { applyCoupon } = await import("./orders-api");

    await applyCoupon("SAVE10");

    expect(auth).toBe("Bearer access-1");
  });

  it("a signed-out browser sends no Authorization header", async () => {
    let headers: Record<string, string> = {};
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
        headers = (init?.headers ?? {}) as Record<string, string>;
        return jsonResponse(emptyCart);
      }),
    );
    const api = await import("./cart-api");

    await api.getCart();

    expect(headers.Authorization).toBeUndefined();
  });
});

describe("applyCartCoupon", () => {
  it("POSTs the code to /api/cart/coupon/ and returns the cart body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok(cartBody));
    vi.stubGlobal("fetch", fetchMock);

    const result = await applyCartCoupon("SAVE10");

    expect(result.coupon_code).toBe("SAVE10");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/cart/coupon/");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ code: "SAVE10" }));
    expect(init.credentials).toBe("include");
  });

  it("maps the missing-code 400 to an ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(err(400, { error: "Coupon code is required" })),
    );

    const e = await applyCartCoupon("X").catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(400);
    expect(e.message).toBe("Coupon code is required");
  });

  it("maps the uniform rejection 400 to one non-leaking message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(err(400, { error: "Invalid coupon code" })),
    );

    const e = await applyCartCoupon("NOPE").catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(400);
    expect(e.message).toBe("Invalid coupon code");
  });

  it("maps cart-not-found 404 to an ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(err(404, { error: "Cart not found" })),
    );

    const e = await applyCartCoupon("SAVE10").catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(404);
    expect(e.message).toBe("Cart not found");
  });
});

describe("deleteCartItem — the cart line removal path", () => {
  it("DELETEs /api/cart/{id}/ with the session credentials and returns the cart body", async () => {
    const remaining = { ...cartBody, items: [{ id: 9, quantity: 2 }] };
    const fetchMock = vi.fn().mockResolvedValue(ok(remaining));
    vi.stubGlobal("fetch", fetchMock);

    const result = await deleteCartItem(7);

    // The re-render path CartLineItem relies on: the mutated cart body, so
    // no follow-up GET is needed after a line disappears.
    expect(result.items).toHaveLength(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/cart/7/");
    expect(init.method).toBe("DELETE");
    expect(init.credentials).toBe("include");
    expect(init.body).toBeUndefined();
  });

  it("maps cart-not-found 404 to an ApiError so the line can surface and refetch", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(err(404, { error: "Cart not found" })),
    );

    const e = await deleteCartItem(7).catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(404);
    expect(e.message).toBe("Cart not found");
  });
});

describe("removeCartCoupon", () => {
  it("DELETEs /api/cart/coupon/ and returns the cart body with coupon cleared", async () => {
    const cleared = { ...cartBody, coupon_code: null };
    const fetchMock = vi.fn().mockResolvedValue(ok(cleared));
    vi.stubGlobal("fetch", fetchMock);

    const result = await removeCartCoupon();

    expect(result.coupon_code).toBeNull();
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/cart/coupon/");
    expect(init.method).toBe("DELETE");
    expect(init.credentials).toBe("include");
  });

  it("maps cart-not-found 404 to an ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(err(404, { error: "Cart not found" })),
    );

    const e = await removeCartCoupon().catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(404);
    expect(e.message).toBe("Cart not found");
  });
});

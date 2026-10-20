import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * D-04a/D-04b: cart calls must carry the access token when one exists so the
 * backend resolves the ACCOUNT cart. Without it the post-login refetch keeps
 * reading the guest cart (which the login merge has already emptied/adopted)
 * and the merged basket never renders.
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

const emptyCart = { id: 1, items: [], coupon_code: null };

beforeEach(() => {
  storage.clear();
  vi.resetModules();
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

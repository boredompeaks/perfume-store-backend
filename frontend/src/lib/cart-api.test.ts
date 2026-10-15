import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { applyCartCoupon, removeCartCoupon } from "./cart-api";

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

afterEach(() => {
  vi.unstubAllGlobals();
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
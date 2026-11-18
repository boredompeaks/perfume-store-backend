import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { restockSubscribe, restockUnsubscribe } from "./products-api";
import { clearTokens, setTokens } from "./tokens";

/**
 * Pins the request shape, the three 2xx codes (authed 201/200, guest 202 —
 * hence `onStatus`), verbatim server messages, and the uniform DELETE 200.
 */

const SLUG = "oud-royale";

const ok = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const err = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status });

/** The uniform 4xx envelope. */
const envelope = (error: string, code: string) => ({
  error,
  code,
  details: {},
});

const preference = { product_id: 7, slug: SLUG, active: true };

beforeEach(() => {
  clearTokens();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("restockSubscribe", () => {
  it("POSTs the opt-in with the email body, JWT and CSRF, and reads 201 as created", async () => {
    vi.stubGlobal("document", { cookie: "csrftoken=tok-123; sessionid=abc" });
    const fetchMock = vi.fn().mockResolvedValue(ok(preference, 201));
    vi.stubGlobal("fetch", fetchMock);
    setTokens("access-1");

    const result = await restockSubscribe(SLUG, "buyer@example.com");

    expect(result.created).toBe(true);
    expect(result.preference).toEqual(preference);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(`/api/products/${SLUG}/restock-notifications/`);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual({
      email: "buyer@example.com",
    });
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer access-1");
    expect(headers["X-CSRFToken"]).toBe("tok-123");
  });

  it("reads 200 as already-registered (an existing preference re-armed)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(ok(preference, 200)));
    setTokens("access-1");

    const result = await restockSubscribe(SLUG, "buyer@example.com");

    expect(result.created).toBe(false);
    expect(result.preference).toEqual(preference);
  });

  it("reads the guest's uniform 202 as registered (signed out, no bearer)", async () => {
    // Signed out: a guest 202 must read as registered, never "already".
    const fetchMock = vi.fn().mockResolvedValue(ok(preference, 202));
    vi.stubGlobal("fetch", fetchMock);
    // Signed out, so no bearer is expected.

    const result = await restockSubscribe(SLUG, "buyer@example.com");

    expect(result.created).toBe(true);
    expect(result.preference).toEqual(preference);
    const [, init] = fetchMock.mock.calls[0];
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual({
      email: "buyer@example.com",
    });
  });

  it("surfaces the in-stock 400 message verbatim", async () => {
    const message = "Product is in stock; no restock notification needed.";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(err(400, envelope(message, "validation_error"))),
    );
    setTokens("access-1");

    const e = await restockSubscribe(SLUG, "a@b.co").catch((x) => x);

    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(400);
    expect(e.message).toBe(message);
  });

  it("surfaces the uniform 404 (unknown or delisted slug) verbatim", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(err(404, envelope("Product not found", "not_found"))),
    );
    setTokens("access-1");

    const e = await restockSubscribe(SLUG, "a@b.co").catch((x) => x);

    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(404);
    expect(e.message).toBe("Product not found");
  });

  it("surfaces the 429 throttle refusal verbatim for the inline alert", async () => {
    const message = "Request was throttled. Expected available in 12 seconds.";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(err(429, envelope(message, "throttled"))),
    );
    setTokens("access-1");

    const e = await restockSubscribe(SLUG, "a@b.co").catch((x) => x);

    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(429);
    expect(e.message).toBe(message);
  });

  it("a signed-in caller whose token fails refresh lands on the 401 verbatim", async () => {
    let refreshCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url.includes("/token/refresh/")) {
          refreshCalls++;
          return err(401, envelope("Token is invalid", "not_authenticated"));
        }
        return err(
          401,
          envelope(
            "Authentication credentials were not provided.",
            "not_authenticated",
          ),
        );
      }),
    );
    setTokens("access-1");

    const e = await restockSubscribe(SLUG, "a@b.co").catch((x) => x);

    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(401);
    expect(e.message).toBe("Authentication credentials were not provided.");
    expect(refreshCalls).toBe(1);
  });

  it("a signed-out submit that lands on a 401 (the remaining guest arms) attaches no bearer and probes refresh once", async () => {
    // The remaining guest 401 arms: no bearer, one refresh probe, message
    // verbatim.
    let refreshCalls = 0;
    let authHeader: string | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/token/refresh/")) {
          refreshCalls++;
          return err(401, envelope("Token is invalid", "not_authenticated"));
        }
        authHeader = (init?.headers as Record<string, string>)?.Authorization;
        return err(
          401,
          envelope(
            "Authentication credentials were not provided.",
            "not_authenticated",
          ),
        );
      }),
    );

    const e = await restockSubscribe(SLUG, "a@b.co").catch((x) => x);

    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(401);
    expect(authHeader).toBeUndefined();
    expect(refreshCalls).toBe(1);
  });
});

describe("restockUnsubscribe", () => {
  it("DELETEs the same path and reads the uniform opted-out body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({ active: false }));
    vi.stubGlobal("fetch", fetchMock);
    setTokens("access-1");

    const result = await restockUnsubscribe(SLUG);

    expect(result).toEqual({ active: false });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(`/api/products/${SLUG}/restock-notifications/`);
    expect(init.method).toBe("DELETE");
    expect(init.credentials).toBe("include");
    expect((init.headers as Record<string, string>).Authorization).toBe(
      "Bearer access-1",
    );
  });

  it("a guest opt-out lands on the 401 verbatim", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url.includes("/token/refresh/")) {
          return err(401, envelope("Token is invalid", "not_authenticated"));
        }
        return err(
          401,
          envelope(
            "Authentication credentials were not provided.",
            "not_authenticated",
          ),
        );
      }),
    );

    const e = await restockUnsubscribe(SLUG).catch((x) => x);

    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(401);
    expect(e.message).toBe("Authentication credentials were not provided.");
  });
});

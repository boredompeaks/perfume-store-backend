import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import type { Cart } from "./types";

/**
 * D-04b cycle 1 regression: the access token is memory-only, so on a full
 * page load the auth bootstrap settles asynchronously. A cart GET fired
 * during "loading" carries no bearer, resolves (and creates) the anonymous
 * session cart, and caches that empty basket. These tests pin the three
 * properties that keep that from happening: no fetch until the bootstrap
 * settles, identity-keyed cache slots, and a refetch when the identity key
 * (or the ["cart"] prefix invalidation from login/register) changes.
 */
const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => void storage.set(key, value),
  removeItem: (key: string) => void storage.delete(key),
});

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function makeCart(id: number, name: string, quantity: number): Cart {
  return {
    id,
    session_id: `session-${id}`,
    items: [
      {
        id: id * 10,
        quantity,
        created_at: "2026-10-01T00:00:00Z",
        product: {
          id: id * 100,
          name,
          slug: name.toLowerCase().replace(/\s+/g, "-"),
          description: "",
          price: "1200.00",
          size: 50,
          stock: 10,
          category: "Eau de Parfum",
          image: null,
          created_at: "2026-10-01T00:00:00Z",
        },
      },
    ],
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
  };
}

const GUEST_CART = makeCart(31, "Guest Bergamot", 2);
const ACCOUNT_CART = makeCart(7, "Account Oud", 2);

async function waitFor(check: () => boolean, label: string) {
  const deadline = Date.now() + 2_000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

function stubCartFetch(seen: Array<string | undefined>) {
  const fetchSpy = vi.fn(
    async (_input: string | URL | Request, init?: RequestInit) => {
      const auth = (init?.headers as Record<string, string> | undefined)
        ?.Authorization;
      seen.push(auth);
      return jsonResponse(auth ? ACCOUNT_CART : GUEST_CART);
    },
  );
  vi.stubGlobal("fetch", fetchSpy);
  return fetchSpy;
}

beforeEach(() => {
  storage.clear();
  vi.resetModules();
});

describe("cart query gating (D-04b)", () => {
  it("never fetches while the auth bootstrap is loading", async () => {
    const seen: Array<string | undefined> = [];
    const fetchSpy = stubCartFetch(seen);
    const { cartQueryOptions } = await import("./use-cart");
    const client = new QueryClient();
    const observer = new QueryObserver(client, cartQueryOptions("loading"));
    const unsubscribe = observer.subscribe(() => {});

    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(observer.getCurrentResult().fetchStatus).toBe("idle");
    unsubscribe();
  });

  it("fetches once without a bearer while anonymous", async () => {
    const seen: Array<string | undefined> = [];
    const fetchSpy = stubCartFetch(seen);
    const { cartQueryOptions } = await import("./use-cart");
    const client = new QueryClient();
    const observer = new QueryObserver(client, cartQueryOptions("anonymous"));
    const unsubscribe = observer.subscribe(() => {});

    await waitFor(
      () => observer.getCurrentResult().isSuccess,
      "anonymous cart",
    );

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(seen).toEqual([undefined]);
    expect(observer.getCurrentResult().data).toEqual(GUEST_CART);
    unsubscribe();
  });

  it("fetches once with the bearer once authenticated", async () => {
    const seen: Array<string | undefined> = [];
    const fetchSpy = stubCartFetch(seen);
    const tokens = await import("./tokens");
    tokens.setTokens("access-1");
    const { cartQueryOptions } = await import("./use-cart");
    const client = new QueryClient();
    const observer = new QueryObserver(client, cartQueryOptions("authenticated"));
    const unsubscribe = observer.subscribe(() => {});

    await waitFor(
      () => observer.getCurrentResult().isSuccess,
      "account cart",
    );

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(seen).toEqual(["Bearer access-1"]);
    expect(observer.getCurrentResult().data).toEqual(ACCOUNT_CART);
    unsubscribe();
  });

  it("fetches when loading flips to authenticated, and refetches after the login invalidation", async () => {
    const seen: Array<string | undefined> = [];
    const fetchSpy = stubCartFetch(seen);
    const { cartQueryOptions } = await import("./use-cart");
    const client = new QueryClient();

    const loading = new QueryObserver(client, cartQueryOptions("loading"));
    const unsubscribeLoading = loading.subscribe(() => {});
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(loading.getCurrentResult().data).toBeUndefined();

    const tokens = await import("./tokens");
    tokens.setTokens("access-1");

    const authenticated = new QueryObserver(
      client,
      cartQueryOptions("authenticated"),
    );
    const unsubscribeAuthenticated = authenticated.subscribe(() => {});
    await waitFor(
      () => authenticated.getCurrentResult().isSuccess,
      "post-bootstrap account cart",
    );

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(seen).toEqual(["Bearer access-1"]);

    // LoginForm/RegisterForm invalidate the ["cart"] prefix. Keyed query
    // keys ("cart", status) must still match that prefix.
    await client.invalidateQueries({ queryKey: ["cart"] });
    await waitFor(() => fetchSpy.mock.calls.length === 2, "login refetch");

    expect(seen).toEqual(["Bearer access-1", "Bearer access-1"]);

    unsubscribeLoading();
    unsubscribeAuthenticated();
  });

  it("never reuses the anonymous cart for the authenticated identity", async () => {
    const seen: Array<string | undefined> = [];
    stubCartFetch(seen);
    const { cartQueryOptions } = await import("./use-cart");
    const client = new QueryClient();

    const anonymous = new QueryObserver(client, cartQueryOptions("anonymous"));
    const unsubscribeAnonymous = anonymous.subscribe(() => {});
    await waitFor(
      () => anonymous.getCurrentResult().isSuccess,
      "anonymous cart",
    );
    expect(anonymous.getCurrentResult().data).toEqual(GUEST_CART);

    const tokens = await import("./tokens");
    tokens.setTokens("access-1");

    const authenticated = new QueryObserver(
      client,
      cartQueryOptions("authenticated"),
    );
    // The identity key starts with no data at all: the guest basket cannot
    // leak into the post-sign-in render.
    expect(authenticated.getCurrentResult().data).toBeUndefined();

    const unsubscribeAuthenticated = authenticated.subscribe(() => {});
    await waitFor(
      () => authenticated.getCurrentResult().isSuccess,
      "account cart",
    );

    expect(authenticated.getCurrentResult().data).toEqual(ACCOUNT_CART);
    expect(authenticated.getCurrentResult().data).not.toEqual(GUEST_CART);
    expect(seen).toEqual([undefined, "Bearer access-1"]);

    unsubscribeAnonymous();
    unsubscribeAuthenticated();
  });
});

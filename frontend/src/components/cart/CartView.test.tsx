import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import type { Cart } from "@/lib/types";
import CartView from "./CartView";

/**
 * The cache is keyed by settled auth identity, so seeding both slots reproduces
 * the sign-in transition and proves a guest basket cannot leak into it.
 */
const auth = vi.hoisted(() => ({
  status: "anonymous" as "loading" | "authenticated" | "anonymous",
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    status: auth.status,
    userId: auth.status === "authenticated" ? 7 : null,
    login: vi.fn(),
    logout: vi.fn(),
  }),
}));

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

function renderWith(seed: Array<[readonly unknown[], Cart]>): string {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
    },
  });
  for (const [key, cart] of seed) client.setQueryData(key, cart);
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <CartView />
    </QueryClientProvider>,
  );
}

describe("CartView identity-keyed cart cache", () => {
  it("never renders a stale anonymous basket after sign-in", () => {
    auth.status = "authenticated";
    const html = renderWith([[["cart", "anonymous"], GUEST_CART]]);

    expect(html).not.toContain("Guest Bergamot");
    // No authenticated cache entry: wait on the gated fetch, not the guest cart.
    expect(html).toContain("skeleton");
  });

  it("renders the account cart, not the anonymous one, when both are cached", () => {
    auth.status = "authenticated";
    const html = renderWith([
      [["cart", "anonymous"], GUEST_CART],
      [["cart", "authenticated"], ACCOUNT_CART],
    ]);

    expect(html).toContain("Account Oud");
    expect(html).not.toContain("Guest Bergamot");
    expect(html).toContain("Proceed to checkout");
  });

  it("still renders the anonymous cart while signed out", () => {
    auth.status = "anonymous";
    const html = renderWith([[["cart", "anonymous"], GUEST_CART]]);

    expect(html).toContain("Guest Bergamot");
    // The anonymous basket is not a dead end.
    expect(html).toContain("Continue to checkout");
    expect(html).toContain("sign in");
  });
});

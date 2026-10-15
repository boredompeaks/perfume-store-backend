import { apiFetch } from "./api";
import type { Product, ProductPage } from "./types";

export function fetchProductsPage(
  params: URLSearchParams,
): Promise<ProductPage> {
  return apiFetch<ProductPage>(`/api/products/?${params.toString()}`, {
    revalidate: 60,
  });
}

export function fetchProduct(slug: string): Promise<Product> {
  return apiFetch<Product>(`/api/products/${slug}/`, { revalidate: 60 });
}

/** SPEC-19-4 [R-19.11]: one armed preference on one product. */
export interface RestockPreference {
  product_id: number;
  slug: string;
  active: boolean;
}

export interface RestockOptIn {
  /** 201 = first opt-in; 200 = an existing preference re-armed. */
  created: boolean;
  preference: RestockPreference;
}

/**
 * Back-in-stock opt-in (§12.G6). The endpoint is authenticated and
 * ownership-scoped server-side: the address the trigger mails is the
 * account's own, so `email` rides along for the form's contract and is
 * ignored by today's view (BACKEND_REQUESTS P1). Every 4xx arrives as the
 * uniform SPEC-9-03 envelope and surfaces as ApiError with the server's
 * message verbatim — the caller never rewrites it.
 */
export async function restockSubscribe(
  slug: string,
  email: string,
): Promise<RestockOptIn> {
  let status = 0;
  const preference = await apiFetch<RestockPreference>(
    `/api/products/${slug}/restock-notifications/`,
    {
      method: "POST",
      body: { email },
      auth: true,
      onStatus: (code) => {
        status = code;
      },
    },
  );
  return { created: status === 201, preference };
}

/**
 * Opt-out: a uniform, existence-hiding 200 (`{active: false}`) whether or
 * not a preference existed.
 */
export function restockUnsubscribe(slug: string): Promise<{ active: boolean }> {
  return apiFetch<{ active: boolean }>(
    `/api/products/${slug}/restock-notifications/`,
    { method: "DELETE", auth: true },
  );
}
